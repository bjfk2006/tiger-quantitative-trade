# 设计（轻量增量）— 已平仓交易盈亏统计 + 按 identifier/时间范围查询

**Date**: 2026-06-23
**类型**: 小功能增量（building-production-feature §Design，1–4 文件）
**状态**: 待确认（未写代码）

## 1. 目标（一句话）
基于既有 `trades` 表，在看板上提供**已平仓交易的盈亏统计**：可按 **identifier + 时间范围（+可选账户）** 查询，展示**单笔明细 + 汇总指标 + 累计盈亏趋势**，方便复盘总结。

**不做**：持仓期间逐 tick 曲线（需另建时序表，数据量大，本期不做）；未平仓（未实现）盈亏统计。

## 2. 数据来源（已有，不改 schema）
`trades` 表（`persistence/db.py`）现有字段即够用：
`id, ts(ms), account, identifier, symbol, direction, action(OPEN/CLOSE), qty, price, reason, order_id, pnl_percent`

- **配对成单笔（round-trip）**：同一 `(account, identifier)` 下，按 `ts` 升序 **FIFO** 把 OPEN 与其后的 CLOSE 配成一笔。
- **单笔已实现盈亏**：
  - `pnl_percent` 直接取 CLOSE 行的值（sink 已按 (close−entry)/entry×100 写入）。
  - `pnl_amount` = (close_price − open_price) × qty × **multiplier**。
- **multiplier 取值**：本 bot 全是美股期权 → 固定 **×100**（不改 schema）。在 `stats` 模块以常量 `OPTION_MULTIPLIER=100` 注明；若将来支持非期权再补存字段。
- **时间过滤口径**：按 **CLOSE 的 ts**（平仓即实现）。未配到 CLOSE 的 OPEN（仍持仓）**不计入**已平仓统计。

## 3. 影响/新增文件
| 文件 | 改动 |
|---|---|
| `option_bot/persistence/db.py` | 新增 `list_trades_in_range(account=None, identifier=None, start_ts=None, end_ts=None)`：参数化 WHERE，返回区间内 trades（按 ts 升序）。**只读** |
| `option_bot/persistence/stats.py`（新） | 纯逻辑：`pair_round_trips(trades)` / `summarize(round_trips)` / `equity_curve(round_trips)`。无 SQL、无 SDK，**易单测** |
| `option_bot/web/dashboard.py` | 新增只读路由 `GET /api/history`（Basic 认证，沿用现有）|
| `option_bot/web/templates/dashboard.html` | 新增「历史统计」区：查询表单 + 汇总卡片 + 明细表 + 累计盈亏 SVG 折线（**纯 JS/SVG，不引图表库**）|
| `option_bot/tests/test_stats.py`（新） | 配对/汇总/边界用例 |

> 不新增运行时依赖；不改数据库 schema；不动交易逻辑/状态机。

## 4. API 设计
**`GET /api/history`**（只读，Basic 认证）
查询参数（全部可选）：
| 参数 | 说明 | 例 |
|---|---|---|
| `identifier` | 期权标识，**精确匹配**（留空=全部）| `SPCX  260626C00165000` |
| `from` / `to` | 日期 `YYYY-MM-DD`（按当地/UTC，见 §7 待定）；闭区间 | `2026-06-01` |
| `account` | 账户（留空=全部，可区分模拟/实盘）| `3170246` |
| `limit` | 明细最多返回条数，默认 200 | |

返回 JSON：
```json
{
  "summary": {
    "count": 12, "wins": 7, "losses": 5, "win_rate": 0.583,
    "total_pnl_amount": 320.5, "avg_pnl_percent": 6.2,
    "max_win": 735.0, "max_loss": -375.0
  },
  "trades": [
    {"open_ts":..., "close_ts":..., "account":"...", "identifier":"...",
     "symbol":"QQQ", "direction":"LONG", "qty":1,
     "open_price":7.5, "close_price":11.3, "reason":"TAKE_PROFIT",
     "pnl_percent":50.7, "pnl_amount":380.0}
  ],
  "equity_curve": [{"ts":..., "cum_pnl":380.0}, {"ts":..., "cum_pnl":5.0}, ...]
}
```
- 错误：DB 不可用 → 500 `{"error":"..."}`；无数据 → 200 空汇总 + 空数组。

## 5. 核心流程
```
/api/history(identifier,from,to,account)
  → db.list_trades_in_range(...)            # 参数化查询区间内 OPEN/CLOSE 行
  → stats.pair_round_trips(rows)            # FIFO 配对成 round-trips（含 pnl_amount=Δprice×qty×100）
  → 按 close_ts ∈ [from,to] 过滤            # 已实现口径
  → stats.summarize(rts) + stats.equity_curve(rts)
  → jsonify
```
区间过滤策略：先把范围**适当放宽**取 trades（因为 OPEN 可能早于 from），配对后再按 **close_ts** 落在 [from,to] 精确过滤。实现上 `list_trades_in_range` 的 start_ts 用 `from` 之前留余量或直接取该 identifier/account 全量（数据量小，简单优先）。

## 6. 看板 UI（轻量）
在现有页面底部加「历史统计」区：
```
┌ 历史统计 ───────────────────────────────┐
│ 标识[____________] 从[YYYY-MM-DD] 到[___] 账户[__] [查询] │
│ ┌汇总┐ 总盈亏 +$320.5 | 胜率 58.3% | 笔数 12 | 平均 +6.2% │
│ 累计盈亏: ╱╲___╱  (内联 SVG 折线)                          │
│ 明细表: 平仓时间│标识│方向│数量│开/平价│原因│盈亏%│盈亏$ │
└──────────────────────────────────────────┘
```
- 纯 JS `fetch('/api/history?...')` + 渲染表格 + 手绘 SVG `<polyline>` 画累计盈亏（零依赖）。
- 盈亏正绿负红，沿用现有样式。

## 7. 待定（给默认）
1. **时间口径**：`from/to` 按 **UTC 日界** 还是**本地时区**？默认 **UTC**（与 ts 存储一致、最简）；要按美东/北京再加换算。
2. **identifier 匹配**：默认**精确**；是否要支持「按标的 symbol 模糊」（如填 `SPCX` 匹配该标的所有合约）？建议**加一个 `symbol` 参数**做模糊更实用——确认要否。
3. **multiplier**：默认全 ×100（期权）。确认无非期权场景。

## 8. 测试（不跑本地，产出后由 CI/你跑）
`test_stats.py`：
- 配对：OPEN→CLOSE 正常配对；多笔 FIFO；未配对 OPEN（持仓中）被排除；CLOSE 无 OPEN 跳过。
- 金额：(close−open)×qty×100 正负;
- 汇总：win_rate / total / avg / max_win / max_loss；空集返回零值。
- 范围/identifier/account 过滤命中与边界。

## 9. 落地后部署
两台各 `git pull` + `docker compose up -d --build` 重建（HK 模拟盘 + 新加坡实盘，互不影响）。纯新增 + 只读，回滚=删新增文件 + 还原 dashboard/db 两处小改。

---

## 10. 决策（已确认）
1. **时间口径 = 美东（America/New_York）**：`from/to` 按 ET 日界换算成 ms（`from`→当日00:00 ET，`to`→次日00:00 ET 作为开区间上界）。
2. **identifier = 下拉列表**：不用自由文本。新增 `GET /api/history/identifiers?from=&to=&account=`，从表里按时间范围 **GROUP BY identifier**（取该区间内有 CLOSE 的标识）返回 `[{identifier,symbol,account,n,last_ts}]` 给前端填充下拉；选「全部」则统计所有标识。
3. **multiplier 固定 ×100**（期权）。
