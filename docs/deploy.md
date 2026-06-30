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
| `TIGEROPEN_PROPS_PATH` | bot 实际读取的账户配置文件（容器内路径），如 `/app/data/tiger_openapi_config_模拟.properties`。**这是决定账户的唯一变量**；`switch-account.sh paper/live` 会改写它。不要指向 `tiger_openapi_config.properties` 符号链接（那只是人类参考） |
| `OBOT_WEB_USER` / `OBOT_WEB_PASSWORD` | **看板登录账密（必填，否则不启动）** |
| `OBOT_WEB_PORT` | 看板端口，默认 8000 |
| `OBOT_OPS_API_KEY` | 操作面 apikey；不填则操作面不启动 |
| `OBOT_OPS_PORT` / `OBOT_OPS_EXPOSE` | 操作面端口 8001 / 是否对外（默认 false=仅本机） |
| `OBOT_TP` / `OBOT_SL` | 固定止盈% / 硬止损%（硬止损所有策略强制生效） |
| `OBOT_CLOSE_BUFFER` | 收盘前 N 分钟强平（所有策略强制生效） |
| `OBOT_EOD_CLOSE_MAX_DTE` | 收盘前强平只作用于 DTE≤该值的期权（默认1）；更长期权持隔夜，见 §18 |
| `OBOT_DAILY_LOSS_LIMIT` | 当日已实现亏损达此$即停止当日开仓（默认300，0=关），见 §18 |
| `OBOT_POLL_INTERVAL` / `OBOT_MAX_QTY` / `OBOT_MAX_SPREAD` | 轮询间隔 / 单笔上限 / 最大点差% |
| **`OBOT_STRATEGY`** | 平仓策略：`threshold`/`trailing`/`breakeven`/`time_in_trade`/`bracket`（默认 threshold，见 §13） |
| `OBOT_TRAIL_ACTIVATION` / `OBOT_TRAIL_GIVEBACK` | 移动止盈：武装阈值% / 从峰值回撤点数(绝对) |
| `OBOT_TRAIL_RELATIVE_RATIO` / `OBOT_TRAIL_RELATIVE_THRESHOLD` | 相对回撤：比例%(0=关) / 启用门槛%(默认50)，见 §13 |
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

# 铁鹰盯盘（只读）：在场 condor 的标的现价离两侧短腿的距离/缓冲%/浮盈亏
# 现价走 implied_spot（平价反推）——paper 账户无美股 stock-brief 行情权限；
# 缓冲<2%、被击穿或移动止盈 armed 时自带 ⚠/● 预警；无在场铁鹰输出"跳过"。
sudo docker exec option-bot python -m option_bot.tools.watch_condor
```

> 两跳 SSH 一键盯盘（跳板机无 sshpass，密码均在本地 ProxyCommand 处理）：
> `… ssh -o ProxyCommand="sshpass -p <跳板密码> ssh -W %h:%p ubuntu@10.55.77.3" ubuntu@43.132.117.132 "sudo bash -c 'docker exec option-bot python -m option_bot.tools.watch_condor'"`。
> 已配工作日 21:42（本地 UTC+8，= 美股开盘后约 10 分钟；冬令时改 22:42）自动巡检。

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
切换 = 改写 `.env` 的 **`TIGEROPEN_PROPS_PATH`**（bot 真正读取的账户文件，权威来源）+ 同步更新 `data/tiger_openapi_config.properties` 符号链接（仅人类参考）+ **`docker compose up -d`（重建以重载 .env，不是 restart）** + 强制 `OBOT_OPEN_ON_START=false`，重启后打印 `当前账户 / is_paper` 供核对。
> 历史坑：早期 `.env` 把 `TIGEROPEN_PROPS_PATH` 指向某个固定账户文件、而切换脚本只翻符号链接，导致**切换无效、bot 一直停在原账户**。现脚本以 `TIGEROPEN_PROPS_PATH` 为准，已修复。`switch-account.sh status` 会同时打印这两者。

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
| `trailing` | 涨破 `activation%` 武装，从峰值回撤 `giveback` 点即平（移动止盈） | `OBOT_TRAIL_ACTIVATION` / `OBOT_TRAIL_GIVEBACK`（+相对回撤见下） |
| `breakeven` | 冲过 `activation%` 后回吐到 `lock%`(0=成本价) 即平（保本） | `OBOT_BREAKEVEN_ACTIVATION` / `OBOT_BREAKEVEN_LOCK` |
| `time_in_trade` | 持仓超过 `max_hold` 分钟即平（theta 兜底） | `OBOT_MAX_HOLD_MINUTES` |
| `bracket` | **可组合**：保本/移动止盈/固定止盈/时长 任选（值>0 启用） | 上述全部 |

**组件优先级（bracket / 各策略统一）**：时间强平 > 硬止损 > 保本 > 移动止盈 > 固定止盈 > 时长 > 持有。

**有状态策略（trailing/breakeven/bracket）的峰值/武装状态每 tick 持久化到状态快照，崩溃重启自动恢复。**

**相对比例回撤（trailing/bracket/straddle 共用）**：默认回撤阈值是绝对 `giveback` 点。开启
`OBOT_TRAIL_RELATIVE_RATIO`（>0，单位%）后变成**混合**——仅当**峰值盈利 ≥ `OBOT_TRAIL_RELATIVE_THRESHOLD%`**（默认 50）时，
回撤阈值取 `max(giveback, 峰值×ratio%)`；峰值未到门槛仍用绝对 `giveback`。`ratio=0` 即关闭（纯绝对，向后兼容）。
盈利越大、允许的回撤越大，避免高盈利时被小回撤过早止盈。

| 峰值 | ratio=20 / threshold=50 / giveback=10 | 平仓触发线 |
|---|---|---|
| +30%（<门槛） | max(10, —)=**10** | 跌回 +20% |
| +60% | max(10, 12)=**12** | 跌回 +48% |
| +200% | max(10, 40)=**40** | 跌回 +160% |

CLI 用法示例：
```bash
# 移动止盈：涨破 +20% 后回撤 10 点平
... run NVDA --direction LONG --expiry 2026-06-26 --strike 210 \
    --strategy trailing --trail-activation 20 --trail-giveback 10 --sl 50

# 移动止盈 + 相对回撤：峰值≥50% 时回撤阈值=max(10, 峰值×20%)，峰值+200%→跌回+160%平
... run NVDA --direction LONG --expiry 2026-06-26 --strike 210 \
    --strategy trailing --trail-activation 20 --trail-giveback 10 \
    --trail-relative-ratio 20 --trail-relative-threshold 50 --sl 50

# bracket 组合：止盈40 + 保本(冲20%回吐到5%) + 移动止盈(25%/10) + 2小时时长
... run NVDA --direction LONG --expiry 2026-06-26 --strike 210 --strategy bracket \
    --tp 40 --breakeven-activation 20 --breakeven-lock 5 \
    --trail-activation 25 --trail-giveback 10 --max-hold-minutes 120 --sl 50
```
env 等价（服务自动开仓时）：`OBOT_STRATEGY=bracket` + 上面各 `OBOT_*` 变量。关掉某组件把对应值设 0。

## 14. 看板：历史统计 + 持仓走势

> **访问**：看板绑 `:8000`（Basic auth）。生产建议经 SSH 隧道本机访问，不直接暴露公网：
> 本机执行 `ssh -L 8000:127.0.0.1:8000 <部署机>`（两跳则在第二跳上转发），再浏览器开 `http://127.0.0.1:8000`。

### 14.0 左侧策略导航 + 铁鹰状态卡片（只读）
登录后左侧有导航：**总览 / 铁鹰 condor / 跨式 straddle / 单腿 single**（活跃策略亮绿点；bot 同时只跑一种 `OBOT_MODE`）。

- **总览**：下面 §14 的全部内容（持仓/交易/历史/逐tick），向后兼容不变。
- **铁鹰 condor**：常驻状态卡片，每 3s 刷新——IV/IVP/闸门、入场信用(保守)vs 中间价、**点差缺口 → 现值 → theta 已填进度条**、浮盈亏/DTE、缓冲/击穿/armed 预警。数据源：真实在场读引擎 `status()`，否则读影子 JSON（当前为影子观察）。
- **跨式/单腿**：未运行时显示「当前未运行」，历史见「总览」。

对应只读 API（Basic 认证，异常不 500）：
```
GET /api/strategy_status     # 引擎 status() + 影子 JSON 聚合：active_mode + 各策略卡片字段
```
> 点差/theta 字段与 `python -m option_bot.tools.watch_condor`（§5）**同源**（共享 `web.strategy_status.compute_condor_view`），数值一致。现价离短腿距离为二期功能（需引擎/影子 tick 落 spot）。

### 14.1 历史统计 + 持仓走势
看板（`http://<host>:8000`，登录后）「总览」面板含两块，均**只读**：

- **历史统计（已平仓）**：选「从/到」日期(**美东时区**) → **刷新标识**(下拉按区间 groupby) → **查询**。展示总盈亏$/胜率/笔数/平均%/最大盈亏 + 累计盈亏折线 + 单笔明细。
- **持仓走势（逐tick）**：在上方下拉选**具体标识** → 查询 → 显示持仓期间每 tick 的盈亏%/现价密集曲线（指标可切换）。

对应只读 API（Basic 认证）：
```
GET /api/history?identifier=&from=&to=&account=      # 已平仓配对统计 + 累计曲线
GET /api/history/identifiers?from=&to=&account=      # 下拉用：区间内标识
GET /api/ticks?identifier=&from=&to=&account=        # 逐tick持仓走势(点多自动抽样)
```
> 逐tick 数据由监控循环每 tick 写入 `position_ticks` 表，按 `OBOT_TICK_RETENTION_DAYS`（默认7天）定期清理。无持仓/收盘时不产生新点。

**查不到记录？先看这两点（默认值/格式坑）：**
- **日期默认「近 7 天」**（`from=今天-7`、`to=今天`，美东日界）。看更早的交易要手动把「从」往前调；查空多半是日期范围没覆盖到交易当天，**不是数据丢了**。
- **标识必须精确**：合约 identifier 是 Tiger 定长格式、`SPCX` 与行权码间有**两个空格**（`SPCX  260626C00155000`）。务必从下拉框选，别手输。
- **同合约跨账户**：实盘/模拟可能在同一天有同一 identifier。下拉项已按账户区分（标「户<account>」）并随查询带上 `account`，避免两笔 tick 混进一条线；留空账户则合并所有账户。
- 排障可直接查库验证数据是否写入：`docker exec option-bot python -c "import sqlite3;print(sqlite3.connect('/app/data/option_bot.db').execute('select count(*) from position_ticks').fetchone())"`。

---

## 15. 双向跨式（straddle）多腿模式

`OBOT_MODE=straddle`：同标的/到期/**同行权价**买 **1 张 call + 1 张 put**，让市场自己筛出赢家方向。与单腿模式互斥（由 `OBOT_MODE` 路由），独立状态文件，复用看板/历史统计（按腿显示两行）。

**规则（每 tick，优先级从高到低）**：
1. **时间强平**（距收盘 ≤ `OBOT_CLOSE_BUFFER`）→ 平所有未平腿（唯一安全兜底）
2. **组合止盈** → 平所有未平腿
   - `fixed`：组合盈亏% ≥ `OBOT_STRADDLE_TP`（占总成本%）
   - `trailing`（默认，吃大波动）：组合冲过 `..._TRAIL_ACTIVATION%` 武装→记组合峰值→回撤 `..._TRAIL_GIVEBACK` 即平。同样支持 §13 相对回撤（复用 `OBOT_TRAIL_RELATIVE_RATIO/THRESHOLD`）
3. **腿止损**：某腿 ≤ −`OBOT_STRADDLE_LEG_STOP%` → 平该腿（剩余腿继续由组合层管理）
4. 持有
> **无硬止损**（长跨式不该被噪声止损打掉）；组合盈亏% =（已实现 + 未实现）/ 两腿总权利金 × 100。

**配置（env / CLI 同名见 `.env.example`）**：
| 变量 | 说明 | 默认 |
|---|---|---|
| `OBOT_MODE` | `single` / `straddle` | single |
| `OBOT_STRADDLE_LEG_STOP` | 单腿止损% | 10 |
| `OBOT_STRADDLE_TP_MODE` | `fixed` / `trailing` | trailing |
| `OBOT_STRADDLE_TP` | fixed：组合止盈%（占总成本） | 10 |
| `OBOT_STRADDLE_TRAIL_ACTIVATION` / `..._GIVEBACK` | trailing：组合武装% / 回撤% | 10 / 10 |
| 开仓 | `OBOT_SYMBOL`/`OBOT_EXPIRY`/`OBOT_STRIKE`/`OBOT_QTY`（**无需 DIRECTION**，自动 call+put） | — |

**启用示例**（`.env`，模拟盘自动建跨式）：
```ini
OBOT_MODE=straddle
OBOT_STRADDLE_TP_MODE=trailing
OBOT_STRADDLE_TRAIL_ACTIVATION=10
OBOT_STRADDLE_TRAIL_GIVEBACK=10
OBOT_STRADDLE_LEG_STOP=10
OBOT_OPEN_ON_START=true
OBOT_SYMBOL=NVDA
OBOT_EXPIRY=2026-06-26
OBOT_STRIKE=210
OBOT_QTY=1
# 实盘自动开跨式还需显式：OBOT_ALLOW_LIVE_AUTO_OPEN=true（危险）
```
改完 `.env` → `sudo docker compose up -d`；切回单腿把 `OBOT_MODE=single`。

> ⚠️ 风险：长跨式付**两份**权利金，**只在标的大幅单边波动时盈利**；横盘/小波动两腿同亏。`leg_stop=−10%` 较早触发，会很快给一条腿定生死。`switch-account.sh live` 切实盘 + 自动开仓闸均仍生效。

---

## 16. 真实操作实例（开仓 / 选策略 / 改策略 / 平仓）

> 全部在部署目录执行：`cd /root/tiger-quantitative-trade`。下面用一次真实的实盘交易（SPCX 155 Call，trailing）贯穿说明。
>
> **三条铁律**（贯穿所有操作）：
> 1. 选策略/选标的全在 `.env`，改完必须 **`docker compose up -d`** 生效（`restart` 不重载 .env，§5/§6）。
> 2. 程序**只会在容器启动那一刻**按 `OBOT_OPEN_ON_START` 决定是否开仓；**没有"远程开仓"接口**（操作面只支持平仓/急停/开关）。
> 3. 实盘自动开仓被双闸拦截：必须同时 `OBOT_OPEN_ON_START=true` **且** `OBOT_ALLOW_LIVE_AUTO_OPEN=true`，否则拒绝（防误开）。

### 16.1 开仓：实盘买 1 张 SPCX 155 Call，trailing 接管

**第 1 步——切实盘并确认账户**（强制关自动开仓，先不开）：
```bash
./switch-account.sh live yes
# 输出应包含： 当前账户: 3170246 is_paper= False
```

**第 2 步——查实时盘口，确认合约/点差/成本**（行情用当前配置即可）：
```bash
sudo docker exec option-bot python -m option_bot.cli.main chain SPCX --expiry 2026-06-26 --direction LONG
# 关注 155 行： bid/ask 越接近越好（点差小），OI 越大越流动
```

**第 3 步——在 `.env` 选标的 + 策略 + 打开双闸**：
```bash
# 标的
OBOT_MODE=single
OBOT_SYMBOL=SPCX
OBOT_DIRECTION=LONG          # LONG=买Call / SHORT=买Put
OBOT_EXPIRY=2026-06-26
OBOT_STRIKE=155
OBOT_QTY=1
OBOT_MAX_SPREAD=5            # 点差>5%拒单防滑点；流动性差可调大
# 策略：trailing（移动止盈），见 §13
OBOT_STRATEGY=trailing
OBOT_TRAIL_ACTIVATION=20     # +20% 武装
OBOT_TRAIL_GIVEBACK=10       # 从峰值回撤10个点平
OBOT_TRAIL_RELATIVE_RATIO=20 # 峰值≥50%时改用 max(10, 峰值×20%)
OBOT_TRAIL_RELATIVE_THRESHOLD=50
OBOT_SL=50                   # 硬止损-50%（兜底，强制）
OBOT_CLOSE_BUFFER=5          # 收盘前5分钟强平（强制）
# 双闸：本次确实要开仓
OBOT_OPEN_ON_START=true
OBOT_ALLOW_LIVE_AUTO_OPEN=true
```

**第 4 步——生效（触发开仓）并核对成交**：
```bash
sudo docker compose up -d
sleep 8
sudo docker compose logs --since 2m | grep -E "开仓已提交|开仓成交|拒绝|ERROR"
# 期望： 开仓成交 entry=7.9 qty=1 -> MONITORING
```

**第 5 步（关键安全收尾）——立刻把双闸改回 false（只改文件，别再重启）**：
```bash
sed -i 's/^OBOT_OPEN_ON_START=.*/OBOT_OPEN_ON_START=false/' .env
sed -i 's/^OBOT_ALLOW_LIVE_AUTO_OPEN=.*/OBOT_ALLOW_LIVE_AUTO_OPEN=false/' .env
# 不执行 up -d：运行中的容器靠 data 里的状态快照继续盯盘；
# 这样即使日后有人 up -d 重启，也不会再误开第二张仓（杜绝事故）。
```
开仓后，常驻进程按 trailing 自动盯盘并在触发时平仓，无需值守。

### 16.2 选策略 / 换策略（开仓前）

改 `OBOT_STRATEGY` + 对应参数 → `up -d` 即可。常见几种（详见 §13）：
```bash
# 固定止盈：+30% 止盈、-50% 止损
OBOT_STRATEGY=threshold ; OBOT_TP=30 ; OBOT_SL=50
# 保本止损：冲过+20%后回吐到+5%即平
OBOT_STRATEGY=breakeven ; OBOT_BREAKEVEN_ACTIVATION=20 ; OBOT_BREAKEVEN_LOCK=5
# 组合 bracket：止盈40 + 保本(20→5) + 移动止盈(25/10) + 2小时时长
OBOT_STRATEGY=bracket ; OBOT_TP=40 ; OBOT_BREAKEVEN_ACTIVATION=20 ; OBOT_BREAKEVEN_LOCK=5 ; OBOT_TRAIL_ACTIVATION=25 ; OBOT_TRAIL_GIVEBACK=10 ; OBOT_MAX_HOLD_MINUTES=120
```
```bash
sudo docker compose up -d     # 生效（若 OPEN_ON_START=false 则只是换了"下次开仓用的策略"，不会立刻开仓）
```

### 16.3 持仓中改策略参数（收紧/放宽止盈）

持仓中也能调参数，例如把回撤收紧到 5 个点锁更多利润：
```bash
sed -i 's/^OBOT_TRAIL_GIVEBACK=.*/OBOT_TRAIL_GIVEBACK=5/' .env
sudo docker compose up -d
```
- 重启后状态机从 `data/` 快照**恢复 trailing 的 armed/peak**，新参数立即按新阈值判定；收盘前强平、硬止损始终生效。
- ⚠️ **持仓中不要切换策略"种类"**（如 trailing→bracket）：快照里的运行态字段不一定对得上，易误判。要换种类请先平仓再换。
- 提醒：`OPEN_ON_START` 若是 true，这次 `up -d` 会在盘中**再开一张**。持仓中调参务必确认它是 false（按 16.1 第 5 步本就该是 false）。

### 16.4 平仓 / 急停 / 开关（操作面 apikey，运行中即时生效）

操作面在**容器内** `127.0.0.1:8001`，须在容器内调用（§5）。`KEY` = `.env` 的 `OBOT_OPS_API_KEY`：
```bash
KEY=$(grep -E '^OBOT_OPS_API_KEY=' .env | cut -d= -f2-)
ops(){ sudo docker exec option-bot python -c \
 "import urllib.request as u;print(u.urlopen(u.Request('http://127.0.0.1:8001$1',method=('POST' if '$2' else 'GET'),headers={'X-API-Key':'$KEY'})).read().decode())"; }

ops /ops/status            # 看状态（持仓/是否允许开仓）
ops /ops/close POST        # 手动市价平掉当前持仓（→ MANUAL）
ops /ops/disable-open POST # 关闭开仓闸（kill switch：只盯盘/平仓，不再开新仓）
ops /ops/enable-open POST  # 重新允许开仓
ops /ops/stop POST         # 停止盯盘线程（不平仓，仅停监控）
```
> 返回 `{"queued":"close"}` 等，命令在 ≤1 个盯盘 tick 内由 bot 线程执行。平仓也可在看板看不到按钮——看板是**只读**的，写操作只走这里。

### 16.5 收尾：切回模拟盘待命（更安全）

交易做完、不想让实盘配置一直挂着：
```bash
./switch-account.sh paper    # 切回模拟（同样 up -d 重建 + 强制 OPEN_ON_START=false）
./switch-account.sh status   # 核对当前指向
```
历史与逐tick走势已落库（`data/option_bot.db`），切账户不丢；按 §14 在看板查看（注意默认日期、按账户筛选）。

---

## 17. 期权日线回测（dolt 数据 + 复用实盘策略）

离线分析工具,在**宿主机**直接跑(容器内无 dolt 数据):用 dolt 库 `post-no-preference/options` 的每日 bid/ask,把某合约喂给**与实盘同一套平仓策略**(`build_strategy`),看"当时这么交易会怎样"。**零侵入实盘**(不动容器)。

> ⚠️ **日线近似**:`option_chain` 每合约每天 1 行,**无法复现 bot 盘中每 2 秒的 trailing 与收盘前强平**;回测用「到期/末日强平」近似收盘强平,峰值按逐日 bid 计,结论偏保守。**不含 SPCX**(该库无此新股)。设计见 `docs/design/2026-06-24-options-daily-backtest.md`。

口径:入场付 **ask**,逐日/平仓按 **bid**(多头真实成交侧);策略返回任一平仓原因即平,否则末日强平。

```bash
cd /root/tiger-quantitative-trade            # 仓库在 root，用 sudo
# 单笔(带逐日轨迹)：AMD 2026-04-17 230 Call，trailing(对齐实盘相对回撤)
sudo HOME=/root python3 -m option_bot.backtest \
  --symbol AMD --expiration 2026-04-17 --strike 230 --put-call Call \
  --from 2026-02-10 --to 2026-04-17 \
  --strategy trailing --trail-activation 20 --trail-giveback 10 \
  --trail-relative-ratio 20 --trail-relative-threshold 50 --sl 50 --verbose

# 同合约多入场批量(出胜率/均值/最大盈亏/原因分布)
sudo HOME=/root python3 -m option_bot.backtest \
  --symbol AMD --expiration 2026-04-17 --strike 230 --put-call Call \
  --from 2026-02-10 --to 2026-04-17 --strategy trailing --batch-entries
```

参数与 `.env`/`run` 同义:`--strategy threshold|trailing|breakeven|time_in_trade|bracket`、`--tp/--sl`、`--trail-*`、`--trail-relative-*`、`--breakeven-*`、`--max-hold-minutes`;`--fill ask|mid`、`--entry-date`、`--json`。

先找历史较长的合约:
```bash
cd /data1/dolt/options && HOME=/root dolt sql -q \
 "select expiration,strike,count(*) n,min(date) f,max(date) l from option_chain \
  where act_symbol='AMD' and call_put='Call' and date between '2026-01-02' and '2026-06-23' \
  group by expiration,strike order by n desc limit 5"
```

### 17.1 滚动 ATM 批量回测（衡量策略本身）

每个交易日按**当日现价**选近月平值合约入场（联用 `stocks` 现价 + `options` 链），跑同一策略到退出，汇总全样本胜率/盈亏。需要 `/data1/dolt/stocks` 与 `/data1/dolt/options` 都在。

```bash
cd /root/tiger-quantitative-trade
sudo HOME=/root python3 -m option_bot.backtest --symbol AMD --put-call Call \
  --from 2026-01-15 --to 2026-05-15 --rolling-atm --target-dte 30 \
  --strategy trailing --trail-activation 20 --trail-giveback 10 \
  --trail-relative-ratio 20 --trail-relative-threshold 50 --sl 50
```
选合约：DTE 最接近 `--target-dte`(默认30，`--min-dte` 默认3) 的到期；该到期下 `|行权价−现价|` 最小者为 ATM。`--step-days` 控入场节奏，`--put-call Put` 测看跌，`--json` 出明细。

> ⚠️ 解读注意：① 仍是**日线近似**（盘中 trailing/强平不可复现）；② 「每个交易日都买 ATM」是激进取样，且单一标的若处于大牛/大熊单边行情，均值会被严重带偏（非策略普适表现）；③ **不含 SPCX**（无期权）。回测结论仅供参考，不等于实盘。

## 18. DTE 区分收盘强平 + 当日亏损上限（风控）

两项实盘风控（设计 `docs/design/2026-06-26-dte-aware-eod-and-daily-loss-limit.md`），缘起 06-25 的两类亏损：7DTE 的 call 被收盘窗口一刀切强平、当天越亏越追。改完 `.env` 后 **`docker compose up -d --force-recreate`** 生效（§6）。

| env | 默认 | 含义 |
|---|---|---|
| `OBOT_EOD_CLOSE_MAX_DTE` | `1` | 收盘前强平**只作用于 DTE≤该值**的期权（0/1=临近到期当日平）；DTE 更大的多日期权**持有过夜**。`0`=只有当日到期(0DTE)才收盘平。 |
| `OBOT_DAILY_LOSS_LIMIT` | `300` | 当日**已实现**亏损达此美元数即**停止当日开仓**（kill switch）；`0`=关闭。 |

**DTE 强平**：DTE = 到期日 − 美东当日（到期当天=0、前一天=1）。临近收盘窗口（`OBOT_CLOSE_BUFFER`）内，仅 `DTE ≤ OBOT_EOD_CLOSE_MAX_DTE` 才 `TIME_FORCE_CLOSE`；更长期权跳过强平、但**硬止损/止盈/移动止盈仍照常生效**。DTE 解析异常时安全退化为「强平」（不把未知期限留过夜）。

> ⚠️ **持有过夜的代价**：① 收盘后市价单仅 RTH 可成交——隔夜若触发止损/止盈，要到次日开盘才真正成交；② 盘后/隔夜行情可能滞后、点差大，看到的盈亏可能失真；③ **隔夜跳空风险自负**。这是「持有多日期权」的固有取舍，用 `OBOT_EOD_CLOSE_MAX_DTE` 自选边界（怕跳空就设 `1`，要持长仓可设更大或专门用多日到期合约）。

**当日亏损上限**：开仓前（`OPEN_ON_START`/每次 `--force-recreate`）按**美东当日**窗口配对已平仓交易、汇总已实现盈亏；`≤ -OBOT_DAILY_LOSS_LIMIT` 则拒绝开仓并打 critical 日志。**只挡新仓、不平已有仓**（持仓可能回血，交给策略管理）。统计故障一律放行开仓（不因核算异常误杀）。正好挡住「同一天越亏越追」。

```bash
# 在 .env 设置（多日期权持隔夜 + 当日亏 $300 停手）
OBOT_EOD_CLOSE_MAX_DTE=1
OBOT_DAILY_LOSS_LIMIT=300
sudo docker compose up -d --force-recreate     # 生效

# 验证：当日已亏到上限再开仓会被拦，日志可见
sudo docker compose logs --since 10m option-bot | grep -i "当日已实现亏损\|stop"
```

## 19. 铁鹰卖方策略（condor）：IV 择时 + 人工确认开仓 + 自动出场

定义风险的**双垂直信用价差**（卖近月 ~16Δ 短腿、买外翼）。只在 **IV 够高**时产出开仓**提案**，
**开仓必须人工 `approve`**；止盈(+50%)/止损(−2×)/到期前(≤dte_exit，现网15)平仓**自动执行**。一次只持一仓。
设计见 `docs/design/2026-06-26-condor-premium-selling-engine.md`，策略原理见 `docs/strategy/2026-06-26-iv-timed-defined-risk-premium-selling.md`。

> ⚠️ **务必先 paper 跑通**：combo 净价/动作约定（`_OPEN_ACTION/_CLOSE_ACTION`）须在模拟盘实测确认成交方向与价格符号正确，再考虑实盘。策略 edge 是 in-sample 回测，非盈利保证。**默认用模拟账户（§16.5 `switch-account.sh paper`）。**

### 19.1 开启 condor 模式

`.env` 设 `OBOT_MODE=condor`，配置参数后 `sudo docker compose up -d --build`：

| env | 默认 | 含义 |
|---|---|---|
| `OBOT_MODE` | — | 设 `condor` 启用本模式 |
| `OBOT_CONDOR_UNDERLYING` | SPY | 标的（SPY/QQQ，流动性好） |
| `OBOT_CONDOR_TARGET_DTE` | 40 | 目标到期天数（30~45） |
| `OBOT_CONDOR_SHORT_DELTA` | 0.16 | 短腿目标 \|delta\|（~1σ 价外） |
| `OBOT_CONDOR_WING_WIDTH` | 5 | 翼宽（行权价美元间距） |
| `OBOT_CONDOR_SIDE` | both | 结构：`both`(整只铁鹰)/`call`(bear call 只卖call价差,熊市/看跌中性,无下方击穿)/`put`(bull put 只卖put价差,看涨中性)。单边权利金更薄、reward/risk 同量级；非法值回退 both。设计 `docs/design/2026-06-30-condor-single-side-spread.md` |
| `OBOT_CONDOR_MIN_IV` | 0.20 | **绝对入场闸**：ATM IV 下限（`absolute` 模式 + IV-Rank 暖机回退用；验证时可调低如 0.05 以便出提案） |
| `OBOT_CONDOR_IV_GATE_MODE` | absolute | 入场闸模式：`absolute`=IV≥min_iv(默认/今天行为) / `rank`=IVP≥阈值 / `both`=地板+IVP。设计 `docs/design/2026-06-29-condor-iv-rank-entry-gate.md` |
| `OBOT_CONDOR_MIN_IV_RANK` | 50 | IV 分位入场阈值(0–100)，`rank`/`both` 用（"今天 IV 比过去一年多大比例的日子高"） |
| `OBOT_CONDOR_IV_RANK_FLOOR` | 0 | `both` 的绝对地板(IV小数,如 0.12 防绝对过低)；0=无地板(both 退化为 rank) |
| `OBOT_CONDOR_IV_RANK_LOOKBACK` | 252 | IV 历史滚动窗口(交易日,≈1年) |
| `OBOT_CONDOR_IV_RANK_MIN_HISTORY` | 60 | 暖机：历史不足此数则**回退 absolute**(用 min_iv,安全)；自采约需 3 个月 |
| `OBOT_CONDOR_IV_RANK_SEED_FROM_VIX` | false | 用 VIX(close−gap)回填历史加速暖机(口径近似,会被真样本老化顶替)；需 `<data>/VIX_History.csv` |
| `OBOT_CONDOR_IV_RANK_VIX_GAP` | 4 | 种子用：VIX 高于 ATM IV 的点数(偏斜溢价) |
| `OBOT_CONDOR_IV_HISTORY_FILE` | (空) | IV 历史文件；空=引擎从 data 目录派生 `iv_history_<symbol>.json`（影子已设为同一文件） |
| `OBOT_CONDOR_PROFIT_TARGET` | 0.5 | 止盈：吃满 50% 权利金平（threshold 策略的 tp，=tp_percent/100） |
| `OBOT_CONDOR_STOP_MULT` | 2.0 | 止损：亏达 2× 权利金平（硬止损 sl，所有策略强制生效，=sl_percent/100） |
| `OBOT_CONDOR_DTE_EXIT` | 21（**现网=15**）| 到期前 N 天平（避 gamma；force_close_dte，最低优先级，盈利/止损先判）。**代码默认 21，现网 2026-06-30 调为 15**——持仓时长回测显示 DTE15（持~25天）为甜点（均值/胜率/profit_factor 均优、回撤持平），见 `docs/backtest/2026-06-30-condor-holding-period.md` |
| `OBOT_CONDOR_CLOSE_STRATEGY` | threshold | 可插拔平仓策略：`threshold`=固定止盈(=默认/今天行为)；`trailing`=移动止盈/回撤保护。复用 `close_strategies`。设计 `docs/design/2026-06-27-condor-pluggable-close-strategy.md` |
| `OBOT_CONDOR_TRAIL_ACTIVATION` | 0 | trailing 武装阈值（**占权利金%**，如 30=盈利达 30% 权利金才武装）；仅 `CLOSE_STRATEGY=trailing` 时用 |
| `OBOT_CONDOR_TRAIL_GIVEBACK` | 0 | trailing 从峰值回撤多少（**占权利金%**，如 15）即锁盈平仓 |
| `OBOT_CONDOR_MAX_LOSS_PCT` | 0.05 | 单仓最大亏损占账户比例（定张数） |
| `OBOT_CONDOR_ACCOUNT_EQUITY` | 0 | 账户净值（>0 按风险定张数；0 回退 `OBOT_MAX_QTY`） |
| `OBOT_CONDOR_PROPOSAL_TTL_MIN` | 10 | 开仓提案有效期（分钟），过期或现价漂移作废重评 |
| `OBOT_CONDOR_SYNTHETIC_GREEKS` | true | 券商无逐档 delta 时按 BS 自算（平价反推现价+briefs平值IV/利率）；false=只用券商 greeks |
| `OBOT_CONDOR_RISK_FREE` | 0 | 合成 delta 用无风险利率；0=用 briefs `rates_bonds`，>0 覆盖 |
| `OBOT_CONDOR_IV_SOURCE` | computed | 入场闸/合成 delta 的 IV 来源：`computed`=从近 ATM 期权 mid BS 反推的活 IV（逐 tick，反推失败回退 briefs）；`briefs`=旧 `volatility` 字段（陈旧标的平值，对照/兜底）。设计 `docs/design/2026-06-27-condor-live-iv-signal.md` |
| `OBOT_CONDOR_OPEN_COMBO_TYPE` | CUSTOM | 开仓单类型：`CUSTOM`=单笔 4 腿原子单（避免两垂直间半成交）；`VERTICAL`=两个垂直（回退） |

> condor 模式**不使用** `OBOT_OPEN_ON_START`/`OBOT_DIRECTION/SYMBOL/...`——开仓只走"提案 + 人工 approve"。
> **合成 greeks**：本项目部署的 HK paper 账户行情不返回逐档 delta（chain.delta 全 0、briefs 只给标的平值 IV），
> 默认开启 BS 合成兜底才能出提案；详见 `docs/design/2026-06-26-condor-synthetic-greeks-fallback.md`。

### 19.2 生命周期与日志

`IDLE → PROPOSED → MONITORING → CLOSED`。只在**美股常规盘(RTH)**评估提案（每 60s 一次，避 `market_state` 限流）。
出提案时日志打 `WARNING`，形如：
```
★ 铁鹰开仓提案（待人工 approve）: SPY 20260807 DTE42 现价612.3 | 净权利金/股≈1.35 最大亏损/股≈3.65 张数1 | 腿: BUYP590 SELLP595 SELLC630 BUYC635
```
看实时：`sudo docker compose logs -f option-bot | grep -E '铁鹰|提案|condor'`。

### 19.3 人工确认开仓（approve / reject）

复用 §16.4 的 `ops()` helper（容器内 `127.0.0.1:8001`，带 `OBOT_OPS_API_KEY`）：
```bash
ops /ops/status            # 看当前状态/是否有待批提案（status.proposal）
ops /ops/approve POST      # ★ 批准 → 提交两个垂直 combo 净限价单 → MONITORING
ops /ops/reject  POST      # 拒绝当前提案 → 回 IDLE（下轮重评）
```
- 批准后引擎按 combo **净限价整笔成交**（不逐腿，保证定义风险）；未在 `fill_timeout` 内成交会提示人工核对挂单。
- 提案过 `PROPOSAL_TTL_MIN` 分钟自动作废重评，`approve` 过期提案返回"已作废"。

### 19.4 自动出场与手动平仓

- **自动**：每 tick 算当前平仓成本 → 止盈(+50%)/止损(−2×)/到期前(≤dte_exit，现网15) 命中即**自动平仓** → CLOSED。日志 `铁鹰触发出场 reason=...`。平仓用**翻转每条腿 BUY/SELL 的反向 combo**（CUSTOM 单笔或 VERTICAL 两单，与开仓单类型一致），不依赖"组合 action 翻转"。
- **手动**：`ops /ops/close POST` 平掉当前持仓。`ops /ops/stop POST` 停盯盘线程（不平仓）。
- **半成交回滚**：approve 时若开仓单未在 `fill_timeout` 内成交，**自动撤单**；若有腿已成交则**逐腿反向市价拉平**，回到 IDLE，绝不留孤儿仓（日志 `撤单/回滚`）。
- **恢复对账**：重启 `resume()` 会逐腿与券商持仓核对方向，不一致则进 `ERROR` 态**待人工核对、不自动出场**（日志 `恢复对账失败...ERROR`）。

### 19.5 combo 下单语义（2026-06-26 paper 已验证 ✓）

开仓语义已实测确认：`combo_type` 正确、四腿方向正确（BUY 翼 / SELL 体）、`action='BUY'` + **净价为负=收款（信用）**，成交 `avg_fill<0`=收到权利金。验证回单：
```bash
sudo docker exec -i option-bot python - <<'PY'
import os
from tigeropen.trade.trade_client import TradeClient
from option_bot.config.loader import load_client_config_from_env
cfg=load_client_config_from_env(props_path=os.environ['TIGEROPEN_PROPS_PATH']); tc=TradeClient(cfg)
for o in tc.get_orders(account=cfg.account)[:4]:
    print(getattr(o,'combo_type',None), getattr(o,'limit_price',None),
          getattr(o,'status',None), getattr(o,'contract_legs',None))
PY
```
**仍待 paper 验证（实现已就绪）**：① CUSTOM 4 腿是否原子成交、净价符号；② 翻转腿平仓后持仓**归 0**（非 2×）；③ 未成交→自动撤单+回滚无残仓。验证计划见 `docs/design/2026-06-26-condor-combo-robustness.md` §4。

### 19.6 限制（Phase 1）

只支持**单仓**、固定结构、止盈/止损/到期平仓。**被突破滚动、回撤分级降挡、并发多仓、真 IV-Rank 为 Phase 2 未实现**。实盘前务必先模拟盘验证 combo 语义并小仓试跑。

## 20. 影子追踪器（shadow）：纯观察验证盈利模式，零下单

`option_bot/shadow.py`。等引擎入场条件满足（RTH + IV≥`OBOT_CONDOR_MIN_IV`）时**锁定**当时的铁鹰结构，之后定时盯市记录盈亏走势、按设计出场规则（+50%止盈/−2×止损/≤21DTE）判定，**只读市场数据、绝不下单/不建 TradeClient**。用于确认"该机会按设计是否如预测获利"。状态机 `WAITING→TRACKING→CLOSED`，先跟完第一条机会。

```bash
# 容器内
docker exec option-bot python -m option_bot.shadow sample   # 推进一步（cron 每 10 分钟跑）
docker exec option-bot python -m option_bot.shadow report   # 看锁定结构 + 走势 + 结果
docker exec option-bot python -m option_bot.shadow reset    # 清空重来
```
- 状态文件 `/app/data/shadow_condor.json`（OBOT_SHADOW_FILE 覆盖），跨重建保留。
- cron（root，每 10 分钟）：`*/10 * * * * /usr/bin/docker exec option-bot python -m option_bot.shadow sample >> /var/log/condor_shadow.log 2>&1`。off-hours/IV 不够时自动空转（不锁定）。
- 盈亏正=权利金衰减获利（符合卖方盈利模式）；`report` 的 OUTCOME 显示该机会最终走到止盈/止损/到期。
- 影子与真实交易**完全解耦**：不影响引擎、不下单；可与 paper 引擎同时跑。
