# 代码评审 — option_bot 配置/状态/实盘安全（同类隐患排查）

**Date**: 2026-06-22
**Reviewer**: Claude (nuclear-fusion / reviewing-code)
**Scope**: `option_bot/**`、`switch-account.sh`、`deploy.sh`、`docker-compose.yml`、`Dockerfile`、`option_bot/.env.example`
**触发背景**: 切换实盘时 `switch-account.sh` 用 `docker compose restart`（不重载 `.env`）+ 残留 `OBOT_OPEN_ON_START=true` → 在实盘账户 `3170246` **误自动买入** 1 张 QQQ 737C（真实订单）。本次专项排查同类「配置不生效 / 状态-环境不一致 / 误触发真实下单 / 账户切换安全」隐患。
**Verdict**: 🔴 **BLOCK** —— 存在多个 Critical（可导致真实资金误下单），实盘使用前必须修复。

---

## 严重程度汇总

| ID | 级别 | 一句话 |
|---|---|---|
| C1 | 🔴 Critical | `switch-account.sh` 用 `restart`，不重载 `.env`（本次事故根因） |
| C2 | 🔴 Critical | `OPEN_ON_START` 在**每次容器启动**都会自动开仓，配合 `restart: unless-stopped` 成为「重启即下单」地雷 |
| C3 | 🔴 Critical | 自动开仓**无实盘/模拟判别**——`is_paper=False` 的实盘账户也会无额外确认直接下真实单 |
| C4 | 🔴 Critical | 切换账户脚本不强制关闭 `OPEN_ON_START`/不清理开仓参数，切到实盘即触发自动开仓 |
| M1 | 🟠 Major | `resume()` 不校验快照账户 == 当前账户，跨账户会「认领」错账户的同名合约 |
| M2 | 🟠 Major | `enable_open` kill switch 仅内存态，重启后回到 env 默认（true），开关不持久 |
| M3 | 🟠 Major | CLI `run` 与服务 supervisor 共用同一 account/db/state，无锁，双开会重复/冲突下单 |
| M4 | 🟠 Major | compose 把 8001 映射到宿主 `0.0.0.0`；一旦 `OBOT_OPS_EXPOSE=true` 即刻公网暴露操作面 |
| m1 | 🟡 Minor | `positions` 表仅以 `identifier` 为主键，跨账户同合约会互相覆盖 |
| m2 | 🟡 Minor | 账户切换不清理旧账户 `option_bot_state.json` 快照（与 M1 叠加） |

---

## Critical

### C1 — `switch-account.sh` 用 `docker compose restart`，不会重载 `.env`（事故根因）
**证据**: `switch-account.sh:18` `sudo docker compose restart >/dev/null 2>&1`
**约束来源**: Docker Compose 语义——`restart` 只重启**已存在的容器**，沿用其**创建时**载入的环境；`env_file` 只在 `up`/重新创建容器时重读（外部契约，见 `docs/deploy.md` §5「改了 .env 后 `up -d`」也印证）。
**影响**: 本次先在测试时以 `OPEN_ON_START=true + QQQ 参数` 创建了容器；随后把 `.env` 改回 `false` **未生效**（只改了文件）；`switch-account.sh` 一 `restart`，容器仍带旧 env，在切到的**实盘**账户上自动开了真实仓。
**修复**: 把 `:18` 改为 `sudo docker compose up -d`（重新创建容器→重读 `.env`）。同时建议切换后用 `up -d` 而非 `restart` 成为全局约定。

### C2 — `OPEN_ON_START` 每次启动都自动开仓 = 「重启即下单」地雷
**证据**: `service.py:99-108`（`_startup_position` → `_do_open_on_start`）、`service.py:214`（`if _b(env_get('OBOT_OPEN_ON_START'))`）；`docker-compose.yml:7` `restart: unless-stopped`
**影响**: 只要 `.env` 里 `OBOT_OPEN_ON_START=true` 持续存在，**任何**容器(重)启动都会开新仓——崩溃自动重启、宿主重启、`up -d` 都会触发。`resume()` 只在「该账户已有同合约持仓」时阻止重开（`state_machine.py:198`），一旦上一笔已平仓，再次重启就**再开一笔**。
**修复**: 改为「一次性」语义：开仓成功后由 supervisor 把开仓意图清掉（例如开仓后不再读取 open_spec，或要求外部把 `OBOT_OPEN_ON_START` 复位）。最稳妥：**移除容器内自动开仓**，开仓只走显式 CLI/受控指令；若保留，必须叠加 C3 的实盘判别。

### C3 — 自动开仓无「实盘/模拟」判别，实盘也会无确认下真实单
**证据**: `service.py:110-118`（`_do_open_on_start` 直接 `sm.open(...)`）；`state_machine.py:open()` 不检查 `is_paper`；`build_bot_from_env:201-202` 取到的 `config.is_paper` 未用于任何放行判断
**约束来源**: 设计文档反复声明「默认模拟盘、切实盘须显式确认」（`docs/design/2026-06-21-us-option-trading-bot-solution.md` §1 与 README 风险提示），代码层未落实该不变量。
**影响**: 与 C1/C4 叠加即本次事故——实盘账户被自动下单且无任何二次确认。
**修复**: 在 `_do_open_on_start`（或 `sm.open`）前加实盘闸：`if not config.is_paper and not _b(env_get('OBOT_ALLOW_LIVE_AUTO_OPEN')): 拒绝并告警`。默认实盘禁止自动开仓。

### C4 — 切换账户脚本不强制关 `OPEN_ON_START`/不清开仓参数
**证据**: `switch-account.sh:16-18`（仅改符号链接 + 重启，无任何 env 校验/清理）
**影响**: 即使修了 C1（改 `up -d`），若 `.env` 仍残留 `OPEN_ON_START=true + 标的参数`，`switch-account.sh live yes` 会在**实盘**直接自动开仓。切换动作本应是「零下单」的。
**修复**: 切换脚本在重启前**强制** `OBOT_OPEN_ON_START=false`（或检测到为 true 时拒绝并提示先复位）；切 live 额外打印当前 `OPEN_ON_START`/`enable_open` 状态供核对。与 C3 的实盘闸互为双保险。

---

## Major

### M1 — `resume()` 不校验快照账户
**证据**: `state_machine.py:182-209`，`get_option_position(pick)` 用的是**当前** adapter 账户（`:195`），但从未比较 `snap.account`（快照里有该字段，`models.py TradeSnapshot.account` / `service`/`_save:214` 会写入）。
**影响**: 切换账户后若 `data/option_bot_state.json` 残留旧账户快照，且新账户恰好持有同一合约，`resume()` 会把新账户的持仓「认领」为受管持仓并自动管理（含真实平仓）。
**修复**: `resume()` 开头加 `if snap.account != self._td.account: 丢弃快照并 return False`。

### M2 — `enable_open` kill switch 仅内存态，重启即失效
**证据**: `service.py:138`（`self._cfg.enable_open = False`）/ `:141`。`cfg` 来自 `build_bot_from_env`，重启后回到 env 默认（`StrategyConfig.enable_open=True`，`models.py`）。
**影响**: 运维 `/ops/disable-open` 关了开仓，容器一重启又自动允许开仓——kill switch 不持久，给人「已禁用」的错觉。
**修复**: 把开关落盘（写 `.env`/状态文件并在启动读取），或文档明确「重启会重置开关」并配合 C2/C3。

### M3 — CLI `run` 与服务 supervisor 无锁双管
**证据**: `cli/main.py run_cmd`（独立构造 `PositionStateMachine` + `MonitorLoop`）与 `service.Supervisor` 都对同一 `account` / `OBOT_DB_FILE` / `OBOT_STATE_FILE` 操作；无互斥。
**影响**: 在服务运行时 `docker exec ... run ...`，两个状态机各自下单/平仓，可能重复开仓、互相平掉、写乱 SQLite/快照。
**修复**: 加单实例锁（文件锁/pid 锁基于 state 文件），或文档强约束「服务运行期间不要再跑 CLI run」。

### M4 — compose 把操作面映射到宿主 `0.0.0.0`
**证据**: `docker-compose.yml:15` `- "8001:8001"`（等价宿主 `0.0.0.0:8001`）。当前安全仅因应用绑容器内 `127.0.0.1`（`web/server.py`）。
**影响**: 一旦设 `OBOT_OPS_EXPOSE=true`（应用改绑 `0.0.0.0`），这条常驻映射立刻把操作面（能平仓/开关）暴露到公网，仅靠 apikey。
**修复**: 宿主侧绑回环：`- "127.0.0.1:8001:8001"`；确需外网再显式放开 + 反代 TLS。

---

## Minor

- **m1** `db.py upsert_position ... ON CONFLICT(identifier)`：`positions` 主键仅 `identifier`，跨账户持有同一合约会互相覆盖快照行（单持仓模型下概率低，但跨账户切换时存在）。建议主键改 `(account, identifier)`。
- **m2** 账户切换不清理 `option_bot_state.json`（`switch-account.sh` 未清快照），与 M1 叠加放大风险。建议切换时一并清快照或在 M1 中以账户校验兜底。

---

## 做得好的地方（保留）

- 操作面**只入队**、由 bot 单线程执行（`service.py:120-145`），平仓幂等靠单线程 + 查 `salable_qty`（`state_machine.py`）——并发模型是干净的。
- `deploy.sh` 用的是 `up -d --build`（重读 env，正确），与 C1 的反面对照——问题只在 `switch-account.sh`。
- 操作面默认仅本机、看板无凭证、私钥只在 bot 侧——分层隔离到位。
- 看门狗 `try/except` 不让 bot 线程拖垮 web（`service.py:93-97`）。

---

## 建议修复优先级（实盘前必做）

1. **C1**：`switch-account.sh` `restart` → `up -d`。
2. **C3**：实盘自动开仓闸（`is_paper=False` 默认禁止自动开仓）。
3. **C4 + C2**：切换脚本强制 `OPEN_ON_START=false`/清开仓参数；自动开仓改一次性或移除。
4. **M1**：`resume()` 校验快照账户。
5. **M4**：8001 宿主绑 `127.0.0.1`。
6. M2 / M3 / m1 / m2：随后处理。

> 这些修复涉及 `switch-account.sh` + `service.py` + `state_machine.py` + `docker-compose.yml`（>3 文件、含安全语义），建议**切到 `building-production-feature`** 走「设计小增量 → 改 → 重建镜像 → 验证」流程，而非零散改。另：当前实盘那笔误开的 QQQ 仓位仍未处理，应优先决定其去留（与本评审独立）。
