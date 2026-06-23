# 设计 — 双向跨式（straddle）多腿持仓 + 腿管理

**Date**: 2026-06-23
**类型**: 架构增量（building-production-feature §Design，多腿持仓，新路径）
**状态**: 待确认（未写代码）

## 1. 目标（一句话）
同时买入同标的、同到期、同行权价的 **1 张 call + 1 张 put（长跨式）**；按腿管理：**某腿亏到 −leg_stop% 即平该腿**（让市场筛出赢家方向），**组合盈亏按「固定/移动」两种止盈方式之一了结剩余腿**；**只用收盘前强平兜底，无硬止损**。

**意图**：多腿不是为了对冲，而是**用一对反向腿让市场自己暴露赢家方向**——亏腿在 −leg_stop% 被砍掉，赢腿留下；组合止盈支持**移动止盈**，好让赢家在大波动里**跑出高额利润**（固定止盈会过早封顶，违背跨式吃大波动的初衷）。

## 2. 为什么是「新路径」而非「加平仓策略」
现有 `CloseStrategy` 接口是对**单腿 pnl%** 决策、动作只有"整仓平/持有"。跨式需要：① 持有**两条腿**；② **跨腿**组合盈亏；③ **平单条腿**（partial close）。这超出单腿策略接口，需要一个**独立的多腿管理器**，与现有单腿路径**并行**（互不影响、不改既有单腿流程）。

## 3. ⚠️ 交易前提（必须知晓，已与用户确认）
长跨式付**两份**权利金，**只有单边波动足够大**才盈利；横盘/小波动时**两腿同亏**（theta+IV 双杀），"平掉亏腿后剩余腿必盈"**不成立**。`leg_stop=−10%` 在标的**小幅move**时就会触发（很早就给一条腿定生死）。本设计**按用户规则实现**，不替用户判断盈利性；用时间强平在收盘前兜底了结。

## 4. 架构
新增并行管理器，**复用**既有 adapters / sink / state store（多腿快照）：
```
StraddleManager (strategy/straddle.py)
  ├ 开仓: 解析 call+put 两腿 → TradingAdapter.open_market ×2
  ├ 监控: 每 tick 取两腿持仓(get_option_position) → 算 per-leg pnl% + 组合 pnl%
  ├ 腿管理: 某腿 ≤ −leg_stop% → close_market 平该腿(记已实现)
  ├ 组合止盈: 组合 pnl% ≥ target → 平掉所有未平腿
  ├ 时间强平: 距收盘 ≤ buffer → 平掉所有未平腿
  └ 持久化: StraddleSnapshot(双腿 + 已实现 + 状态)；resume 以券商持仓为准
StraddleSupervisor/loop: 类似 MonitorLoop 的 tick 循环(可复用其外壳)
```
- **数据层已天然支持多腿**：`positions` 表按 identifier、`trades` 按腿追加、sink.on_open/on_position/on_close **逐腿调用** → 看板自动显示两行、历史统计自动按腿配对。无需改持久化 schema。
- **路由**：`OBOT_MODE=single|straddle`（或 CLI 子命令 `run-straddle`）。single 走现有单腿；straddle 走新管理器。默认 single。

## 5. 数据模型
```
@dataclass
class StraddleLeg:
    identifier: str; put_call: str; qty: int
    entry_price: float|None; open_order_id: int|None
    closed: bool=False; realized_pnl: float=0.0   # 平腿时记 (close-entry)*qty*100

@dataclass
class StraddleSnapshot:
    account, symbol, expiry, strike, qty
    legs: list[dict]          # 两腿
    state, opened_at, external_id
    tp_mode: str              # fixed / trailing
    combo_armed: bool=False   # trailing 模式：组合是否已武装
    combo_peak: float|None=None  # 组合盈亏%峰值（trailing 用，每 tick 持久化、崩溃可恢复）
```

## 6. 组合盈亏口径（确认：总成本×5~10%）
- 总成本 `C = Σ entry_i × qty × 100`（两腿权利金）。
- 已实现 `R = Σ(已平腿) (close−entry)×qty×100`（亏腿为负）。
- 未实现 `U = Σ(未平腿) (mkt−entry)×qty×100`。
- **组合盈亏% = (R + U) / C × 100**。
- 单腿止损用 **per-leg pnl% = (mkt−entry)/entry×100 ≤ −leg_stop%**。

**组合止盈支持两种模式（`straddle_tp_mode`）**：
- **`fixed`（固定）**：组合盈亏% ≥ `straddle_tp`（默认 10，5~10 可配）→ 平所有未平腿。简单、可预期，但会封顶赢家。
- **`trailing`（移动，推荐用于吃大波动）**：组合盈亏% ≥ `straddle_trail_activation`（默认 10）时**武装**并记**组合峰值**；之后组合盈亏% ≤ `峰值 − straddle_trail_giveback`（默认 10）→ 平所有未平腿。**赢家继续涨则止盈线跟随上移**，回撤才走，捕捉高额利润。
> 这与单腿的 threshold/trailing 同构，只是作用在**组合盈亏%**上。trailing 的"武装+峰值"是有状态的，需持久化（§5）。

## 7. 退出规则与优先级（每 tick）
1. **时间强平**（距收盘 ≤ close_buffer）→ 平所有未平腿（最高，唯一安全兜底）。
2. **组合止盈**（fixed: 组合 pnl% ≥ straddle_tp；trailing: 武装后回撤达 giveback）→ 平所有未平腿。
   - trailing 两阶段：先更新组合峰值/武装，再判回撤（同单腿 trailing）。
3. **腿止损**（某未平腿 pnl% ≤ −leg_stop）→ 平该腿（partial）。
4. 持有。
> 无硬止损（用户确认）。两腿都被腿止损平掉后 → 持仓为空 → 结束（净亏两腿，属该策略固有风险）。
> 注：组合止盈优先于腿止损——若组合已达标就整体了结，不必再纠结单腿。

## 8. 配置
| env / CLI | 说明 | 默认 |
|---|---|---|
| `OBOT_MODE` / `--mode` | `single`/`straddle` | single |
| `OBOT_STRADDLE_LEG_STOP` / `--leg-stop` | 单腿止损%（亏到即平该腿） | 10 |
| `OBOT_STRADDLE_TP_MODE` / `--straddle-tp-mode` | 组合止盈方式 `fixed`/`trailing` | fixed |
| `OBOT_STRADDLE_TP` / `--straddle-tp` | fixed：组合止盈%（总成本占比，5~10） | 10 |
| `OBOT_STRADDLE_TRAIL_ACTIVATION` / `--straddle-trail-activation` | trailing：组合武装阈值% | 10 |
| `OBOT_STRADDLE_TRAIL_GIVEBACK` / `--straddle-trail-giveback` | trailing：组合从峰值回撤% | 10 |
| `OBOT_CLOSE_BUFFER` | 收盘前强平分钟（复用） | 5 |
| 开仓 | `OBOT_SYMBOL/EXPIRY/STRIKE/QTY`（同行权价 call+put） | — |

## 9. 影响/新增文件
| 文件 | 改动 |
|---|---|
| `strategy/straddle.py`（新） | `StraddleLeg`/`StraddleManager`（open/poll/leg-stop/combined-tp/time-force/close_all/resume/persist）|
| `domain/models.py` | `StraddleConfig` 或扩 StrategyConfig（mode/leg_stop/straddle_tp）；`StraddleSnapshot` |
| `config/state_store.py` | 复用；或加 straddle 专用快照文件（与单腿分开，避免混淆）|
| `service.py` / `cli/main.py` | 按 `OBOT_MODE`/`--mode` 路由到 StraddleManager；开仓双腿参数 |
| `adapters/market_data.py` | 加 `resolve_pick` 的 put 版本（或 resolve 同行权价 call+put 两腿）|
| `web`（可选） | 看板「当前持仓」已能显示两行；可加"组合盈亏"汇总行 |
| `tests/test_straddle.py`（新） | 组合盈亏、腿止损、组合止盈、时间强平、resume（用 mock adapter）|

> 不改现有单腿状态机/策略；straddle 是并行新增。持久化 schema 不变。

## 10. 核心流程
```
开仓: 同标的/到期/行权价 → resolve call+put → 各下 1 张市价单 → 记两腿 entry/cost → 持久化
每 tick:
  取两腿持仓 → 算 per-leg pnl% 与 组合 pnl%
  ① 距收盘≤buffer → 平所有未平腿 → 结束
  ② 组合pnl% ≥ straddle_tp → 平所有未平腿 → 结束
  ③ 某未平腿 ≤ −leg_stop → 平该腿(记 realized)；若两腿都平 → 结束
崩溃恢复: 读快照 → 以券商两腿实际持仓为准对齐 → 继续
```
- **幂等**：每次平腿前查 salable_qty>0；已平腿不重复下单。
- **失败模式**：某腿下单/平仓被拒 → 退避重试该腿；组合盈亏在数据缺失时该 tick 跳过(不误平)。

## 11. 待确认 / 边界
1. **同行权价(straddle) vs 不同行权价(strangle)**：本设计先做 **straddle（同 ATM 行权价）**。要 strangle 再加（call/put 各一个行权价）。
2. **行权价选取**：用 `--strike` 指定；不指定则取**最接近现价的平值**（用期权链 delta≈0.5，正股价取不到时用链推）。
3. **leg_stop=−10% 偏早**：标的小幅波动就会平掉一条腿，可能过早定方向。可调大 `--leg-stop`，或你接受按 −10% 实现。
4. **资金**：两份权利金，实盘需够买两张（看板/资金校验照单腿逻辑各查一次）。
5. **组合止盈优先于腿止损**——确认这个优先级 OK（先到组合目标就整体走）。

## 12. 实现计划（确认后）
1. domain：StraddleSnapshot + 配置字段。
2. `straddle.py`：StraddleManager（纯可测的盈亏/决策 + 调 adapter 的 IO）。
3. market_data：resolve call+put 两腿。
4. service/cli：`--mode straddle` 路由 + 双腿开仓参数 + 安全闸（实盘自动开仓仍受 OBOT_ALLOW_LIVE_AUTO_OPEN 约束）。
5. 单测（纯逻辑：组合盈亏/优先级/腿止损/恢复）。
6. .env.example + deploy.md 文档。
7. 香港重建验证（模拟盘下端到端：开两腿→腿止损→组合止盈/时间强平）。

---

**确认点**：
① straddle（同行权价）先做、strangle 以后；
② 组合止盈（fixed/trailing 任一）优先于腿止损；
③ leg_stop 默认 −10% 你接受（知道它偏早）；
④ 组合止盈**默认 fixed**、可切 trailing（按你的意图，吃大波动用 trailing）——默认值 OK 吗？还是默认就上 trailing？
确认后我实现。
