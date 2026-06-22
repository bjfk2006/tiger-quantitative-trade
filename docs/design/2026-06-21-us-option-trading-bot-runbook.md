# Runbook — 美股期权自动交易程序 option_bot

**配套设计**: `docs/design/2026-06-21-us-option-trading-bot-solution.md`
**代码**: `option_bot/`（独立包，未改动 tigeropen SDK 源码）
**状态**: 代码 + 单测已写；**本地未执行任何 build/test/install**（按 building-production-feature 硬约束）。以下命令需验证者在配好环境的机器/CI 上执行。

> ⚠️ 真实资金风险：首期**务必跑模拟盘（paper account）**。实盘前需完整跑通模拟盘三条链路（止盈/止损/收盘前强平）。

---

## 1. 依赖准备

- **无新增第三方依赖**。option_bot 仅用 tigeropen 已声明的依赖：`click>=8.0`、`pandas`、`pytz`（均在 `pyproject.toml` 中）+ 标准库。
- 安装（在仓库根目录）：
  ```bash
  pip install -e .          # 安装 tigeropen（含 click/pandas/pytz）
  # option_bot 为根目录下的本地包，随仓库一起 import；无需额外安装
  ```
- 校验 import 链（验证者执行）：
  ```bash
  python -c "import option_bot, click, pandas, pytz; print('deps ok')"
  ```

## 2. 数据库 migration

`N/A` —— 无数据库。仅一个本地 JSON 状态快照文件 `option_bot_state.json`（崩溃恢复用），由程序自动创建/清理，无 schema。

## 3. 配置变更

- **凭证**（复用 SDK 三级加载：参数 > 环境变量 > 配置文件）：
  - 方式一 环境变量：`TIGEROPEN_TIGER_ID` / `TIGEROPEN_ACCOUNT`(paper account) / `TIGEROPEN_PRIVATE_KEY`(内容或文件路径)。
  - 方式二 CLI 参数：`--tiger-id --account --private-key --props-path`。
  - 方式三 `tiger_openapi_config.properties` 文件（`--props-path` 指向）。
- **新增本程序配置项**（CLI 参数，均有默认值）：
  | 参数 | 默认 | 含义 |
  |---|---|---|
  | `--direction` | 必填 | LONG=买Call / SHORT=买Put |
  | `--expiry` `--strike` | 必填 | 到期日 / 行权价 |
  | `--qty` `--max-qty` | 1 / 1 | 数量 / 单笔上限 |
  | `--tp` `--sl` | 30 / 50 | 止盈+% / 止损-% |
  | `--close-buffer` | 5 | 收盘前 N 分钟强平 |
  | `--enable-open/--no-enable-open` | 开 | 是否允许开新仓（kill switch；关闭则只盯盘/平仓） |
  | `--poll-interval` | 2.0 | 监控轮询间隔(秒) |
  | `--early-close-file` | 无 | 半日市日期表 JSON `{"2025-11-28":"13:00"}` |
  | `--state-file` | option_bot_state.json | 快照路径 |
- **secret**：私钥仅经 SDK 加载，**不入日志、不入快照**（设计 §8/§9）。

## 4. 构建命令

`N/A` —— 纯 Python，无编译/打包产物。如需纳入发行包，确认 `pyproject.toml` 的 `packages.find` 是否要纳入 `option_bot`（当前仅 `exclude=["tests","tests.*"]`，会自动发现 `option_bot`）。

## 5. 静态检查

```bash
python -m pyflakes option_bot            # 期望 0 错误（未用导入已清理）
python -m py_compile $(find option_bot -name '*.py')   # 语法检查
```

## 6. 单元测试

测试不触达 SDK/网络（适配层全部 mock，时钟注入），可离线运行：

```bash
python -m pytest option_bot/tests -v
# 或：python -m unittest discover -s option_bot/tests -p 'test_*.py'
```

覆盖的关键用例（按 task plan）：
- `test_risk_guard.py`：评估优先级（时间强平 > 止损 > 止盈 > 持有）、`minutes_to_close=None` 不触发时间强平、开仓预检（RTH/点差/开关）、pnl% 计算。
- `test_market_clock.py`：收盘前分钟数、收盘后/非交易日返回 None、半日市 13:00、日历缓存。
- `test_state_machine.py`：开仓 happy path、非 RTH 拒单、超额拒单、方向不匹配；平仓 happy、**无持仓幂等收尾**、已 CLOSED 空操作；pnl% 取持仓字段 + 降级估算；以及三条回归用例 —— **#1 未确认成交不丢仓**、**#2 时间强平绕过 auto-close 开关**、**#3 部分成交进入监控**。

预期：全部通过。**本地未执行**（硬约束）；待验证者在 CI/本地跑。

## 7. 集成测试（模拟盘，手工）

需真实模拟盘凭证，**不在 CI 自动跑**。验证者在 paper account 执行下列三条链路：

1. **止盈链路**：选一支流动性好的近月期权，`--tp` 设小（如 2），`run` 开仓 → 价格上行触发 → 观察自动 SELL 成交、状态 CLOSED、快照清除。
2. **止损链路**：`--sl` 设小（如 2）→ 价格下行触发 → 自动平仓。
3. **收盘前强平**：临近收盘时启动并持仓，`--close-buffer 5` → 16:00 前 5 分钟无条件平仓（优先于盈亏阈值）。半日市需用 `--early-close-file` 提供 13:00。

## 8. 手工验证清单（staging / paper）

- [ ] `chain AAPL` 列出到期日；`chain AAPL --expiry 2025-08-15 --direction LONG` 列出 CALL 链。
- [ ] `run AAPL --direction LONG --expiry ... --strike ...` 下单前有确认提示；`--yes` 可跳过。
- [ ] 开仓后日志出现 `开仓成交 entry=... -> MONITORING`，状态快照文件生成。
- [ ] 盈亏%日志随轮询刷新；临近收盘轮询间隔收紧。
- [ ] 触发平仓后日志 `触发平仓 reason=...`、`平仓成交 ... -> CLOSED`，快照被清除。
- [ ] **边界**：非交易时段 `run` 应被预检拒绝（提示非 RTH）。
- [ ] **边界**：点差过大的冷门期权应被拒绝（提示滑点风险）。
- [ ] **恢复**：开仓后 kill 进程再 `run`，应 `resume()` 直接接管已有持仓而非重复开仓。
- [ ] **回归**：断开网络制造数据失败，连续达 `max_data_failures` 后触发 kill switch（状态 ERROR + critical 日志），不误平。

## 9. CI gates

- 应触发：`pyflakes` + `py_compile` + `pytest option_bot/tests`。
- 通过判定：静态检查 0 错误，单测全绿。
- 集成测试（§7）**不进 CI**（需真实凭证 + 真实市场时段），由人工在 paper 执行并记录。

## 10. 回滚策略

- option_bot 为**独立新增包**，不改 SDK；回滚 = 删除/忽略 `option_bot/` 与 `docs/design/2026-06-21-*` 即可，对 tigeropen 零影响。
- 运行时急停：`Ctrl-C`/SIGTERM 优雅停机（**不自动平仓**，提示人工确认现有持仓）；或用 `--no-enable-open` 启动为「只盯盘/平仓、不开新仓」模式（kill switch；无遗留持仓时直接退出）。
- 若某次发布引入回归：`git revert` 对应 commit 范围；状态快照文件可直接删除（下次以远端持仓为准重建）。

---

## 已知残余风险（需运营知晓）

- **R5 半日市**：SDK 不提供半日市收盘时刻，依赖 `--early-close-file` 手工维护；漏配会按 16:00 计算 → 半日市当天可能晚平。
- **市价单滑点**：已加点差预检，但成交仍可能偏离盘口（设计 §10 R1）。
- **进程级单点**：本地进程是唯一盯盘者；§11 待定项 1 的「券商侧 STP 兜底」未实现（首期靠本地 + 崩溃恢复）。
- **强平用市价**：收盘前流动性差时滑点更大（§11 待定项 7，默认市价）。
