# Runbook — option_bot 看板 + SQLite + Docker 增量

**配套设计**: `docs/design/2026-06-21-option-bot-ui-sqlite-docker.md`
**状态**: 代码 + 单测已写；**本地未执行 build/test/镜像构建**（building-production-feature 硬约束）。下列命令需验证者在配好环境/Docker 的机器执行。

> ⚠️ 真实资金：首期跑**模拟盘**。看板可暴露公网（带登录），**操作面默认仅本机**且能力限于平仓+开关（无远程开仓）。**明文 Basic/apikey，公网必须前置 TLS 反代**。

---

## 1. 依赖准备

- 唯一新增运行时依赖：**Flask**，已声明为可选 extra `web`（`pyproject.toml [project.optional-dependencies] web`）。SDK 默认安装不含它。
- 本地（非 Docker）跑服务：
  ```bash
  pip install -e ".[web]"     # 装 tigeropen + flask
  ```
- 校验：
  ```bash
  python -c "import option_bot.web.server, flask, sqlite3; print('web deps ok')"
  ```

## 2. 数据库 migration

`N/A`（无迁移工具）。SQLite 三张表（`trades`/`positions`/`ops_audit`）由 `SqliteRepo.init_schema()` 启动建表（WAL 模式），幂等 `CREATE TABLE IF NOT EXISTS`。

## 3. 配置变更（env）

复制 `option_bot/.env.example` 为项目根 `.env` 并填写。关键项：

| 类别 | 变量 | 说明 |
|---|---|---|
| 凭证(SDK) | `TIGEROPEN_TIGER_ID` / `TIGEROPEN_ACCOUNT` / `TIGEROPEN_PRIVATE_KEY` | 模拟盘传 paper account；私钥可填内容或容器内路径 |
| 看板(必填) | `OBOT_WEB_USER` / `OBOT_WEB_PASSWORD` | **未设置则拒绝启动**（防无密码裸奔） |
| 看板 | `OBOT_WEB_HOST`(0.0.0.0) / `OBOT_WEB_PORT`(8000) | 外网可达 |
| 操作面 | `OBOT_OPS_API_KEY` | **不设置则操作面不启动** |
| 操作面 | `OBOT_OPS_PORT`(8001) / `OBOT_OPS_EXPOSE`(false) | false=仅容器内本机；true 才绑 0.0.0.0 |
| 策略 | `OBOT_TP`/`OBOT_SL`/`OBOT_CLOSE_BUFFER`/`OBOT_POLL_INTERVAL`/`OBOT_MAX_QTY`/`OBOT_EARLY_CLOSE_FILE` | 同 CLI |
| 持久化 | `OBOT_DB_FILE`(data/option_bot.db) / `OBOT_STATE_FILE` | 落数据卷 |
| 自动开仓 | `OBOT_OPEN_ON_START`(false) + `OBOT_SYMBOL/DIRECTION/EXPIRY/STRIKE/QTY` | **默认 false**：容器起来只盯盘/恢复，不自动下单 |

- 私钥/token 放数据卷 `./data`（compose 挂到 `/app/data`），**不要进镜像、不要提交 git**。

## 4. 构建命令

```bash
# 一键起（推荐）
docker compose up -d --build
# 或手动
docker build -t option-bot:latest .
docker run -d --env-file .env -p 8000:8000 -p 127.0.0.1:8001:8001 \
  -v "$PWD/data:/app/data" --name option-bot option-bot:latest
```
预期：镜像基于 `python:3.11-slim`，`pip install ".[web]"` 装好 tigeropen+flask。

## 5. 静态检查

```bash
python -m pyflakes option_bot
python -m py_compile $(find option_bot -name '*.py')
```

## 6. 单元测试（离线，不触网络/SDK）

```bash
python -m pytest option_bot/tests -v
```
本增量新增/相关用例：
- `test_persistence.py`：建表 + trades/positions/ops_audit 的 CRUD（临时 db）。
- `test_sink.py`：SqliteSink 的 on_open/on_position/on_close(已实现盈亏%)/on_position_closed；NullSink 空操作。
- `test_service.py`：CommandQueue put/drain/拒绝未知命令；Supervisor 排空 close→`sm.close(MANUAL)`、stop→停止、disable/enable_open→翻转标志、close 无持仓时忽略。
- `test_web.py`：看板 Basic 401/200 + 错密码 401 + `/healthz` 免认证；操作面 apikey 401/202 + `/ops/close` 入队 + ops_audit 落库 + Bearer + `/ops/status`。
- `test_state_machine.py`：新增「开仓后调用 sink.on_open」断言。

预期全绿。**本地未执行**（硬约束）。

## 7. 集成验证（手工，paper）

1. 填 `.env`（paper account + Basic 用户名密码 + 可选 apikey），`docker compose up -d --build`。
2. 浏览器开 `http://<host>:8000`，输入用户名密码 → 看到「当前持仓 / 交易记录」两表，顶部 bot 状态绿点，3s 自动刷新。
3. 用 CLI 在容器内开一仓（或设 `OBOT_OPEN_ON_START` + 标的参数）：
   ```bash
   docker exec -it option-bot python -m option_bot.cli.main \
     --account <paper> run AAPL --direction LONG --expiry 2025-08-15 --strike 200 \
     --db-file /app/data/option_bot.db --yes
   ```
   看板应在数秒内出现该持仓与 OPEN 记录。
4. 操作面（默认仅本机；容器内或开 EXPOSE 后）：
   ```bash
   curl -XPOST -H "X-API-Key: $OBOT_OPS_API_KEY" http://127.0.0.1:8001/ops/close
   # 预期 202 {"queued":"close"}；≤1 个 tick 后看板持仓消失、出现 CLOSE 记录
   ```

## 8. 手工验证清单

- [ ] 无凭证访问 `/api/positions` → 401；正确 Basic → 200。
- [ ] `/healthz` 免认证 200，`bot_alive` 反映后台线程存活。
- [ ] 操作面无 apikey → 401；正确 apikey `POST /ops/close` → 202 且看板随后平仓。
- [ ] 默认 `OBOT_OPS_EXPOSE=false` 时，外网访问 8001 不通；置 true 后才通。
- [ ] 未设 `OBOT_WEB_PASSWORD` 时容器**启动失败并提示**。
- [ ] 未设 `OBOT_OPS_API_KEY` 时操作面不启动（日志告警），看板正常。
- [ ] 杀掉 bot 线程模拟异常 → 看板仍可访问、红点提示 `bot down`。
- [ ] `ops_audit` 表有每次操作记录（动作/IP/key 前缀/结果）。

## 9. CI gates

- 应触发：`pyflakes` + `py_compile` + `pytest option_bot/tests`（离线，可跑）。
- 不进 CI：Docker 集成与 paper 实测（需凭证 + 市场时段），人工执行。

## 10. 回滚

- 本增量绝大多数为**新增文件**；对既有改动仅：① `state_machine` 构造新增可选 `sink`（默认 NullSink）+ open/close/tick 处加 sink 调用；② `monitor_loop` 抽 `run_once()`（`run()` 行为不变）；③ `cli` 加 `--db-file`；④ `market_data` 加 `resolve_pick`；⑤ `pyproject.toml` 加可选 extra `web`。
- 回滚 = 删除 `option_bot/{persistence,web,service.py}` + `Dockerfile`/`compose`/`.dockerignore` + 还原上述 5 处；既有 CLI 行为零变化（sink 默认 NullSink）。
- 运行时急停：`docker compose down`；或 `POST /ops/stop` 停盯盘 + `/ops/close` 平仓。

## 11. 安全硬提示（必读）

- **公网暴露前置 TLS**：看板 8000（及如开放的操作面 8001）是**明文** Basic/apikey。务必前置 `caddy`/`nginx`/Cloudflare Tunnel 做 TLS + 限流。Flask 自带 server 仅供个人/内网；生产用 `gunicorn -w 1 --threads N`（**单 worker**，避免重复起 bot 线程）置于反代后。caddy 示例（自动 HTTPS）：
  ```
  your.domain.com {
      reverse_proxy 127.0.0.1:8000
  }
  ```
- **操作面默认仅本机**；要外网用 `OBOT_OPS_EXPOSE=true` 且务必走 TLS + 强 apikey。
- **能力受限**：操作面只能平仓/开关；**apikey 泄漏也无法远程开仓动用资金**。
- **凭证隔离**：看板进程不持有 API 私钥；私钥只在 bot 线程经 SDK 加载。

## 已知语义说明（运营须知）

- **kill switch 的 `disable-open`/`enable-open`**：仅 gate「开仓路径」（CLI / `OPEN_ON_START`）。在单持仓服务**正在盯盘**时并无新开仓动作，故此开关此刻对当前持仓无效——盯盘期最有用的操作是 **`/ops/close`（平仓）** 与 **`/ops/stop`（停止盯盘）**。
- **数据失败 kill switch**：连续 `OBOT...`(max_data_failures) 次数据拉取失败 → 状态置 ERROR、停止盯盘待人工接管（不自动恢复）。
- **半日市**：SDK 不提供，需 `OBOT_EARLY_CLOSE_FILE` 维护；漏配按 16:00 ET 计算可能晚平。
