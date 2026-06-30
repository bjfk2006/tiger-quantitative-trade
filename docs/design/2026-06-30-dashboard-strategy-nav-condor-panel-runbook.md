# Runbook：看板策略导航 + 铁鹰状态面板（一期 MVP）

配套设计 `docs/design/2026-06-30-dashboard-strategy-nav-condor-panel.md`。本机仅做纯函数单测 + 静态检查；
**应用启动 / API / 浏览器验证在 HK 容器执行**（本机无 flask/SDK/凭证，跑了是假信号）。

## 1. 依赖准备
- **无新依赖**：仅用现有 flask/pytz/标准库。`pyproject.toml` 未改，`option_bot.web.tools` 子包随源码打进镜像。
- 安装命令：无（重建镜像即带入）。

## 2. 数据库 migration
- **无**。一期不动 schema，不读写 DB 新表（`/api/strategy_status` 只读 `status_provider()` + 影子 JSON 文件）。

## 3. 配置变更
- **无新增必填项**。沿用既有 `OBOT_SHADOW_FILE`（默认 `/app/data/shadow_condor.json`）；`web/dashboard.py:_load_shadow` 读它，缺失/损坏返回 None 不报错。

## 4. 构建命令（HK 宿主机）
```bash
cd /root/tiger-quantitative-trade
git fetch origin main && git reset --hard origin/main
docker compose up -d --build          # 重建镜像、重启 option-bot（data/ 挂载状态不丢）
docker ps --filter name=option-bot --format '{{.Status}}'   # 期望 healthy
```
> 重建前确认 `OBOT_OPEN_ON_START=false`、`OBOT_ALLOW_LIVE_AUTO_OPEN=false`（防重启误开仓）。

## 5. 静态检查（已在本机完成）
- `python -m py_compile` 四个改动/新增 .py → 通过。
- HTML 标签平衡：section 4/4、main 1/1、nav 1/1；4 panel ↔ 4 nav-item。
- 无 console.log/TODO/debugger 残留。

## 6. 单元测试（纯函数，已在本机跑过；CI 应复跑）
```bash
python -m unittest option_bot.tests.test_strategy_status      # 14 passed（本机已验证）
python -m unittest option_bot.tests.test_condor option_bot.tests.test_iv_history   # 90 passed（回归）
```
- 全量：`python -m unittest discover option_bot/tests`（CI gate）。

## 7. 集成 / API 验证（HK 容器内）
```bash
# 容器内取策略状态（看板进程 = bot 同进程，status_provider 可用）
KEY=<OBOT_WEB_USER>:<OBOT_WEB_PASSWORD>
docker exec option-bot python -c "import urllib.request as u,base64,os; \
 a=base64.b64encode(b'$KEY').decode(); \
 print(u.urlopen(u.Request('http://127.0.0.1:8000/api/strategy_status',headers={'Authorization':'Basic '+a})).read().decode())"
```
期望 JSON：`active_mode=condor`，`strategies.condor.source=shadow`、`live=true`、含 `gap0_pct/pnl_pct/theta_filled_pt`。
- 对照 `docker exec option-bot python -m option_bot.tools.watch_condor` 的点差行，数值应一致（同源 compute_condor_view）。

## 8. 手工验证清单（浏览器，经 SSH 隧道访问 :8000）
- [ ] 打开看板默认显示「总览」——原四块（持仓/交易/历史/逐tick）**原样可用**（regression）。
- [ ] 左侧出现 4 个导航项；「铁鹰 condor」旁绿点（active）。
- [ ] 点「铁鹰」→ 显示卡片：IV/IVP/闸门、信用 收1.09/中间价1.18、点差缺口→现值→theta已填、进度条。
- [ ] 进度条宽度 ≈ (pnl−gap0)/(0−gap0)；pnl 转正时满格。
- [ ] 点「跨式/单腿」→ 显示「当前未运行」（灰点）。
- [ ] 卡片每 3s 随轮询刷新；切换面板不刷新整页。
- [ ] 影子平仓后（outcome!=null）卡片显示「已平仓 + 原因」，不再画进度条。

## 9. CI gates
- lint/syntax + `unittest discover` 全绿；无新依赖故无 lockfile 变更。

## 10. 回滚策略
- 纯展示、只读、无 DB/schema/配置变更 → 回滚零副作用。
- `git revert <commit>` 后 `docker compose up -d --build`；或 `git reset --hard <prev>` 重建。
- 影子/引擎数据不受影响（本特性不写任何状态）。
