# DTE 区分收盘强平 + 当日亏损上限（kill switch）

日期：2026-06-26 · 能力：building-production-feature · 状态：已确认默认值，待实现

## 背景与动机

06-25 实盘亏 $426，两条直接诱因：

1. **收盘前一刀切强平**：`BaseCloseStrategy.decide` 在 `minutes_to_close ≤ close_buffer` 时无条件 `TIME_FORCE_CLOSE`。一张 7DTE（06-25 开、07-02 到期）的 call 被当日收盘强平 −$107，本可持到次日等反弹。短到期（0/1DTE）当日平是对的，但多日期权不该被收盘窗口误杀。
2. **越亏越追**：当天连开三笔追入下跌的 SPCX，无单日止损闸，把前两日 +$416 的盈利一次性回吐。

## 目标

- **F1 DTE 区分收盘强平**：收盘前强平只作用于「临近到期」的期权（DTE ≤ 阈值），更长期权**持有过夜**，由止盈/止损/移动止盈继续盯。
- **F2 当日亏损上限**：当日**已实现**亏损达到上限即**停止当日开仓**（只挡开仓，不强平已有仓）。

## 已确认默认值

- `eod_close_max_dte = 1`：DTE ≤ 1（今天/明天到期）收盘前照常强平；DTE ≥ 2 持有过夜。
- `daily_loss_limit = 300`（美元）：当日已实现亏损 ≥ $300 停止当日开仓；0 = 关闭。

## F1 设计

DTE = `到期日 − 美东当日`（自然日）。到期日当天 DTE=0，前一天 DTE=1。

**落点**
1. `StrategyContext` 新增字段 `dte: Optional[int] = None`。
2. `state_machine.decide_close()` 注入 `ctx.dte`（与现有 `ctx.opened_at` 注入同处），用可注入的 `self._now_ms()` + `cfg.timezone` 算「美东今天」与 `pick.expiry`（YYYYMMDD）之差 → 可单测。
3. `BaseCloseStrategy.decide()` 改收盘强平分支：

```python
if ctx.minutes_to_close is not None and ctx.minutes_to_close <= self.close_buffer:
    if ctx.dte is None or ctx.dte <= self.eod_close_max_dte:
        return CloseReason.TIME_FORCE_CLOSE
    # 否则：DTE 较长，不收盘强平，落到下面的止损/止盈继续判
```

- `dte is None`（未知）→ 仍强平（安全默认，绝不把未知期限留到隔夜）。
- DTE 较长时**跳过的只是 TIME_FORCE_CLOSE**；硬止损、移动止盈/保本仍照常生效。
4. `eod_close_max_dte` 经 `BaseCloseStrategy.__init__(..., eod_close_max_dte=1)` 默认值持有；`build_strategy` 构建后统一 `strat.eod_close_max_dte = cfg.eod_close_max_dte`（不必逐个子类构造函数改动）。
5. 配置：`StrategyConfig.eod_close_max_dte` + env `OBOT_EOD_CLOSE_MAX_DTE`。

**回测不受影响**：`backtest.engine` 经 `build_strategy` 复用策略，但其 `StrategyContext` 不带 `dte`（=None）→ 末日仍强平，现有 12 个用例语义不变。

## F2 设计

**落点**
1. 纯函数 `stats.realized_pnl_amount(trades, start_ts, end_ts, account, multiplier)`：复用 `pair_round_trips → filter_by_close_ts → summarize`，返回 `total_pnl_amount`。
2. `Supervisor` 新增 `repo`、`account` 注入；开仓前（`_do_open_on_start` 首行）调 `_daily_loss_blocked()`：
   - 算美东「今天」窗口 `[00:00, 次日00:00)` 的毫秒区间；
   - `repo.list_trades_in_range(account=account)`（不按 ts 过滤——配对需完整 OPEN）；
   - 算当日已实现盈亏；`pnl ≤ -|limit|` → critical 日志 + 拒绝开仓（return）。
   - 任意异常 → 放行（不因统计故障误杀开仓），仅告警。
3. 配置：`StrategyConfig.daily_loss_limit` + env `OBOT_DAILY_LOSS_LIMIT`。

**只挡开仓、不平已有仓**：已持仓可能回血，强平它与「按策略管理」冲突；kill switch 只阻断新仓。本设计的开仓路径唯一入口是 OPEN_ON_START（每次 force-recreate 重新核算），正好挡住「越亏越追」。

## 过夜持仓的已知代价（文档需标注）

- 收盘后市价单仅 RTH 可成交：若隔夜触发止损/止盈，要到次日开盘才真正成交；
- 盘后/隔夜行情可能滞后、点差大，`current_pnl_percent` 读到的盈亏可能失真；
- 隔夜跳空风险由用户承担——这是「持有多日期权」的固有取舍，`eod_close_max_dte` 让用户自选边界。

## 改动文件清单

| 文件 | 改动 |
|---|---|
| `domain/models.py` | StrategyConfig +`eod_close_max_dte=1` +`daily_loss_limit=0.0` |
| `strategy/close_strategies.py` | StrategyContext +`dte`；BaseCloseStrategy DTE 感知 + `eod_close_max_dte`；build_strategy 注入 |
| `strategy/state_machine.py` | decide_close 注入 `ctx.dte`；`_compute_dte()` |
| `persistence/stats.py` | +`realized_pnl_amount()` |
| `service.py` | Supervisor +repo/account + `_daily_loss_blocked()`；build 读两个新 env、传 repo/account |
| `tests/` | close_strategies DTE 用例；stats 当日盈亏用例 |
| `docs/deploy.md` | §18：两项配置说明 + 过夜代价告警 + 操作示例 |
| `.env.example` | 两个新 env |

## 不做（避免过度设计）

- 不改 straddle 路径（StraddleSupervisor 暂不加这两项；如需后续单列）。
- 不做「自动平掉已有仓」的 kill switch（只挡开仓）。
- 不引入 DTE 感知的回测（保持日线近似现状）。
