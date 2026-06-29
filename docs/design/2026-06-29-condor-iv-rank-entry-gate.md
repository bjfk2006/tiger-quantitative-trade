# 设计：铁鹰入场闸引入 IV-Rank（绝对阈值 → 相对分位）

日期：2026-06-29 · 能力：building-production-feature · 状态：**已实现**（照四点推荐；248 测试全绿，默认 absolute 零行为变化）

> 实现补记：把"both 模式的绝对地板"独立成 `condor_iv_rank_floor`（不复用 `condor_min_iv`），
> 使暖机回退始终用安全的 `condor_min_iv`(0.20)——避免"为 both 下调 min_iv 同时把暖机闸也放松"的
> 隐患。IV 历史文件路径用 `condor_iv_history_file`（引擎从 data 目录派生、影子显式指同一文件），
> 因 shadow 的 `lock_or_none` 用 /tmp 探针路径、不能靠 state 目录派生共享。

> 起因：[回测 2026-06-29-iv-entry-gate-frequency] 实证——绝对 `IV≥20%` 闸在平静期偏高，
> 机会稀疏且成簇（近 1–3 年某月有窗口仅 ~25–30%）。premium-selling 真正在意的是"现在 IV
> 相对自己最近一年是不是贵"，而非绝对值。本设计把入场闸从绝对阈值升级为 **IV 分位/IV-Rank**，
> 建立在已部署的活 IV 信号（[设计 2026-06-27-condor-live-iv-signal]，`atm_iv_live`）之上。
> **默认 `absolute` 模式＝今天行为零变化**，仅在显式切换时生效。

## 1. 问题

- 绝对闸 `condor_min_iv=0.20`：平静期（如当前活 IV~14%）长期不触发，bot 空等数周；
  波动率整体抬升的年份又可能过于频繁。**同一个绝对数在不同波动率状态下含义不同。**
- premium seller 的本质信号是**相对贵**：今天 IV 处于自身近一年分布的高位 → 卖方溢价好。

## 2. 度量

设近 `L` 个交易日的活 IV 历史 `H`、当前活 IV `iv_now`：

- **IV Percentile（IVP，主，鲁棒）** = `#{x∈H : x < iv_now} / |H| × 100`（0–100）。
  "过去一年里有多大比例的日子 IV 比今天低"——对单次极端尖峰不敏感。
- **IV Rank（IVR，辅，仅记录/参考）** = `(iv_now − min H)/(max H − min H) × 100`。
  直观但对极值敏感（一次 COVID 尖峰把 max 抬高后长期显得低）。`max==min` 时无定义。

> 选 **IVP 做闸门**（鲁棒），同时把 IVR 一并算出落日志/看板，便于对照。卖方在 IVP **高**时入场。

## 3. 入场闸模式

新增 `condor_iv_gate_mode`：

| 模式 | 条件 | 说明 |
|---|---|---|
| `absolute`（默认） | `iv_now ≥ condor_min_iv` | **＝今天行为，零变化** |
| `rank` | `IVP ≥ condor_min_iv_rank` | 纯相对分位 |
| `both`（**推荐**） | `iv_now ≥ condor_min_iv` **且** `IVP ≥ condor_min_iv_rank` | 既要相对贵、又要绝对不太便宜 |

`both` 的动机：IVP 可能很高但绝对 IV 仍很小（如 IVP=90 而 IV 仅 9%，相对贵、绝对溢价仍薄）。
卖方两者都要。建议切 `both` 时把 `condor_min_iv` **从 0.20 下调到一个地板**（如 0.12–0.13，
仅防绝对过低），让 IVP 承担"择时贵/便宜"。具体地板值上线后按活 IV 分布定，不在本设计内写死。

## 4. 数据与冷启动（关键、诚实）

IVP 需要**活 IV 的历史序列**，而本 bot 只从 2026-06-27 才开始算活 IV——**没有现成历史**
（[condor-account-data-gap]：chain.IV 全 0、briefs 陈旧）。设计如下：

### 4.1 IV 历史存储 `IVHistoryStore`
- 文件 `/app/data/iv_history_<symbol>.json`，内容 `[{date:'YYYY-MM-DD', iv:0.142, src:'live'|'seed'}]`。
- **每个交易日采一个样**：引擎 `_try_propose` 成功算出活 IV 时，按当日日期 **dedup 写入**
  （当日已有则更新/跳过）。原子 `os.replace`。一天一点（年序列标准做法）。
- **滚动裁剪**：只保留最近 `L`（默认 252 交易日 ≈ 1 年）条。
- 引擎与影子**共用同一文件**：引擎每 60s（IDLE）可能采样、影子 cron 每 10min 采样；
  dedup-by-date + 原子替换 ⇒ 同日并发写幂等（同日值近似、最后写胜），可接受。影子无引擎时自采。

### 4.2 暖机回退（保证安全）
- 历史不足 `condor_iv_rank_min_history`（默认 60 交易日 ≈ 3 个月）时，`rank`/`both` 模式
  **回退到 `absolute`**（用 `condor_min_iv`），并日志说明。⇒ 数据不够时绝不乱开仓。
- **现实**：活 IV 序列才 2 天，自然暖机要 ~3 个月 IVP 才有意义。

### 4.3 可选：VIX 代理种子（加速暖机，默认关）
- `condor_iv_rank_seed_from_vix=false`：开启则用 CBOE VIX 历史（`VIX − gap` 近似 ATM IV，
  见回测文档口径修正）**一次性回填**过去 `L` 天的 `src:'seed'` 历史，让 IVP 立即可用。
- 口径警告：种子是 VIX 代理、与自算活 IV 方法不同（虽同为 ATM 量纲）；**随滚动窗口在 `L` 天内
  老化replace为真样本**。默认关；仅在想立刻启用 IV-Rank 时开，并接受前期分位含代理偏差。
- 复用已存在的 `option_bot/backtest/iv_gate_freq.py` 的 VIX 加载逻辑，避免重造。

## 5. 纯函数（可单测，无 SDK）
- `iv_percentile(history, iv_now) -> float|None`（空历史→None）
- `iv_rank(history, iv_now) -> float|None`（空或 max==min→None）
- `passes_entry_gate(...)` 扩展：增加 `mode/ivp/min_rank/history_ok` 参数，按 §3 表判定；
  `history_ok=False`（暖机未满）时无论 mode 一律走 `absolute` 分支。保持原有
  has_position/RTH/iv-None 短路顺序在最前。

## 6. 接入点
- 新增 `option_bot/strategy/iv_history.py`：`IVHistoryStore`（load/append_daily/prune/values）+
  `iv_percentile`/`iv_rank` 纯函数。
- `condor.py: _try_propose`：算出活 `iv` 后 → `store.append_daily(today, iv)`；
  `ivp=iv_percentile(store.values(), iv)`；`history_ok = len(store) >= min_history`；
  `passes_entry_gate(iv, min_iv, rth=True, has_position=False, mode, ivp, min_rank, history_ok)`。
  提案 dict + 日志带上 `ivp/ivr`。
- `domain/models.py: StrategyConfig` 增：`condor_iv_gate_mode='absolute'`、`condor_min_iv_rank=50.0`、
  `condor_iv_rank_lookback_days=252`、`condor_iv_rank_min_history=60`、
  `condor_iv_rank_seed_from_vix=False`、（可选）`condor_iv_rank_vix_gap=4.0`。
- `service.py`/`shadow.py`：读 `OBOT_CONDOR_IV_GATE_MODE / _MIN_IV_RANK / _IV_RANK_LOOKBACK /
  _IV_RANK_MIN_HISTORY / _IV_RANK_SEED_FROM_VIX`；shadow 用同一 store。
- `status()`/看板/`condor.sh`：展示 `iv / ivp / ivr / gate_mode / history_days`。
- `deploy.md`：配置表 + IV-Rank 运维段（暖机、种子、回退）。

## 7. 默认零变化
不配任何新变量 → `condor_iv_gate_mode='absolute'` → 完全等于今天的绝对 `IV≥0.20`。
HK 现网不受影响，除非显式 `OBOT_CONDOR_IV_GATE_MODE=both` 等。

## 8. 测试
- `iv_percentile`/`iv_rank`：已知序列的分位/rank 数值、边界（空、单点、全相等、当前为最大/最小）。
- `IVHistoryStore`：同日 dedup、滚动裁剪到 L、原子写、坏文件容错、并发同日幂等。
- 闸门：`absolute` 与现状逐条等价；`rank`/`both` 在给定 ivp 下的开/拦；
  **暖机未满 → 强制回退 absolute**（核心安全用例）。
- 种子：seed_from_vix 回填条数/`src` 标记/老化（被真样本顶替）。
- 集成：manager 在 `both` 模式下，活 IV 高但 IVP 低 → 不开；两者都达标 → 提案。
- 既有 227 全绿不回归。

## 9. 局限
- 暖机期长（自采 ~3 个月）；VIX 种子可加速但前期分位含代理偏差。
- 一天一样本：忽略盘中 IV 波动（年分位标准做法，可接受）。
- IVP 是相对度量：极端长期低波动环境下，IVP 高也可能绝对溢价薄——故推荐 `both` 加绝对地板。
- 不改变"卖方在贵时入场"的本质，也不保证盈利；paper/影子先行。

## 10. 待确认
1. 闸门度量用 **IV Percentile（主）+ IV Rank（仅记录）**？（推荐）
2. 模式默认 `absolute`（零变化），推荐运维切 **`both`**（绝对地板 + IVP≥阈值），并把 `condor_min_iv`
   从 0.20 下调为地板（如 ~0.12）？（推荐）
3. 冷启动默认 **自采暖机 + 满 60 日前回退 absolute**；**VIX 代理种子默认关**、仅按需开启加速？（推荐）
4. 默认参数 **IVP 阈值 50、回看 252 日、暖机 60 日**？（推荐，均可配）
