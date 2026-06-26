# 设计：铁鹰卖方策略引擎（IV 择时 + 人工确认开仓 + 自动监控/出场）

日期：2026-06-26 · 能力：building-production-feature · 状态：**P1 已实现（纯核心+编排已单测；combo 下单语义待 paper 验证）**

> 实现落点：`option_bot/strategy/condor.py`（纯决策核心 + CondorManager/CondorSupervisor）、
> `adapters/trading.py::place_combo`、`adapters/market_data.py::get_underlying_price`、
> `domain/models.py`（CondorLeg/CondorSnapshot/BotState.PROPOSED + condor_* 配置）、
> `service.py`（condor 模式 + CMD_APPROVE/REJECT）、`web/ops.py`（/ops/approve、/ops/reject）。
> 测试：`tests/test_condor.py`（31 例：纯决策 + 提案/批准/监控止盈/恢复编排，全绿）。
> **未决**：combo 净价正负/动作约定（`_OPEN_ACTION`/`_CLOSE_ACTION`）须在 paper 账户实测确认；
> 真 IV-Rank、滚动、回撤降挡、多仓为 Phase 2。

落地 `docs/strategy/2026-06-26-iv-timed-defined-risk-premium-selling.md` 的执行手册。**真钱、架构级新增**——多腿组合 + IV 择时 + 组合风控 + 人工确认开仓。

## 1. 范围（MVP Phase 1，避免过度设计）

**做**：
- 单个铁鹰持仓（一次一个），标的为可配置的美股流动品种（SPY/QQQ）。
- **自动**：IV 入场闸 → 结构构建（按 delta 选行权价）→ 产出开仓提案 → 自动监控与出场（+50% 止盈 / −2× 止损 / ≤21DTE 平）。
- **人工**：开仓必须经 ops `approve` 显式确认才提交。
- 默认 **paper 账户**;实盘需既有安全闸 + 每次人工确认。

**Phase 2 推迟**（确认 P1 跑通后再做）：被突破滚动、回撤分级降挡、并发多仓/多标的、真正的 IV-Rank（需自建 IV 历史）。

## 2. 可行性（已查证，含证据）

| 能力 | 结论 | 证据 |
|---|---|---|
| 期权 IV + 希腊字母 | ✅ | `quote_client.get_option_briefs` 返回 `implied_vol/delta/gamma/theta/vega`；`OptionFilter(delta_min/max, implied_volatility_min/max)` |
| 按 delta 选行权价 | ✅ | `quote/domain/filter.py` GREEKS 过滤 |
| 多腿组合下单 | ✅ | `trade_client.place_order(contract_legs, combo_type='VERTICAL')`，示例为 AAPL 垂直价差单笔 MLEG |
| 多腿先例 | ✅ | `option_bot/strategy/straddle.py` StraddleManager/Supervisor |

## 3. 架构

新增 `option_bot/strategy/condor.py`：

- **纯决策函数**（无 SDK，易单测）：
  - `passes_entry_gate(iv_metric, rth, has_position, ...)` → 入场闸。
  - `select_structure(chain_df, spot, target_dte, short_delta, wing_width)` → 选两腿×2 的行权价/到期。
  - `exit_decision(credit, current_value, dte, profit_target, stop_mult, dte_exit)` → +50%/−2×/≤21DTE。
- **`CondorManager`**（IO，仿 StraddleManager）：构建提案、批准后提交 combo、监控、出场、快照/恢复。
- **`CondorSupervisor`**（仿 StraddleSupervisor）：编排循环、持有待批提案、处理 approve/reject。

**下单（关键安全点）**：新增 `TradingAdapter.open_combo(legs, combo_type, net_limit, mark)` / `close_combo(...)`，用 `order_utils` 的 combo_order **以净限价整体成交**。铁鹰 = 牛市认沽信用价差 + 熊市认购信用价差两个垂直 combo。**绝不逐腿市价进**（逐腿可能只成一腿 → 破坏定义风险）。净价不达可接受值则**放弃本次**。

## 4. 人工确认开仓流程

```
引擎每轮:
 1. 入场闸: IV 够高? RTH? 当前无持仓? 非事件窗口? —— 任一不满足 → 跳过
 2. 通过 → 构建结构(到期~30-45DTE, 短腿~16Δ, 翼宽~1σ), 询价净 credit, 算最大亏损/张数
 3. 产出"开仓提案": 日志 + 落 sink + ops/web 可查; 状态=AWAITING_APPROVAL
 4. 操作员复核 → ops `approve <token>` (或 `reject <token>`)
    - 提案有 TTL(如 N 分钟) 或现价漂移>阈值 → 自动作废重评(防批准到陈旧价)
 5. approve → 提交两个垂直 combo 净限价 → 确认成交 → MONITORING
 6. reject/超时 → 丢弃, 下轮重评
```

**自动执行(无需人工)**：+50% 止盈 / −2× 止损 / ≤21DTE 平——都是**降风险**动作,符合"开仓人工、其余自动"。

## 5. IV 闸 / IV-Rank

- **P1**：用当前 ATM `implied_vol`（briefs）对比**我们已能算的滚动已实现波动**（bot 已拉股价历史），或可配的绝对/相对阈值；**同时每日把 ATM IV 落库**，为 P2 的真 IV-Rank 积累历史。
- **P2**：累计 ≥3~6 个月 IV 历史后，用 1 年百分位算真正 IV-Rank（或 `implied_vol_30_days` 百分位）。

## 6. 配置（OBOT_CONDOR_*）

`UNDERLYING / TARGET_DTE(40) / SHORT_DELTA(0.16) / WING_WIDTH(1σ 或固定档) / MIN_IV_GATE / PROFIT_TARGET(0.5) / STOP_MULT(2.0) / DTE_EXIT(21) / MAX_LOSS_PCT(账户%) / QTY / PROPOSAL_TTL`。

## 7. 安全与诚实

- **默认 paper**；实盘走既有 `is_paper`/`ALLOW_LIVE_AUTO_OPEN` 闸 + **每次人工 approve**（双保险）。
- **定义风险 only**，单仓最大亏损 ≤ 配置账户%；P1 最多 1 仓。
- combo **净限价整笔成交**保证定义风险完整性。
- 崩溃恢复：快照（legs/combo id/state），`resume()` 与券商持仓核对（仿 straddle）。
- **诚实**：策略 edge 是 in-sample、未经盘外验证；引擎只是执行器,不保证盈利。**强烈建议先 paper 跑通再考虑实盘**。

## 8. 分阶段计划

- **P1（本次）**：condor.py（纯决策+Manager+Supervisor）+ adapter combo 下单/平仓 + ops `approve/reject` + 配置 + 单测 + 文档。paper-first。
- **P2**：滚动、回撤降挡、并发/多标的、真 IV-Rank。

## 9. 待确认决策

1. **范围**：先做 P1（单铁鹰、人工确认开仓、自动出场），滚动/降挡/多仓推迟？（推荐）
2. **标的**：P1 用 SPY 还是 QQQ？
3. **IV 闸**：P1 先用"绝对阈值 + 已实现波动对比"并积累 IV 历史，待数据够再上真 IV-Rank？（推荐）
4. **账户**：默认 paper，实盘走既有闸 + 人工 approve？（推荐）
5. **确认通道**：ops API `approve/reject`（+ web 显示提案）即可？
