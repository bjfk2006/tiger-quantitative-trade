# 设计：期权日线回测工具（复用可插拔平仓策略）

- 日期：2026-06-24
- 范围（用户已选）：**B —— 用 `post-no-preference/options` 的 `option_chain` 每日 bid/ask 做开/平仓回测，复用 bot 现有 `close_strategies`**。日线粒度；不含 SPCX（该库无 SPCX）。
- 目标：给定一个期权合约 + 平仓策略，回放其逐日真实买卖价，得到「若当时这么交易，结果如何」，输出单笔/批量统计。**最大价值 = 复用与实盘一模一样的策略代码**（`build_strategy` + `StrategyContext`），让回测与实盘判仓逻辑同源。

## 1. 关键约束 / 诚实声明
- **日线粒度**：`option_chain` 每个合约每天仅 1 行（PK 含 date）。**无法复现 bot 盘中每 2 秒的 trailing 与"收盘前 5 分钟强平"**。回测是「日级近似」，结论偏保守（盘中峰值/谷值不可见，trailing 触发会被低估）。文档与输出都会标注此点，不夸大。
- **复用而非重写策略**：回测只喂 `StrategyContext(pnl_percent=...)` 给 `build_strategy(name, cfg).decide(ctx)`，与实盘同一套 `close_strategies.py`。
- **运行位置 = 宿主机**：dolt 数据在 host `/data1/dolt/options`，dolt 命令在 host；bot 容器既无 dolt 也没挂 `/data1`。故回测做成 **host 上可跑的独立入口**，只 import 纯模块（`domain.models` / `strategy.close_strategies`，均不依赖 tigeropen），不走 `cli/main.py`（那条会 import SDK）。

## 2. 数据访问
- `dolt_source.load_option_series(repo, symbol, expiry, strike, put_call, from_date, to_date)`：
  - 通过 `subprocess` 执行 `dolt sql -q "<SQL>" --result-format json`，`cwd=repo`（默认 `/data1/dolt/options`），`env HOME=/root`。
  - SQL：`select date,bid,ask from option_chain where act_symbol=? and expiration=? and strike=? and call_put=? and date between ? and ? order by date`（date 为 PK 首列，按区间走索引）。
  - 解析 JSON `rows` → `[{date, bid, ask}]`（Decimal→float，缺失值跳过）。

## 3. 回测语义（单合约）
- **入场**：在 `entry_date`（或区间内首个有数据日）以当日 **ask** 买入（与实盘市价单偏保守一致）。`entry_price = ask`。
- **逐日盯盘**：第 d 日，多头平仓应以 **bid** 卖出 → `pnl% = (bid_d − entry_ask)/entry_ask × 100`。构造 `StrategyContext(pnl_percent=pnl, minutes_to_close=None, opened_at=entry_ms, now_ts=d_ms)` 喂给策略。
  - `now_ts/opened_at` 用日期转 ms（日分辨率），使 `time_in_trade`（分钟）仍可用（按整日倍数，文档标注）。
  - `minutes_to_close=None`（日线无盘中），故"收盘前强平"在回测中**改为到期日强平**（见下），不逐日触发。
- **平仓**：`decide(ctx)` 返回任一 `CloseReason` → 当日 **bid** 平仓，记 `(entry_date, exit_date, entry_ask, exit_bid, pnl%, reason)`。
- **到期/数据末尾**：未触发则在最后一个有数据日（≤expiration）强平（`reason=TIME_FORCE_CLOSE`），价用当日 bid。
- 硬止损/止盈/trailing 等全部由策略自身按上述每日 pnl% 判定，回测引擎不内置规则（与实盘一致）。

## 4. 模块设计（新增，最小集）
```
option_bot/backtest/__init__.py
option_bot/backtest/engine.py       # 纯逻辑：run_backtest(series, cfg, strategy_name) -> BacktestResult
option_bot/backtest/dolt_source.py  # dolt 子进程取单合约日线
option_bot/backtest/__main__.py     # host 入口：参数→loader→engine→打印(表+JSON)；仅 import 纯模块
option_bot/tests/test_backtest_engine.py  # 纯逻辑单测(合成价格路径→预期出场)，不依赖 dolt
```
- `engine.run_backtest(series, cfg, strategy_name)`：`series=[{date,bid,ask}]` → 返回 `BacktestResult{entry_date, exit_date, entry, exit, pnl_percent, reason, peak_pnl, days_held}`。
- `BacktestResult` 用 dataclass；批量时返回列表 + 汇总。

## 5. 批量（小步）
- 默认**单合约单入场**（`--entry-date` 不给则用区间首日）。
- 可选 `--batch-entries`：把区间内**每个交易日**都当一次入场、跑同一策略到该合约到期，得到一组结果 → 复用 `persistence/stats.py` 的思路做胜率/均值/最大盈亏汇总（或在 engine 内聚合，避免耦合）。
- **滚动 ATM**（每日选近月平值合约）暂不做，列为后续增量（需联表 stocks 取现价定 ATM）。

## 6. CLI 形态（host 运行）
```bash
cd /root/tiger-quantitative-trade
python3 -m option_bot.backtest \
  --repo /data1/dolt/options \
  --symbol AMD --expiration 2024-12-20 --strike 150 --put-call Call \
  --from 2024-09-01 --to 2024-12-20 \
  --strategy trailing --trail-activation 20 --trail-giveback 10 \
  --trail-relative-ratio 20 --trail-relative-threshold 50 --sl 50
```
输出：逐日 pnl% 轨迹（可选 `--verbose`）+ 平仓结果行 + 一句"日线近似"提示。

## 7. 取舍与默认值
| 选择 | 默认 | 理由 |
|---|---|---|
| 入场价 | **ask**（买入付卖价） | 与实盘市价单偏保守一致；可加 `--fill mid` |
| 盯盘/平仓价 | **bid**（卖出收买价） | 多头平仓真实成交侧 |
| 时间维度 | 到期强平替代收盘前强平 | 日线无盘中 |
| 首版范围 | 单合约（+可选同合约多入场批量） | 最小可用；滚动 ATM 后续 |

## 8. 不做（避免过度设计）
- 不引入 BS 合成定价（那是方案 C）。
- 不做盘中插值/分钟级伪造。
- 不接 stocks 库（除非后续做滚动 ATM）。
- 不改动 bot 运行时/容器（纯离线 host 工具，零侵入实盘）。

## 9. 验证计划
- 纯逻辑单测：合成价格路径覆盖 trailing 触发、止损、止盈、到期强平、相对回撤。
- host 实测：对真实 AMD 某合约跑一遍，肉眼核对逐日 pnl% 与平仓点合理。

## 10. 待用户确认点
1. 入场 ask / 平仓 bid（保守）是否 OK？（默认是）
2. 首版「单合约 + 可选同合约多入场」是否够？滚动 ATM 以后再加？（默认是）
3. 确认后我按本文件实现 + 单测 + host 实测，再提交。
