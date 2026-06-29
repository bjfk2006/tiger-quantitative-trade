# 设计：铁鹰盈亏回测器（condor P&L backtester）

日期：2026-06-29 · 能力：building-production-feature · 状态：**设计待确认（未实现）**

> 起因：现有 `python -m option_bot.backtest`（engine.py）是**单腿/滚动 ATM**，**不建模四腿铁鹰**，
> 故给不出 condor 的真盈亏（见 `docs/backtest/2026-06-29-iv-entry-gate-frequency.md` §8）。本设计新增一个
> **铁鹰盈亏回测器**：用历史期权链逐日重放 live 的开仓/持仓/出场决策，输出真·盈亏统计。
> **核心原则：最大化复用 live 纯函数，回测=编排，不重写策略逻辑。**

## 1. 数据来源与约束（已核实）

dolt `option_chain` 表字段：`date, expiration, strike, call_put, bid, ask`——**只有 bid/ask，无 delta/IV**
（与实盘账户一致，[condor-account-data-gap]）。`stocks.ohlcv` 提供标的日 close。

⇒ 回测必须走 live 的**合成 greeks 路径**：平价反推现价 `implied_spot` + ATM 活 IV `atm_iv_live` +
BS 自算 delta `enrich_greeks`，再 `select_by_delta` 选腿。**与实盘同一套近似，回测才忠实。**

- 复用 `dolt_source`：`load_underlying_closes`（日期轴/现价参照）、`load_symbol_chain`（取 calls+puts 链快照）。
- **宿主机运行**（容器无 dolt）；数据须覆盖该标的/区间（SPY 有；QQQ 需确认 dolt 仓库覆盖）。

## 2. 算法（日线近似，复用 live 纯函数）

**预处理**：
1. 载入 [from, to+horizon] 的标的 close + 全期权链（call & put）。
2. 按日期分组成**每日链快照** `{date: [rows(exp,strike,pc,bid,ask)]}`。
3. 预算**每日 ATM 活 IV 序列** `iv[date]`：每日 `implied_spot` → `atm_iv_live`。供入场闸 + IV-Rank。

**入场（单仓顺序，忠实 live 单仓语义）**：对候选日 d
1. **入场闸**：`passes_entry_gate(iv[d], min_iv, rth=True, has_position=False, mode,
   ivp=iv_percentile(iv[d−lookback:d], iv[d]), min_rank, rank_floor,
   history_ok=足够长)`。IV-Rank 直接用回测窗口内自算的 IV 序列分位——**回测有全序列，无需 VIX 种子**。
2. 过闸 → 选到期（链中最接近 `target_dte` 且 > `dte_exit`）→ 当日该到期的 call/put 行
   `enrich_greeks`（合成）→ `build_condor(calls, puts, short_delta, wing_width)` 得四腿。
3. `entry_credit = net_credit(legs, quotes_d, 'conservative', closing=False)`；≤0 跳过。
   `max_loss = condor_max_loss(...)`；`qty = size_by_max_loss(...)`。

**持仓（逐日盯市直到出场/到期）**：对入场后每个交易日 j（用 (exp,strike,pc) 在 j 日快照查四腿 bid/ask）
- `close_cost = net_credit(legs, quotes_j, 'mid', closing=True)`；`dte_j`；
  `pnl_pct = condor_pnl_percent(entry_credit, close_cost)`。
- `reason = strat.decide(StrategyContext(pnl_percent=pnl_pct, minutes_to_close=None, dte=dte_j, ...))`，
  其中 `strat = build_condor_close_strategy(cfg)` **每笔建一次**（trailing 有状态，逐日喂同一实例）。
- 命中 reason → 当日 `close_cost` 平仓记账；未命中至到期 → 到期日按内在价结算（TIME_FORCE_CLOSE）。
- 某腿当日缺报价 → 持有跳过该日（不误判），记 missing。

**单仓顺序**：一笔出场后，从次日继续找下一入场（镜像 live"单仓、平了再开"）。
（可选 `--independent` 改为每 step_days 独立入场并行统计，便于看胜率分布；默认单仓顺序。）

## 3. 复用清单（新代码只做编排）

`build_condor` / `select_by_delta` / `nearest_strike_row` / `net_credit` / `condor_max_loss` /
`size_by_max_loss` / `condor_pnl_percent` / `build_condor_close_strategy` / `passes_entry_gate` /
`atm_iv_live` / `implied_spot` / `enrich_greeks` / `greeks_missing` / `iv_percentile`。
**这些已在 live 用、已单测**；回测器不复制任何策略判定。

## 4. 落点
- `option_bot/backtest/condor_engine.py`（新）：`run_condor_backtest(closes, chain, cfg, ...)` →
  `{summary, trades}`；纯编排，入参为已载入的数据（便于单测，不碰 dolt）。
- `option_bot/backtest/__main__.py`：加 `--condor` 模式（与 `--rolling-atm` 并列），复用 condor_* 参数
  （short-delta/wing/target-dte/dte-exit/min-iv/gate-mode/min-iv-rank/close-strategy/trail-*…）。
- `option_bot/tests/test_condor_backtest.py`：合成链（已知 σ 的 BS 定价 + 构造价格路径）→
  断言止盈/止损/到期/trailing 各出场、pnl 口径、单仓顺序不重叠、缺报价跳过。

## 5. 输出
- **summary**：笔数、胜率、总盈亏($/%)、均值、最大盈/亏、平均持有天、出场原因分布、
  **顺序权益曲线的最大回撤**、profit factor（毛盈/毛亏）。
- **trades**：每笔 entry/exit 日、四腿行权价、entry_credit、exit_cost、pnl($ 与 %ofMaxLoss)、reason、days_held、peak。
- `--json` 输出；文本模式打印 summary + 最好/最差各 3 笔（仿 rolling-ATM）。
- 可对 **SPY / QQQ** 分别跑，直接对比（呼应 §QQQ 频率：验证"频率高是否真换来更高收益，还是被击穿吃掉"）。

## 6. 诚实与局限（写入输出脚注）
- **日线近似**：无盘中；出场按当日 close_cost(mid)，比真实盘中触发**晚一拍**；**跳空**体现为次日跳变（贴近真实但出场价用当日值）。
- **成交假设**：开仓用 conservative(marketable)、平仓用 mid 的净价；真实多腿 combo 成交特性不同（paper 实测不可靠）。
- **合成 greeks**：历史无 delta/IV，选腿/IV 全靠 BS/平价近似（同 live）；平值单一 IV、无 skew。
- **不建模**：美式提前行权/指派（铁鹰 DTE21 前平、影响小）、保证金变动、分红。
- **数据依赖**：dolt `option_chain` 须覆盖该标的与整个持仓区间；缺腿报价的日按"持有"处理并计数。
- 回测结果是**历史近似**，不预示未来；卖方策略的尾部（跳空击穿）在日线下可能被低估。

## 7. 待确认
1. **单仓顺序回测**（忠实 live 单仓、可出权益曲线/回撤）为默认，另留 `--independent` 滚动入场看胜率分布？（推荐）
2. 历史无 greeks → **合成 greeks 选腿 + 自算 ATM IV**（与 live 同路径，唯一可行）？（推荐）
3. 入场闸支持 **absolute/rank/both**，IV-Rank 用**回测窗口内自算 IV 序列**的分位（无需 VIX 种子）？（推荐）
4. 出场**复用 `build_condor_close_strategy`**（threshold/trailing，逐日喂状态），日线近似？（推荐）
5. CLI 扩展现有 **`python -m option_bot.backtest --condor`**（不另起入口）？（推荐）
6. 先在 **SPY** 验证回测器正确性，再跑 **QQQ** 对比（需先确认 dolt 有 QQQ 期权链覆盖）？（推荐）
