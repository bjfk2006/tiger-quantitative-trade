# 设计：铁鹰 combo 健壮化（原子开仓 + 可靠平仓 + 半成交回滚 + 持仓对账）

日期：2026-06-26 · 能力：building-production-feature · 状态：**已实现（单测 52 绿；5 项待确认采纳：1-4 推荐 + 保留 VERTICAL 回退；待 paper 验证 §4）**

> 落点：`adapters/trading.py`（`cancel_order`、`flatten_leg`）、`strategy/condor.py`
> （`_reverse_legs`、`_submit_open`/`_open_custom`/`_open_vertical`、`_cancel_quiet`/`_poll_filled`/
> `_unwind`、`approve()` 重写、`_close_all()` 翻转腿、`resume()` + `_reconcile_legs()` 对账→ERROR）、
> `domain/models.py`（`condor_open_combo_type`，默认 CUSTOM）、`service.py`/`.env.example`
> （`OBOT_CONDOR_OPEN_COMBO_TYPE`）。复用 `BotState.ERROR` 作"待人工核对"态（run_once 不自动动作）。

> 起因：2026-06-26 paper 实测 approve 一次，暴露三个问题（详见 [[condor-combo-semantics]]）。
> 开仓 combo 语义本身已验证正确（VERTICAL / 四腿方向 / 负净价=信用，成交 avg_fill=-1.1）；
> 本设计修的是**执行健壮性**，不是选腿/风控逻辑。

## 1. 问题（实测证据 + 代码定位）

| # | 问题 | 证据 / 代码 |
|---|---|---|
| P1 | **半成交孤儿仓** | `approve()` 顺序提交两个 VERTICAL（`condor.py:511-526`）：put 垂直 FILLED、call 垂直 30s 未成交（paper 不把两单当原子）→ `return False`，但 put 垂直已成交、`self.legs` 未登记、状态退回 PROPOSED → **引擎不监控的单边孤儿仓 + 悬空 call 挂单**。实盘=未对冲裸仓。 |
| P2 | **平仓 combo 语义未验证** | `_close_all()`（`condor.py:628-636`）用**相同 leg sides** + 组合 `action='SELL'` 平仓，依赖"SDK 组合级 action 翻转各腿方向"这一**未证实**假设。若不翻转 → 自动出场会**反向加仓**。`_close_all` 是止盈/止损/到期出场命脉。 |
| P3 | **崩溃恢复不与券商对账** | `resume()`（`condor.py:680-700`）只信任本地快照，不查券商实际持仓 → P1 的孤儿仓/快照漂移无法被发现纠正。设计原 §7 承诺的"与券商持仓核对"未落地。 |

> 附：`tc.get_positions` 默认 `sec_type=STK`，但适配层 `get_option_position` 已正确用 `SecurityType.OPT`（`trading.py:127`），故逐腿持仓查询本身无误；P3 是 resume **没调用**它做对账。

## 2. 设计目标

开仓**原子**（要么四腿全成、要么零成）、平仓**无歧义且原子**、任何残腿能**自动回滚/对账**，全程 paper 先验。

## 3. 方案

### 3.1 原子开仓：单个 4 腿 CUSTOM combo（替代两个 VERTICAL）
SDK `ComboType.CUSTOM` 支持任意腿数单笔组合。把铁鹰四腿装进**一个** CUSTOM 单：
- 一单 = 一次成交事件 → **从结构上消除两垂直间的半成交**（P1 根因）。
- legs：`[BUY put_long, SELL put_short, SELL call_short, BUY call_long]`（与现有 build_condor 顺序一致）。
- 净价 = 全condor信用，`limit = -round(total_credit, 2)`（负=收款，已验证符号）。`action='BUY'`。
- `adapters/trading.py::place_combo` 已支持任意腿数 + 任意 combo_type，**仅需传 `'CUSTOM'` 与四腿**，适配层零改动。

### 3.2 无歧义平仓：翻转腿动作的 4 腿 CUSTOM combo（替代"相同腿+action=SELL"）
不再依赖组合级 action 翻转，而是**显式翻转每条腿的 BUY/SELL**，镜像已验证的开仓机制：
- 平仓 legs = 开仓各腿动作取反：`[SELL put_long, BUY put_short, BUY call_short, SELL call_long]`。
- `action='BUY'`、`limit = +round(total_debit, 2)`（正=付钱买回，镜像开仓负=收钱）。
- 语义明确：腿动作直接表达"减仓方向"，不猜 SDK 组合 action 行为。**P2 消除**。

### 3.3 半成交回滚 + 撤单能力（安全网）
即便原子 combo，仍要兜底"未在 fill_timeout 内成交"：
- `adapters/trading.py` 新增 `cancel_order(order_id)`（SDK `cancel_order` 已存在）。
- `approve()` 重写：提交 CUSTOM 开仓单 → `_await_fill` → 若未成交：**撤单**，再查实际成交（防撤单/成交竞态）；若竟有部分腿成交，用 **3.2 的平仓单或逐腿反向市价单**回滚已成交部分 → 回到 IDLE，**绝不留孤儿**。
- 逐腿反向兜底（`option_contract_by_symbol`+`market_order`，SELL 平 long / BUY 平 short）作为 combo 回滚失败时的最后手段（本次手动清仓已验证可行）。

### 3.4 resume 与券商对账（P3）
`resume()` 载入快照后，对每条腿调 `get_option_position` 核对方向/数量：
- 全部吻合 → MONITORING。
- 缺腿/数量不符 → 不进 MONITORING，**WARNING 告警 + 进入待人工核对状态**（不自动乱平），避免基于错误状态自动出场。

## 4. paper 验证计划（实现后必跑，先于信任自动出场）
1. **原子开仓**：approve → 查回单：`combo_type=CUSTOM`、四腿方向、`limit<0`、单笔 FILLED（一次成交）。
2. **平仓**：开小仓 → 触发 `_close_all`（或手动 close）→ 查持仓**归 0**（非 2×）、回单四腿动作为开仓取反、`limit>0`。
3. **回滚**：故意给极端 limit 让开仓不成 → 确认自动撤单、无残仓、回 IDLE。
4. **对账**：构造快照与券商不一致 → 确认 resume 告警不乱动。

## 5. 改动落点
- `option_bot/adapters/trading.py`：新增 `cancel_order`；（可选）`close_combo` 便捷封装。
- `option_bot/strategy/condor.py`：`approve()`（CUSTOM 原子开仓 + 回滚）、`_close_all()`（翻转腿 CUSTOM 平仓）、`resume()`（对账）、常量/辅助 `_reverse_legs`、`_unwind()`。
- `option_bot/domain/models.py`：（可选）`condor_open_combo_type` 配置（默认 `CUSTOM`，留 `VERTICAL` 回退）。
- `option_bot/tests/test_condor.py`：原子开仓单构造、翻转腿平仓单构造、未成交→撤单+回滚、resume 对账不一致告警（均用 mock 适配层）。
- `docs/deploy.md`：§19 更新（CUSTOM 单、平仓语义、回滚行为）。

## 6. 诚实与限制
- CUSTOM 四腿是否在该账户**原子成交**、净价符号，仍须 paper 实测确认（4.1）；不及预期则回退两 VERTICAL + 强回滚。
- 平仓改翻转腿后，旧"相同腿+action=SELL"路径废弃；切换前用 4.2 验证。
- 回滚用市价单兜底有滑点；但"平仓减风险"优先于价格。
- 仍是 in-sample edge、paper-first；本设计只提升执行可靠性，不改盈利预期。

## 7. 待确认
1. 开仓改**单个 4 腿 CUSTOM 原子单**（而非两 VERTICAL）？（推荐）
2. 平仓改**显式翻转腿的 CUSTOM 单**（不依赖组合 action 翻转）？（推荐）
3. 新增 `cancel_order` + approve 半成交**自动撤单+回滚到 IDLE**？（推荐）
4. resume 增加**逐腿券商对账**，不一致则告警待人工、不自动出场？（推荐）
5. 保留 `VERTICAL` 两单模式作配置回退，还是直接全切 CUSTOM？
