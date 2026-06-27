# 设计：铁鹰平仓改为可插拔策略（复用既有 close_strategies 框架）

日期：2026-06-27 · 能力：building-production-feature · 状态：**已实现**（照四点默认；227 测试全绿，默认 threshold 与旧 exit_decision 逐条等价）

> 实现补记：旧 exit_decision 的优先级是「止盈>止损>到期前」，故 `force_close_dte` 在
> `BaseCloseStrategy.decide` 中置于**最低优先级**（盈利/止损先判，仅 pnl 不可得时兜底强平），
> 而非安全优先的最高位——以严格保持铁鹰旧行为；straddle（force_close_dte=None）流程不变。

> 现状：straddle/回测已用 `close_strategies.py`（接口+安全基类+threshold/trailing/breakeven/
> bracket+注册表），但**铁鹰仍用硬编码的 `exit_decision`** 绕过该框架。本设计把铁鹰出场接入
> 同一框架，使其平仓策略可按配置插拔（尤其新增**移动止盈/回撤保护**）。
> **默认行为与今天完全一致**（用户已确认"符合预期"），仅在显式选 trailing 时才改变。

## 1. 问题

`condor.py: exit_decision(entry_credit, close_cost, dte, profit_target=0.5, stop_mult=2.0, dte_exit=21)`
是铁鹰专用、**信用口径**的固定三段式（止盈 50%×权利金 / 止损 2×权利金 / DTE≤21 强平），
与可插拔框架并存但不互通。straddle 能换 trailing/breakeven，铁鹰不能——不一致、且无法给铁鹰上移动止盈。

## 2. 阻抗差与桥接（关键）

| | 既有框架 | 铁鹰现状 |
|---|---|---|
| 盈亏口径 | **百分比** `pnl_percent` | **信用** `pnl=entry_credit−close_cost` |
| 时间出场 | 盘中 `minutes_to_close` 收盘前强平 | **多日** `dte≤dte_exit` |

**桥接 1 — 归一化（base=入场权利金）**：`pnl_percent = (entry_credit − close_cost) / entry_credit × 100`。于是
- 今天止盈 `pnl ≥ 0.5×credit` ⟺ `tp_percent=50 = condor_profit_target×100`
- 今天止损 `pnl ≤ −2×credit` ⟺ `sl_percent=200 = condor_stop_mult×100`
- ⇒ `ThresholdStrategy(sl=200, tp=50)` **逐条复现** exit_decision 的止盈/止损。trailing 的"回撤%"即"占权利金的%"。

**桥接 2 — DTE 强平**：铁鹰的 `dte≤dte_exit` 是纯多日触发，非盘中 EOD。给 `BaseCloseStrategy`
加一个**向后兼容**的 `force_close_dte=None`：decide() 最前面判 `if force_close_dte is not None and
ctx.dte is not None and ctx.dte <= force_close_dte: return TIME_FORCE_CLOSE`。straddle 不传（None）→ 行为不变；
铁鹰传 `dte_exit`。安全（时间+硬止损）仍在基类，符合框架原设计哲学。铁鹰喂 `minutes_to_close=None`
（多日价差不做盘中 EOD），故只有 force_close_dte + 硬止损 + 子类盈利逻辑会触发。

**边界**：`close_cost` 不可得 → `pnl_percent=None`（基类对 None 已只判时间/不判盈亏，与今天一致）；
`entry_credit≤0` 不会发生（入场已 gate >0），仍加除零保护置 None。

## 3. 复用而非重建

- **不新增任何策略类**：直接用 `close_strategies.py` 现有 threshold/trailing/breakeven/bracket。
- 新增 `build_condor_close_strategy(cfg)`：把铁鹰参数映射进这些类（tp=profit_target×100、
  sl=stop_mult×100、force_close_dte=dte_exit、trailing 用 condor_trail_*），`close_buffer` 任意
  （因 minutes_to_close=None，EOD 分支永不进）。**与 straddle 的 build_strategy 并列、互不影响**
  （两者 tp/sl 字段来源不同：straddle 用 tp_percent/sl_percent，铁鹰用 condor_* 映射）。

## 4. 接入点

- `close_strategies.py`：`BaseCloseStrategy.__init__(..., force_close_dte=None)` + decide() 顶部判 DTE；
  新增 `build_condor_close_strategy(cfg)`。
- `condor.py`：
  - `__init__`：`self._strategy = build_condor_close_strategy(cfg)`。
  - `_monitor_once`：算 `pnl_percent` → 构 `StrategyContext(pnl_percent, minutes_to_close=None,
    dte=dte, opened_at=self._opened_at, now_ts=self._now_ms())` → `reason = self._strategy.decide(ctx)`。
    替换原 `exit_decision(...)` 调用。
  - **持久化**：`_snapshot()` 写 `strategy_name + strategy_state=self._strategy.state()`；
    `resume()` 重建策略并 `load_state()`（trailing 的 armed/peak 跨重启不丢）。
  - **删除** `exit_decision`（接入后无调用者；其 6 个单测改写为 ThresholdStrategy 等价断言）。
- `domain/models.py`：
  - `StrategyConfig` += `condor_close_strategy='threshold'`、`condor_trail_activation=0.0`、
    `condor_trail_giveback=0.0`（0=trailing 关；单位=占权利金的%）。
  - `CondorSnapshot` += `strategy_name='threshold'`、`strategy_state: dict`（镜像 TradeSnapshot，
    from_dict 已忽略未知字段→兼容旧快照）。
- `service.py`：读 `OBOT_CONDOR_CLOSE_STRATEGY / _TRAIL_ACTIVATION / _TRAIL_GIVEBACK`。
- `shadow.py`：`mark()` 改用 `build_condor_close_strategy(cfg)` + decide()，与引擎一致；
  trailing 的 armed/peak 存进影子 JSON 状态（影子本就持久化）。
- `deploy.md`：补 condor 平仓策略配置表。

## 5. 默认等价性（必须保证）

不配任何新变量时：`condor_close_strategy='threshold'` → `ThresholdStrategy(sl=stop_mult×100=200,
tp=profit_target×100=50, force_close_dte=dte_exit=21)`，逐情形等于今天的 exit_decision。
**HK 现网默认无变化**，除非显式 `OBOT_CONDOR_CLOSE_STRATEGY=trailing`。

## 6. 测试
- **等价性**：参数化对照——对一组 (entry_credit, close_cost, dte)，
  `build_condor_close_strategy(threshold).decide(ctx)` 的结论 == 旧 exit_decision（覆盖止盈/止损/
  DTE/持有/优先级/None 边界，即原 6 个用例）。
- **force_close_dte**：dte≤阈值→TIME_FORCE_CLOSE；straddle（force_close_dte=None）不受影响（回归）。
- **trailing on 铁鹰**：信用归一化下，峰值+40% 回撤到 +25% → TRAILING_STOP；武装前不触发；
  state()/load_state() 往返。
- **持久化/恢复**：MONITORING 快照含 strategy_state，resume 后 trailing 峰值还原。
- **shadow**：mark 用新 builder，trailing 状态在样本间累积。
- 既有 220 全绿不回归。

## 7. 待确认
1. **归一化以"入场权利金"为基**（tp=50%/sl=200% 精确等于今天的 0.5×/2×；trailing% 即占权利金%）？（推荐）
2. **本期范围 = threshold（默认，精确等价）+ trailing（移动止盈）**；breakeven/bracket 框架已具备、按需后开？（推荐）
3. **铁鹰独立配置**（新增 condor_trail_*，与 straddle 的 trail_* 解耦，因同时只跑一种模式）？（推荐）
4. **删除 exit_decision**（接入后无调用者，测试改为策略等价断言），保证默认零行为变化？（推荐）
