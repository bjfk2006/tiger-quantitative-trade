# option_bot Docker 部署与运维文档

面向后续维护的操作手册：在云服务器（Ubuntu + Docker）上部署/升级/排错 option_bot（美股期权自动交易 + 看板）。

- 代码仓库：`https://github.com/bjfk2006/tiger-quantitative-trade`
- 运行形态：**单容器单进程**（看板 server `:8000` + 操作面 server `:8001` + bot 盯盘线程），见 `docs/design/2026-06-21-option-bot-ui-sqlite-docker.md`
- 当前线上：服务器 `43.156.91.243`，部署目录 `~/tiger-quantitative-trade`（即 `/home/ubuntu/tiger-quantitative-trade`）

> ⚠️ 真实资金风险：默认使用**模拟盘**。切换实盘需显式操作（见 §6）。公网暴露看板务必做访问收口（见 §9）。

---

## 1. 前置条件

- Ubuntu 服务器，已安装 **Docker（含 compose v2）**。校验：
  ```bash
  docker --version && docker compose version
  ```
- `git`、`openssl`（生成凭证用）。
- 老虎开放平台的 **API 配置文件** `tiger_openapi_config.properties`（含 tiger_id / account / 私钥 / license），且账户已开通**美股期权行情 + 交易**权限。
- 出网正常（容器需访问老虎网关、看板需被你访问）。

---

## 2. 目录与文件结构（部署后）

```
~/tiger-quantitative-trade/
├─ docker-compose.yml          # 编排（端口、卷、env_file）
├─ Dockerfile                  # 镜像构建
├─ .env                        # 运行配置（凭证/端口/策略）★不入 git
├─ deploy.sh                   # 一键部署脚本
├─ switch-account.sh           # 模拟/实盘 账户切换
├─ option_bot/                 # 业务代码
└─ data/                       # ★持久化卷(挂载到容器 /app/data)，不入 git
   ├─ tiger_openapi_config.properties        # 符号链接 → 当前账户
   ├─ tiger_openapi_config_模拟.properties    # 模拟盘配置(含私钥)
   ├─ tiger_openapi_config_综合.properties    # 实盘配置(含私钥)
   ├─ option_bot.db            # SQLite(交易/持仓/审计)
   └─ option_bot_state.json    # 崩溃恢复快照
```

`.env`、`data/`、`*.properties`、`*.pem`、`*.db` 均被 `.gitignore` 忽略，**切勿提交**。

---

## 3. 首次部署

### 方式 A：一键脚本（推荐）

```bash
# 1) 把脚本传到服务器
scp deploy.sh ubuntu@<server>:/tmp/
ssh ubuntu@<server>
sudo bash /tmp/deploy.sh          # 首次：clone 仓库 + 生成 .env 骨架后停下
```
> 注：`deploy.sh` 默认部署到 `/root/tiger-quantitative-trade`。当前线上实例部署在 `~/`（家目录）。如需指定目录，编辑脚本顶部的 `DEST` 变量。

```bash
# 2) 上传凭证 + 编辑 .env（见 §4），然后再跑一次完成构建启动
sudo bash <部署目录>/deploy.sh
```

### 方式 B：手动（与线上实例一致，部署到家目录）

```bash
cd ~
git clone https://github.com/bjfk2006/tiger-quantitative-trade.git
cd tiger-quantitative-trade
mkdir -p data

# 上传老虎配置文件到 data/（含私钥），见 §4
# 编辑 .env，见 §4

sudo docker compose up -d --build      # 构建镜像并启动
```

启动后验证：
```bash
sudo docker compose ps                 # STATUS 应为 Up (healthy)
curl -fsS http://127.0.0.1:8000/healthz   # {"bot_alive":true,"status":"ok"}
```

---

## 4. 凭证与配置

### 4.1 上传老虎 API 配置（含私钥）

把本地 `tiger_openapi_config_*.properties` 传到服务器 `data/`，并用符号链接指向当前要用的账户：
```bash
# 本地执行（示例：上传模拟盘配置）
scp tiger_openapi_config_模拟.properties \
    "ubuntu@<server>:tiger-quantitative-trade/data/tiger_openapi_config_模拟.properties"

# 服务器执行
cd ~/tiger-quantitative-trade
chmod 600 data/tiger_openapi_config_*.properties
ln -sfn tiger_openapi_config_模拟.properties data/tiger_openapi_config.properties   # 默认模拟盘
```

### 4.2 `.env`（从 `option_bot/.env.example` 复制后编辑）

| 变量 | 说明 |
|---|---|
| `TIGEROPEN_PROPS_PATH` | 固定 `/app/data/tiger_openapi_config.properties`（指向符号链接） |
| `OBOT_WEB_USER` / `OBOT_WEB_PASSWORD` | **看板登录账密（必填，否则不启动）** |
| `OBOT_WEB_PORT` | 看板端口，默认 8000 |
| `OBOT_OPS_API_KEY` | 操作面 apikey；不填则操作面不启动 |
| `OBOT_OPS_PORT` / `OBOT_OPS_EXPOSE` | 操作面端口 8001 / 是否对外（默认 false=仅本机） |
| `OBOT_TP` / `OBOT_SL` | 固定止盈% / 硬止损%（硬止损所有策略强制生效） |
| `OBOT_CLOSE_BUFFER` | 收盘前 N 分钟强平（所有策略强制生效） |
| `OBOT_POLL_INTERVAL` / `OBOT_MAX_QTY` / `OBOT_MAX_SPREAD` | 轮询间隔 / 单笔上限 / 最大点差% |
| **`OBOT_STRATEGY`** | 平仓策略：`threshold`/`trailing`/`breakeven`/`time_in_trade`/`bracket`（默认 threshold，见 §13） |
| `OBOT_TRAIL_ACTIVATION` / `OBOT_TRAIL_GIVEBACK` | 移动止盈：武装阈值% / 从峰值回撤点数 |
| `OBOT_BREAKEVEN_ACTIVATION` / `OBOT_BREAKEVEN_LOCK` | 保本：武装阈值% / 回吐到几%平(0=成本价)；bracket 中 0=关 |
| `OBOT_MAX_HOLD_MINUTES` | 持仓时长上限(分钟)，0=关 |
| `OBOT_DB_FILE` / `OBOT_STATE_FILE` / `OBOT_TICK_RETENTION_DAYS` | SQLite / 状态快照 / 逐tick保留天数(默认7) |
| `OBOT_OPEN_ON_START` / `OBOT_ALLOW_LIVE_AUTO_OPEN` | 启动是否自动开仓（默认 false）/ 实盘自动开仓需显式 true（危险） |
| `OBOT_SYMBOL`/`OBOT_DIRECTION`/`OBOT_EXPIRY`/`OBOT_STRIKE`/`OBOT_QTY` | 自动开仓的标的参数（配合 OPEN_ON_START） |

生成强随机看板密码 / apikey：
```bash
openssl rand -hex 8     # 看板密码
openssl rand -hex 24    # ops apikey
```
改完 `.env` 后需重启生效：`sudo docker compose up -d`（见 §5）。

---

## 5. 日常运维命令（在部署目录执行）

```bash
cd ~/tiger-quantitative-trade

# 查看状态 / 健康
sudo docker compose ps
curl -fsS http://127.0.0.1:8000/healthz

# 实时日志 / 最近日志
sudo docker compose logs -f
sudo docker compose logs --tail=100

# 重启 / 停止 / 启动
sudo docker compose restart
sudo docker compose down            # 停止并移除容器(数据卷保留)
sudo docker compose up -d           # 启动(用现有镜像)

# 改了 .env 后让其生效
sudo docker compose up -d           # compose 检测到 env 变化会重建容器

# 进容器执行 CLI（例：查看期权链 / 下模拟单）
sudo docker exec -it option-bot python -m option_bot.cli.main chain AAPL --expiry 2026-06-26 --direction LONG
sudo docker exec -it option-bot python -m option_bot.cli.main \
  run NVDA --direction LONG --expiry 2026-06-24 --strike 210 \
  --qty 1 --tp 30 --sl 50 --max-spread 20 --db-file /app/data/option_bot.db --yes
```

操作面命令（apikey）。⚠️ **重要**：操作面绑定在**容器内** `127.0.0.1:8001`，从**宿主机**直接 `curl 127.0.0.1:8001` **不通**（这是安全隔离，外部/宿主机都不可达）。必须**在容器内**调用；slim 镜像无 `curl`，用 python urllib：

```bash
KEY=<OBOT_OPS_API_KEY>
# 手动平仓 (POST /ops/close)
sudo docker exec option-bot python -c \
 "import urllib.request as u;print(u.urlopen(u.Request('http://127.0.0.1:8001/ops/close',method='POST',headers={'X-API-Key':'$KEY'})).read().decode())"

# 停止盯盘 (POST /ops/stop)：把上面的 /ops/close 改成 /ops/stop
# 查状态 (GET /ops/status)：去掉 method='POST'
sudo docker exec option-bot python -c \
 "import urllib.request as u;print(u.urlopen(u.Request('http://127.0.0.1:8001/ops/status',headers={'X-API-Key':'$KEY'})).read().decode())"
```
> 若设 `OBOT_OPS_EXPOSE=true` 并经反代暴露，才可从外部带 apikey 访问；默认仅容器内。预期返回 `{"queued":"close"}` 等，命令在 ≤1 个盯盘 tick 内由 bot 线程执行。

---

## 6. 账户切换（模拟 / 实盘）

```bash
cd ~/tiger-quantitative-trade
./switch-account.sh status      # 查看当前指向
./switch-account.sh paper       # 切模拟盘（重启 + 打印账户确认）
./switch-account.sh live yes    # 切实盘综合户(真实资金)，必须带 yes 显式确认
```
切换 = 重指向 `data/tiger_openapi_config.properties` 符号链接 + `docker compose restart`，重启后打印 `当前账户 / is_paper` 供核对。

> ⚠️ 切实盘前务必确认：账户买力是否够、`OBOT_MAX_QTY` 与止损设置、`OBOT_OPEN_ON_START` 应为 false。

---

## 7. 更新升级（拉取新代码并重建）

```bash
cd ~/tiger-quantitative-trade
git pull --ff-only                 # 拉取 GitHub 最新代码
sudo docker compose up -d --build  # 重建镜像并滚动重启（.env / data 卷不受影响）

# 验证
sudo docker compose ps
curl -fsS http://127.0.0.1:8000/healthz
```
> 若本地存在未跟踪文件导致 `git pull` 冲突：先 `git stash -u` 或删除冲突的未跟踪文件再 pull。

---

## 8. 镜像导出 / 导入（跨机复用，免重建）

```bash
# 导出（在已构建镜像的机器）
sudo docker save option-bot:latest | gzip > option-bot-image.tar.gz

# 拷到另一台 amd64 机器后导入
docker load -i option-bot-image.tar.gz

# 该机器准备 .env + data/（私钥/配置）后直接起：
docker compose up -d               # compose 用现有 option-bot:latest，不会重建
```
> 镜像**不含任何密钥/配置**，复用只需另配 `.env` + `data/`。注意镜像是 **amd64**，仅能在 x86_64 机器运行。

---

## 9. 端口与安全

| 端口 | 用途 | 默认绑定 | 认证 |
|---|---|---|---|
| 8000 | 看板（只读） | `0.0.0.0`（外网可达） | 用户名/密码（Basic） |
| 8001 | 操作命令面 | 容器内 `127.0.0.1`（外网不可达） | apikey |

- 看板是**明文 HTTP**，公网暴露务必：在**云安全组**只放开 8000 给你的 IP；或用 **SSH 隧道**访问（`ssh -L 8000:127.0.0.1:8000 ubuntu@<server>` 后访问 `http://127.0.0.1:8000`，并关闭公网 8000）；需 HTTPS 则前置 caddy/nginx 反代。
- 操作面默认仅本机，保持 `OBOT_OPS_EXPOSE=false`。
- API 私钥只在 bot 侧加载；看板进程不持有凭证。
- 私钥 / `.env` / `data/` 不入 git、不入镜像。

---

## 10. 数据与备份

- 持久化全部在 `data/`（挂载卷）：SQLite（`option_bot.db`）、状态快照、配置/私钥。
- 备份：
  ```bash
  tar czf obot-data-backup-$(date +%F).tgz -C ~/tiger-quantitative-trade data
  ```
- 看交易/持仓记录（也可直接看看板）：
  ```bash
  sudo docker exec option-bot sqlite3 /app/data/option_bot.db \
    "SELECT ts,action,identifier,qty,price,reason,pnl_percent FROM trades ORDER BY ts DESC LIMIT 20;"
  ```
  （若容器无 sqlite3，可在宿主机 `sqlite3 data/option_bot.db ...`）

---

## 11. 故障排查

| 现象 | 排查 |
|---|---|
| 容器起不来/反复重启 | `sudo docker compose logs --tail=100`；常见：`.env` 缺 `OBOT_WEB_USER/PASSWORD`（会拒绝启动）、私钥路径错、配置文件名不是 `tiger_openapi_config.properties` |
| 看板能开但 bot 状态红点(`bot_alive:false`) | bot 线程异常退出；看日志定位（认证失败/网络/数据连续失败熔断） |
| `permission denied ... US ... quote market` | 账户缺对应**美股行情权限**（期权需 `usOptionQuote`，正股实时需股票 Lv1）；去老虎开通 |
| 开仓被拒「非 RTH」 | 美股非常规时段；等开盘（美东 09:30–16:00） |
| 开仓被拒「点差过大」 | 流动性差/盘前点差宽；`run` 加大 `--max-spread`，或换活跃合约 |
| `git pull` 冲突 | 未跟踪文件冲突 → `git stash -u` 后再 pull |
| 看板公网打不开 | 云安全组未放行 8000 / 容器未运行 |
| 宿主机 `curl 127.0.0.1:8001/ops/*` 无响应 | 操作面绑**容器内** loopback，宿主机/外部都不通。改在容器内调用：`sudo docker exec option-bot python -c "...urllib...8001/ops/close..."`（见 §5） |
| 容器内 `sqlite3: not found` | slim 镜像无 sqlite3 CLI；查库用看板 API `/api/trades`，或在宿主机 `sqlite3 data/option_bot.db ...` |

---

## 12. 运维速查

```bash
cd ~/tiger-quantitative-trade
sudo docker compose ps                  # 状态
sudo docker compose logs -f             # 日志
sudo docker compose restart             # 重启
git pull --ff-only && sudo docker compose up -d --build   # 升级
./switch-account.sh status              # 当前账户
curl -fsS http://127.0.0.1:8000/healthz # 健康
```

---

## 13. 平仓策略（可插拔，开仓时选）

每个策略都**强制自带**两条安全底座：**收盘前 N 分钟强平 + 硬止损**（任何策略不可绕过）；策略只定制「怎么止盈」。

| `OBOT_STRATEGY` / `--strategy` | 行为 | 主要参数 |
|---|---|---|
| `threshold`（默认） | 盈利 ≥ `tp%` 即止盈 | `OBOT_TP` |
| `trailing` | 涨破 `activation%` 武装，从峰值回撤 `giveback` 点即平（移动止盈） | `OBOT_TRAIL_ACTIVATION` / `OBOT_TRAIL_GIVEBACK` |
| `breakeven` | 冲过 `activation%` 后回吐到 `lock%`(0=成本价) 即平（保本） | `OBOT_BREAKEVEN_ACTIVATION` / `OBOT_BREAKEVEN_LOCK` |
| `time_in_trade` | 持仓超过 `max_hold` 分钟即平（theta 兜底） | `OBOT_MAX_HOLD_MINUTES` |
| `bracket` | **可组合**：保本/移动止盈/固定止盈/时长 任选（值>0 启用） | 上述全部 |

**组件优先级（bracket / 各策略统一）**：时间强平 > 硬止损 > 保本 > 移动止盈 > 固定止盈 > 时长 > 持有。

**有状态策略（trailing/breakeven/bracket）的峰值/武装状态每 tick 持久化到状态快照，崩溃重启自动恢复。**

CLI 用法示例：
```bash
# 移动止盈：涨破 +20% 后回撤 10 点平
... run NVDA --direction LONG --expiry 2026-06-26 --strike 210 \
    --strategy trailing --trail-activation 20 --trail-giveback 10 --sl 50

# bracket 组合：止盈40 + 保本(冲20%回吐到5%) + 移动止盈(25%/10) + 2小时时长
... run NVDA --direction LONG --expiry 2026-06-26 --strike 210 --strategy bracket \
    --tp 40 --breakeven-activation 20 --breakeven-lock 5 \
    --trail-activation 25 --trail-giveback 10 --max-hold-minutes 120 --sl 50
```
env 等价（服务自动开仓时）：`OBOT_STRATEGY=bracket` + 上面各 `OBOT_*` 变量。关掉某组件把对应值设 0。

## 14. 看板：历史统计 + 持仓走势

看板（`http://<host>:8000`，登录后）底部新增两块，均**只读**：

- **历史统计（已平仓）**：选「从/到」日期(**美东时区**) → **刷新标识**(下拉按区间 groupby) → **查询**。展示总盈亏$/胜率/笔数/平均%/最大盈亏 + 累计盈亏折线 + 单笔明细。
- **持仓走势（逐tick）**：在上方下拉选**具体标识** → 查询 → 显示持仓期间每 tick 的盈亏%/现价密集曲线（指标可切换）。

对应只读 API（Basic 认证）：
```
GET /api/history?identifier=&from=&to=&account=      # 已平仓配对统计 + 累计曲线
GET /api/history/identifiers?from=&to=&account=      # 下拉用：区间内标识
GET /api/ticks?identifier=&from=&to=&account=        # 逐tick持仓走势(点多自动抽样)
```
> 逐tick 数据由监控循环每 tick 写入 `position_ticks` 表，按 `OBOT_TICK_RETENTION_DAYS`（默认7天）定期清理。无持仓/收盘时不产生新点。
