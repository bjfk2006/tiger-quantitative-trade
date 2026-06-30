# 设计：看板策略导航 + 铁鹰常驻状态面板（方案 B）

日期：2026-06-30 · 能力：building-production-feature · 状态：**设计待确认（未实现）**

> 起因：用户希望在看板（:8000）上直接看到铁鹰的 **theta 填平进度 / 点差缺口 / iv·ivp / 短腿缓冲**，
> 并在**左侧增加不同策略的导航**。当前这些只在每日定时的文本盯盘（`tools/watch_condor.py`）里有，
> 看板完全看不到。本文出方案 B（常驻状态面板）的设计，确认后再实现。

## 1. 需求（一句话）

在看板左侧加策略导航栏，点开「铁鹰」显示**常驻状态卡片**：IV/IVP/闸门、入场信用 vs 中间价、
**点差缺口→现值→theta 已填**、当前浮盈亏/DTE、（二期）现价离两侧短腿距离——兼容**影子(JSON)**与**真实引擎(status())**两种数据源。

### 范围内
- 看板布局重构为「左侧导航 + 右侧内容区」（纯 CSS flex，不引框架）。
- 新增后端聚合：把 `supervisor.status()` + 影子 JSON 合并为统一「策略状态」负载，新增只读 API。
- 铁鹰状态卡片前端渲染；复用现有持仓/逐 tick 表。
- 抽出 `watch_condor` 的派生字段计算为共享纯函数（CLI 与 web 同源，避免两份口径）。

### 范围外（明确不做）
- ❌ 不做多策略**同时运行**（架构上 bot 单 mode，见 §3.1）——导航只高亮"当前活跃"策略，其余为历史/未运行。
- ❌ 不引入前端框架（Vue/React）/ 构建链——slim 镜像无 node，维持 vanilla JS + 手绘 SVG。
- ❌ 不改下单/平仓/策略判定逻辑（纯展示，只读）。
- ❌ 不做 WebSocket 实时推送——维持现有 3s 轮询。

## 2. 现状证据（file:line）

| 事实 | 位置 |
|---|---|
| 看板已拿到 `supervisor.status`（仅喂 `/healthz` 的 bot_alive） | `web/server.py:58`、`web/dashboard.py:122` |
| 看板数据全来自 `repo`(SQLite)：positions/trades/history/ticks | `web/dashboard.py:49,58,80,103` |
| 页面为单列竖排，3s 自动刷新，无导航；vanilla JS + 手绘 SVG | `web/templates/dashboard.html`（216 行） |
| bot **单 mode**（single/straddle/condor 三选一），只建一个 supervisor | `service.py:292,306,313` |
| 铁鹰 `Manager.status()`：mode/state/symbol/expiry/qty/entry_credit/max_loss/iv/ivp/ivr/gate_mode/iv_history_days/proposal/legs；**无 mid_credit/close_cost/pnl/spot** | `strategy/condor.py:1032` |
| `CondorSupervisor.status()` = manager.status() + bot_alive + queue_size | `strategy/condor.py:1095` |
| proposal 里有 `mid_credit` | `strategy/condor.py:627` |
| 影子 JSON：`{status, entry{entry_credit,mid_credit,spot,legs,strategy_state}, trajectory[{ts,close_cost,pnl,pnl_pct_of_credit,dte}], outcome}`，由独立 cron 进程写 | `shadow.py:24,143,182` |
| 点差/theta/短腿距离派生计算已存在（CLI 用） | `tools/watch_condor.py:watch()` |

## 3. 关键设计约束与决策

### 3.1 单 mode 运行 → 导航语义
bot 进程同一时刻只跑一种策略（`OBOT_MODE`）。因此左侧导航**不是**三个并行 live 面板，而是：

- **「当前策略」**（active，由 `status()['mode']` 决定）：绿点 + 完整 live 卡片。
- **其余策略**：灰点「未运行」，点开显示**该策略的历史记录**（从 DB 按标的/类型筛选已平仓 trades），不显示 live。
- 顶部保留「总览」入口 = 现有的持仓/交易/历史/逐 tick 全量视图（向后兼容，老用户不丢功能）。

> 这样导航既满足"按策略切换"，又不假装显示未运行策略的实时数据。

### 3.2 数据源合并（影子 vs 引擎）
铁鹰 live 数据有两个来源，**互斥优先**：

| 场景 | 来源 | 判据 |
|---|---|---|
| 已真实开仓 | 引擎 `supervisor.status()` | `state==OPEN/MONITORING` 且 `qty>0` |
| 仅影子观察（当前） | 影子 `shadow_condor.json` | 引擎无在场仓 且 影子 `status==TRACKING` |
| 都没有 | 显示「等待入场」+ 最近 iv/ivp/闸门 | 引擎 status() 的 iv/ivp 仍有效 |

聚合层按上表选源，产出统一负载；前端不感知来源差异（带一个 `source: engine|shadow` 标记供显示）。

### 3.3 派生字段在哪算（避免 web 频繁拉行情）
点差缺口/theta 填平**只需 entry_credit、mid_credit、最新 close_cost**——这些影子 JSON 已有、引擎 status() 补上即可，**web 不需要调用行情**。
现价离短腿距离（二期）需要 spot：**不在 web 端每 3s 拉 `get_chain`**（会触发限流），而是**在引擎 tick / 影子 sample 时把 spot 落进 status/trajectory**，web 只读。→ 看板保持廉价只读。

## 4. 方案对比（先给推荐）

| 方案 | 内容 | 工作量 | 取舍 |
|---|---|---|---|
| **A（推荐）** | 后端聚合器 + 新 API + 前端左导航重构；派生计算抽共享纯函数 | 中（~5 文件） | 满足导航诉求、复用现有表/ticks、不引框架；改动可控 |
| B | 只在现页顶部加一张铁鹰卡片，不做导航 | 小（~3 文件） | 不满足"左侧导航"诉求 |
| C | 引入 Vue/React SPA 重写看板 | 大 | 过度设计：slim 镜像无构建链，216 行页面不值得 |

**推荐 A**，并**分两期**降低风险（见 §6）。

## 5. 详细设计（方案 A）

### 5.1 后端聚合层（新模块 `option_bot/web/strategy_status.py`）
纯函数，无 SDK、无 IO 副作用（IO 由调用方传入），便于单测：

```python
def compute_condor_view(entry: dict, last_tick: dict | None, spot: float | None = None) -> dict:
    """入场信用/中间价/最新 close_cost → 点差缺口、theta 已填、pnl；可选 spot → 短腿距离。
    复用自 tools/watch_condor 抽出的同一算法（见 5.4）。返回纯可序列化 dict。"""

def build_strategy_status(engine_status: dict, shadow_state: dict | None) -> dict:
    """按 §3.2 选源 + §3.1 标 active，产出：
    {
      'active_mode': 'condor',
      'strategies': {
        'condor': {'live': bool, 'source': 'shadow'|'engine', 'state': ...,
                   'iv':..., 'ivp':..., 'gate_mode':..., 'symbol':..., 'expiry':...,
                   'entry_credit':1.09, 'mid_credit':1.18, 'close_cost':1.17,
                   'gap0_pct':-8.3, 'pnl_pct':-7.3, 'theta_filled_pt':1.0,
                   'dte':39, 'legs':[...], 'strikes':{'put':709,'call':784},
                   'spot':744.57, 'buf_put_pct':4.78, 'buf_call_pct':5.30},  # spot 系字段二期
        'straddle': {'live': false}, 'single': {'live': false},
      }
    }"""
```

### 5.2 新 API（`web/dashboard.py`）
```python
@app.route('/api/strategy_status')
def strategy_status():
    eng = status_provider() if status_provider else {}
    shadow = _load_shadow()        # 读 OBOT_SHADOW_FILE，缺失/损坏 → None（不抛）
    return jsonify(build_strategy_status(eng, shadow))
```
- 只读、Basic auth 同现有 `_auth()`；异常返回 `{}` 不 500。
- 影子文件路径复用 `shadow.SHADOW_FILE`（`OBOT_SHADOW_FILE`，默认 `/app/data/shadow_condor.json`）。

### 5.3 引擎 status() 补字段（`strategy/condor.py:1032`）
Manager 每 tick 已算 close_cost/pnl（`condor.py:850`）——把**最近一次**存为实例字段并在 status() 暴露：
新增 `mid_credit`（开仓时记下）、`close_cost`、`pnl_percent`、`spot`（tick 已取/可由 implied_spot 得）。
→ 真实开仓后 web 同样有点差/theta/距离，无需读影子。**不改任何判定逻辑，仅记录+透出。**

### 5.4 共享派生计算（重构 `tools/watch_condor.py`）
把 `watch()` 里"点差缺口/theta/短腿距离"算式抽到 `strategy_status.compute_condor_view`，
`watch_condor.py` 改为**调用**它再格式化文本。→ CLI 与 web **同一口径**，单测覆盖一处。
（grep 确认 `watch_condor` 仅被 cron/CLI 调用，无其他引用，重构安全。）

### 5.5 前端（`web/templates/dashboard.html`）
- 布局：`body` 改为 flex —— 左 `<nav class="side">`（固定宽 ~160px）+ 右 `<main>`。
- 左导航项：`总览` / `铁鹰 condor●` / `跨式 straddle` / `单腿 single`（`●`=active 绿点，由 `/api/strategy_status.active_mode` 点亮）。
- 点击切换：纯客户端 `show(panelId)`，隐藏/显示 `<section>`，不刷新页面。
- 「总览」= 现有四块（持仓/交易/历史/逐 tick）原样保留。
- 「铁鹰」面板 = 新状态卡片：
  ```
  铁鹰 SPY · 影子观察(source) · 等待/在场 · DTE 39
  IV 15.6% | IVP 78% | 闸 both(地板0.12/IVP≥50) ✅过闸
  信用 收1.09(保守) / 中间价1.18 | 区间[709,784]
  点差缺口 -8.3% ──theta──> 现 -7.3% (已填 +1.0pt)   [进度条]
  现价≈744.6 | 距 put +4.8% / 距 call +5.3%  (二期)
  ⚠/● 预警行（缓冲<2% / armed / 击穿）
  ```
  数值取自 `/api/strategy_status`，3s 轮询刷新（复用现有 `setInterval`）。
- 「跨式/单腿」面板：`live:false` → 显示「当前未运行」+ 该类历史 trades（复用 `/api/history` 加 mode/标的过滤，或纯前端筛）。

## 6. 实施分期（降风险）
- **一期（MVP，推荐先做）**：左导航 + 铁鹰卡片的**点差/theta/iv/ivp/pnl/DTE**（**纯读 status()+影子 JSON，零新行情调用**）。文件：`strategy_status.py`(新) + `dashboard.py`(+1 route) + `dashboard.html`(布局+卡片) + `watch_condor.py`(重构复用) + 测试。
- **二期**：现价离短腿距离——需 §5.3 引擎/影子落 spot；含 `condor.py`/`shadow.py` 小改 + 二期前端字段。

## 7. 错误处理 / 边界
- 影子文件缺失/半写/JSON 损坏 → 聚合层吞掉返回 `live:false`，不影响总览。
- `active_mode != condor`（将来切回 straddle/single）→ 铁鹰面板显示「未运行」，不报错。
- 已平仓（`outcome!=null`）→ 卡片显示最终 reason + 落袋，不再画进度条。
- 除零：`entry_credit<=0` → 点差/pnl 显示「—」（复用 `condor_pnl_percent` 的 None 兜底）。

## 8. 安全 / 兼容
- 新 API 走现有 Basic auth；纯只读，无副作用、不入队、不下单。
- 看板默认 `0.0.0.0:8000` 但**仅内网/隧道**访问（现状不变）；不新增暴露面。
- 向后兼容：总览四块零改动；新内容是叠加。无 DB schema 变更（一期）。

## 9. 影响文件清单
| 文件 | 改动 | 期 |
|---|---|---|
| `option_bot/web/strategy_status.py` | **新增** 聚合 + 派生纯函数 | 一 |
| `option_bot/web/dashboard.py` | +`/api/strategy_status` 路由 | 一 |
| `option_bot/web/templates/dashboard.html` | 布局 flex + 左导航 + 铁鹰卡片 + JS | 一 |
| `option_bot/tools/watch_condor.py` | 重构：派生计算移到 strategy_status 并调用 | 一 |
| `option_bot/tests/test_strategy_status.py` | **新增** 单测（选源/点差/theta/边界） | 一 |
| `option_bot/strategy/condor.py` | status() 补 mid_credit/close_cost/pnl/spot | 二 |
| `option_bot/shadow.py` | trajectory 落 spot | 二 |

## 10. 待确认（请逐条选）
1. **导航语义**按 §3.1（active 高亮 live、其余历史/未运行、保留「总览」）？（推荐）
2. **分两期**，先上一期（点差/theta/iv/ivp，零新行情调用），二期再加现价距离？（推荐）
3. 复用方式按 §5.4（抽共享纯函数，CLI/web 同源）而非各写一份？（推荐）
4. 维持 vanilla JS + 3s 轮询，不引框架/不上 WebSocket？（推荐）
5. 数据源合并按 §3.2（引擎在场优先、否则影子、再否则等待）？（推荐）

> 确认后我转入实现：按 §6 一期的 task plan 落地，并产出 runbook（部署/重建/验证/回滚）。本地不跑 build/test（见 skill 硬约束），验证步骤写进 runbook 由 HK 执行。
