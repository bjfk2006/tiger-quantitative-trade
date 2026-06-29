# 设计：铁鹰 BS 重定价回测器（B 方案）

日期：2026-06-29 · 能力：building-production-feature · 状态：**设计待确认（未实现）**

> 起因：上一版"重放市场报价"的回测器（`condor_engine.py`，设计 2026-06-29-condor-pnl-backtester）
> 已在 HK 跑通，但实证 dolt `option_chain` 是**孤立日切快照**——95 个可建仓日里仅 2 个建仓后还能
> 4 腿齐报价盯市（98% 为 0），**撑不起逐合约连续盯市**。本设计（B 方案）改用 **Black-Scholes 重定价**：
> 只用**连续的日收盘价 + 波动率指数**合成整条铁鹰 P&L，**完全不依赖 option_chain**，可跑全样本、
> 得到有统计意义的收益分布。**模型价、非市场成交价**，用于相对比较而非精确预测。

## 1. 关键洞察：用 BS 就不需要期权链

铁鹰每条腿的理论价 = `bs_price(spot, strike, T, iv, r, put_call)`。给定**每日现价**和**每日 IV**，
就能给四腿逐日定价、算平仓成本、跑出场。所需数据只剩两条**连续**序列：

| 数据 | 来源 | 连续性 |
|---|---|---|
| 标的日收盘价 | dolt `stocks.ohlcv`（`load_underlying_closes`） | ✓ 连续 |
| 日 ATM IV | 波动率指数 CSV：`iv_t=(VIX_close−gap)/100`（SPY→VIX, QQQ→VXN） | ✓ 连续 |

⇒ **不碰 option_chain，稀疏问题消失**；且 **QQQ 也能回测**（用 VXN，若 `stocks.ohlcv` 有 QQQ）。

## 2. 算法（全 BS 合成，单仓顺序，复用 live 纯函数）

**预处理**：把两条序列按日期对齐 → `[(date, spot, iv)]`（取交集日）。

**入场（候选日 t，单仓顺序镜像 live）**：
1. **入场闸**：`passes_entry_gate(iv_t, min_iv, True, False, mode, ivp=iv_percentile(iv 序列[t−lookback:t], iv_t), …)`。
   IV-Rank 用**连续 IV 序列**自身的分位（干净、无需种子）。
2. 过闸 → **合成行权价网格**：以 `spot_t` 为中心、按 `strike_spacing`（默认 $1）铺 ±N% 的行权价；
   对每格用 `bs_delta(spot_t, K, T0, iv_t, r, pc)` 赋 delta（`enrich_greeks` 复用）。
3. `build_condor(calls, puts, short_delta, wing_width)` 选 16Δ 短腿 + 翼（**复用 live**）。
   到期日 `expiry = t + target_dte`（日历日）。
4. **入场信用** = `net_credit(legs, bs_qbi_t, 'mid', closing=False)`，其中 `bs_qbi_t[id]` 的
   bid=ask=`bs_price(...)`（故 mid=BS 理论价）。可减 `slippage` 模拟保守成交。≤0 跳过。
   `max_loss=condor_max_loss(...)`；`qty=size_by_max_loss(...)`。

**持仓（每个序列日 j 至 dte≤0 或出场）**：
- `T_j=dte_j/365`；每腿 `bs_price(spot_j, K, T_j, iv_j, r, pc)`（`T_j≤0` 用内在价结算）。
- `close_cost = net_credit(legs, bs_qbi_j, 'mid', closing=True)`（可加 slippage）。
- `pnl_pct = condor_pnl_percent(entry_credit, close_cost)`。
- `reason = strat.decide(StrategyContext(pnl_percent, minutes_to_close=None, dte=dte_j))`，
  `strat = build_condor_close_strategy(cfg)` 每笔建一次、逐日喂状态（threshold/trailing）。
- 命中 → 当日 close_cost 平仓；到期未触发 → 内在价结算（TIME_FORCE）。
- **BS 总能定价 → 每笔都可完整跟踪**（无缺报价问题）⇒ 全样本有效，样本量足。

**单仓顺序**：平了次日再开（可 `--independent` 独立入场看分布）。

## 3. 复用清单（新代码仅编排）
`build_condor` / `select_by_delta` / `nearest_strike_row` / `bs_delta` / `bs_price` / `enrich_greeks` /
`net_credit` / `condor_max_loss` / `size_by_max_loss` / `condor_pnl_percent` /
`build_condor_close_strategy` / `passes_entry_gate` / `iv_percentile`。VIX/VXN 加载复用
`backtest/iv_gate_freq.load`。**不重写任何策略判定**。

## 4. 落点
- `option_bot/backtest/condor_engine.py`：新增 `run_condor_bs_backtest(spot_series, iv_series, cfg, *,
  multiplier=100, entry_to=None, independent=False, strike_spacing=1.0, slippage=0.0, risk_free=0.04)`
  → `{summary, trades}`（summary 同 quote 版：胜率/总均盈亏/最大盈亏/最大回撤/profit_factor/出场分布）。
  纯入参为两条序列，便于单测、不碰 dolt。
- `option_bot/backtest/__main__.py`：加 `--condor-bs` 模式 + `--vix-csv/--gap/--strike-spacing/--slippage/
  --div-yield`，复用 condor 参数；用 `load_underlying_closes`（stocks repo）+ `iv_gate_freq.load`(VIX/VXN) 组序列。
- `option_bot/tests/test_condor_bs_backtest.py`：合成 (spot, iv) 路径 →
  止盈/止损/到期/trailing 各出场、横盘 theta 盈利、崩盘击穿损失封顶在翼宽、单仓不重叠、入场≈信用。

## 5. 输出与用途
- summary + 逐笔（entry/exit、四腿、信用、平仓成本、pnl($/%权利金/%最大亏损)、reason、days、peak、iv/ivp）。
- **可对 SPY(VIX) / QQQ(VXN) 分别跑直接对比**——正面回答"QQQ 频率高是否真换来更高净收益，还是被击穿吃掉"。
- 适合**相对比较**：threshold vs trailing、不同入场闸（absolute/rank/both）、不同短腿Δ/翼宽、SPY vs QQQ。

## 6. 诚实与局限（写入输出脚注，重要）
- **模型价，非市场成交价**：用 BS 理论 mid，无真实买卖价差/盘口；`slippage` 仅粗略近似摩擦。
- **平 IV、无 skew（最大偏差）**：全腿用当日单一 IV（VIX−gap）。真实指数**看跌期权有 skew**
  （OTM put 更贵），且**崩盘时 OTM put 的 IV 比 ATM 涨得更多**（skew 变陡）。⇒ 本模型会
  **低估下行/尾部损失**——**正是你问的"跳空击穿"那种最坏情形会被低估**；方向性击穿（现价穿过短腿）
  本身能算对，但 vol 维度的额外损失会偏小。结论要据此打折，尤其别用它低估尾部风险。
- **VIX→ATM IV 的 gap≈4** 单点近似；VIX(30D) 用于 ~40D 期权（期限结构略偏）。
- **跳空**：现价/IV 为日收盘，隔夜跳空体现为次日跳变（P&L 能反映），但出场价用当日 BS 值、晚一拍。
- **合成行权价网格**（$spacing），非真实挂牌行权价；无股息（SPY ~1.3%，可加 `--div-yield`，默认 0）。
- 定位：**相对比较 / 量级感**，不是精确的实盘损益预测。真·市场成交回测仍需 A 方案（连续逐合约 EOD 数据）。

## 7. 待确认
1. **全 BS 合成、不依赖 option_chain**（只用连续日 close + 波动率指数；QQQ 用 VXN 也能跑）？（推荐）
2. **入场信用与逐日价值同基**（均 BS-mid），加可配 `slippage` 近似保守成交摩擦？（推荐）
3. **平 IV 无 skew**，并在输出明确"低估下行/尾部损失"，结果仅作相对比较？（推荐，诚实标注）
4. 复用 `build_condor`/`bs_*`/`net_credit`/可插拔出场/IV-Rank 闸 + **合成行权价网格**（spacing 可配）？（推荐）
5. CLI `python -m option_bot.backtest --condor-bs`，先 **SPY(VIX)** 验证正确性，再跑 **QQQ(VXN)** 对比（先确认 `stocks.ohlcv` 有 QQQ）？（推荐）
