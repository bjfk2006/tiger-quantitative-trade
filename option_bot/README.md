# option_bot — 美股期权自动交易程序

基于 [tigeropen](https://github.com/tigerfintech/openapi-python-sdk) SDK 的美股单腿期权自动交易工具：查看期权链 → 市价开仓（做多买 Call / 做空买 Put）→ 实时盯盘 → **止盈 / 止损 / 收盘前 N 分钟强平** 自动平仓。附只读 Web 看板 + 操作命令面 + SQLite 持久化，支持 Docker 一键启动。

> ⚠️ **这是真实资金交易程序，请务必先读文末「风险提示」。首期请跑模拟盘（paper account）。**

---

## 能力

- 查看美股标的的期权到期日与期权链。
- **做多 = 买入 Call，做空 = 买入 Put**（均为 BUY-to-open，风险限定在权利金；**不含裸卖期权 / 远程开仓**）。
- 市价单开仓，开仓前预检交易时段与盘口点差（滑点防护）。
- 轮询持仓未实现盈亏%，按阈值自动 SELL-to-close 平仓。
- 三类平仓触发，优先级：**时间强平（收盘前 N 分钟）> 止损 > 止盈**。
- 崩溃恢复（以券商侧持仓为准）、kill switch、连续数据失败熔断。
- 只读 Web 看板（持仓 + 交易记录）、apikey 操作命令面（平仓 / 开关）、SQLite 历史与审计。

## 架构（单进程 3 线程）

```
看板 server (:8000, 用户名/密码, 只读)
操作 server (:8001, apikey, 默认仅本机) ──入队──▶ 命令队列
bot 监督器 (盯盘循环) ──每 tick 排空命令──▶ 状态机(开/平仓) ──▶ 老虎网关
                         状态/交易 ──▶ SQLite ◀── 看板只读
```
详见 `docs/design/2026-06-21-us-option-trading-bot-solution.md` 与 `...-ui-sqlite-docker.md`。

## 快速开始

### 0. 前置
- Python ≥ 3.8；老虎开放平台 `tiger_id` + RSA 私钥 + 授权账户（**首期用模拟盘 paper account**）。
- 安装（含 Web 依赖）：`pip install -e ".[web]"`（在仓库根目录）。

### 1. 配置
复制示例并填写（**切勿提交真实 `.env` / 私钥到 git**，已被 `.gitignore` 忽略）：
```bash
cp option_bot/.env.example .env
# 填 TIGEROPEN_TIGER_ID / TIGEROPEN_ACCOUNT(paper) / TIGEROPEN_PRIVATE_KEY
# 看板必填 OBOT_WEB_USER / OBOT_WEB_PASSWORD；操作面可选 OBOT_OPS_API_KEY
```

### 2A. Docker 一键起（推荐）
```bash
mkdir -p data           # 放 SQLite / 私钥 / token / state
docker compose up -d --build
# 看板: http://<host>:8000  (输入 OBOT_WEB_USER / OBOT_WEB_PASSWORD)
```
- 看板端口 `8000` 外网可达（带登录）；操作面 `8001` **默认仅容器内本机**，需外网时设 `OBOT_OPS_EXPOSE=true`。
- 容器默认**不自动开仓**，只盯盘 / 恢复既有持仓。开仓见下方 CLI 或设 `OBOT_OPEN_ON_START=true` + 标的参数。

### 2B. 命令行（CLI）
```bash
# 看期权链（做多看 CALL）
python -m option_bot.cli.main --account <paper> --private-key key.pem \
  chain AAPL --expiry 2025-08-15 --direction LONG

# 开仓 + 自动盯盘平仓（止盈+30% / 止损-50% / 收盘前5分钟强平）
python -m option_bot.cli.main --account <paper> --private-key key.pem \
  run AAPL --direction LONG --expiry 2025-08-15 --strike 200 \
  --tp 30 --sl 50 --close-buffer 5 --qty 1 --db-file data/option_bot.db

# 只盯盘/平仓不开新仓（kill switch 模式）
python -m option_bot.cli.main --account <paper> run AAPL --direction LONG \
  --expiry 2025-08-15 --strike 200 --no-enable-open
```

### 3. 操作命令面（apikey，默认仅本机）
```bash
curl -XPOST -H "X-API-Key: $OBOT_OPS_API_KEY" http://127.0.0.1:8001/ops/close   # 手动平仓
curl -XPOST -H "X-API-Key: $OBOT_OPS_API_KEY" http://127.0.0.1:8001/ops/stop    # 停止盯盘
curl       -H "X-API-Key: $OBOT_OPS_API_KEY" http://127.0.0.1:8001/ops/status   # 状态
```

## 主要配置项（env，详见 `.env.example`）

| 类别 | 变量 |
|---|---|
| 凭证 | `TIGEROPEN_TIGER_ID` / `TIGEROPEN_ACCOUNT` / `TIGEROPEN_PRIVATE_KEY` |
| 看板(必填) | `OBOT_WEB_USER` / `OBOT_WEB_PASSWORD` / `OBOT_WEB_PORT`(8000) |
| 操作面 | `OBOT_OPS_API_KEY`（不填则不启动）/ `OBOT_OPS_PORT`(8001) / `OBOT_OPS_EXPOSE`(false) |
| 策略 | `OBOT_TP`(30) / `OBOT_SL`(50) / `OBOT_CLOSE_BUFFER`(5) / `OBOT_POLL_INTERVAL`(2) / `OBOT_MAX_QTY`(1) |
| 持久化 | `OBOT_DB_FILE` / `OBOT_STATE_FILE` |
| 自动开仓 | `OBOT_OPEN_ON_START`(false) + `OBOT_SYMBOL/DIRECTION/EXPIRY/STRIKE/QTY` |

## 测试
```bash
python -m pytest option_bot/tests -v
```

---

## ⚠️ 风险提示（务必阅读）

- **真实资金风险**：本程序会下达**真实交易订单**。请先在**模拟盘（paper account）**完整验证止盈 / 止损 / 收盘前强平三条链路，再考虑实盘，并从 `--max-qty 1` 起步。
- **市价单滑点**：期权流动性差时市价成交价可能明显偏离盘口。程序有点差预检，但**不能消除滑点**。
- **单进程盯盘的单点风险**：盯盘是本地单进程；进程崩溃 / 断网期间持仓无人自动平仓。程序有崩溃恢复（以券商持仓为准）与连续失败熔断，但**不替代人工监控**；如需更强兜底请自行评估券商侧止损单。
- **仅 RTH**：美股期权仅常规时段交易；**半日市（13:00 ET 提前收盘）SDK 不提供**，需用 `OBOT_EARLY_CLOSE_FILE` 手工维护，否则按 16:00 计算可能晚平。
- **到期日风险**：临近到期 / 深度实值期权可能涉及行权 / 被行权，收盘前强平用于降低该风险，但请知悉到期规则。
- **明文认证**：看板 Basic、操作面 apikey 均为**明文传输**。暴露公网**必须**前置 TLS 反向代理（caddy / nginx / Cloudflare Tunnel）+ 限流；生产用 `gunicorn -w 1` 置于反代后。
- **凭证安全**：API 私钥 / token / `.env` 已被 `.gitignore` 忽略，**切勿提交到 git 或放入镜像**；建议仓库设为 private。
- **免责声明**：本项目仅供学习与个人研究，作者 / 贡献者不对任何交易盈亏负责。使用即表示你自行承担全部风险。
