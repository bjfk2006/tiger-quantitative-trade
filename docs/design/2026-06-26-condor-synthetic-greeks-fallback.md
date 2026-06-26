# 设计：铁鹰策略「合成希腊字母」兜底（券商无逐档 delta 时按 Black-Scholes 自算）

日期：2026-06-26 · 能力：building-production-feature · 状态：**已实现（单测 47 绿；4 项待确认均按推荐采纳；待部署 paper 验证）**

> 实现落点：`condor.py`（纯函数 `_parse_pct/norm_cdf/bs_delta/greeks_missing/implied_spot/
> enrich_greeks` + `CondorManager._fetch_iv_rate` + `_try_propose` 兜底分支）、
> `domain/models.py`（`condor_synthetic_greeks`/`condor_risk_free`）、`service.py`
> （`OBOT_CONDOR_SYNTHETIC_GREEKS`/`OBOT_CONDOR_RISK_FREE`，`_b` 增 default 参数）、`.env.example`。
> 偏差说明：未新增 `market_data.get_option_iv_rate`，改为复用既有 `get_option_quote`（返回 brief，
> 含 `volatility`/`rates_bonds`）在 manager 内解析——更少改动面，符合"不过度设计"。

> 起因：2026-06-26 美股开市后 paper 验证发现 HK 账户行情**不提供逐档 delta / 逐档 IV**，导致
> condor 的「按 16Δ 选短腿」永远选不出腿、不出提案。本设计加一个**自算 greeks 的兜底**，
> 让自动提案路径在这种数据条件下也能工作。combo 下单语义验证因此被阻塞，待本兜底落地后再做。

## 1. 问题（已实测证据）

盘中（13:30 UTC 后）直连 `QuoteClient` 实测，账户权限 `usOptionQuote`（L1）：

| 数据源 | 字段 | 实测值 |
|---|---|---|
| `get_option_chain('SPY', exp)` | `implied_vol` | **全 0.0**（322 行） |
| `get_option_chain` | `delta`/`gamma`/`theta`/`vega` | **全 0.0** |
| `get_option_briefs(ids)` | `volatility` | `"16.65%"` 字符串，**所有行同值**（标的层面单值，非逐档微笑） |
| `get_option_briefs` | delta 列 | **不存在** |
| `get_option_briefs` | `rates_bonds` | `0.039751`（无风险利率，可用） |
| `get_stock_briefs(['SPY'])` | — | **权限被拒**（无美股股票行情权限） |

后果：
1. `atm_iv()` 读 chain 的 `implied_vol`=0 → **IV 闸恒读 0.0%**（假阴性，已被运维临时把 `MIN_IV` 降到 0.05 绕过，但仍因 delta 缺失出不了腿）。
2. `select_by_delta()` 依赖逐档 delta → 全 0 → **选不出 16Δ 短腿** → 永不出提案。**这是拦路的根本。**

现价无法直接取（股票行情被拒），但 **put-call 平价**可稳定反推：近 ATM 多档 `S≈C_mid−P_mid+K` 一致收敛到 **732.1**（K=732 处 C≈P）。

## 2. 方案（最小兜底，不改变风控/下单语义）

仅替换「**选腿所需的 greeks/现价从哪来**」：券商没有就**自算**。下单、净价、定义风险、止盈止损全部不变。

### 2.1 现价：put-call 平价反推
`implied_spot(chain_rows)`：在近 ATM、买卖价均 >0、点差较小的若干档上取 `S = C_mid − P_mid + K·e^{−rT}`，取**中位数**抗噪。（实测一致性极好，±0.1。）

### 2.2 IV 与 r：来自 briefs
- IV：取一组近 ATM identifier 调一次 `get_option_briefs`，解析 `volatility`（`"16.65%"`→`0.1665`）。**单一平值 IV，无 skew**（账户只给这个）。
- r：同一 briefs 的 `rates_bonds`（0.0398）；可被配置 `condor_risk_free`（>0 时覆盖）。

### 2.3 逐档 delta：Black-Scholes 自算
- `norm_cdf(x)=0.5(1+erf(x/√2))`（用 `math.erf`，不引 scipy）。
- `bs_delta(S,K,T,σ,r,put_call)`：`d1=(ln(S/K)+(r+σ²/2)T)/(σ√T)`；call `=N(d1)`，put `=N(d1)−1`。
- `enrich_greeks(rows,S,σ,T,r)`：对 delta 缺失/为 0 的行填入自算 delta，并把 `implied_vol` 填为 σ。返回填充行数。

自验：S=732.1, σ=0.1665, r=0.0398, DTE=42 → put-16Δ≈**696**、call-16Δ≈**779**（5 宽翼 → long 691P/784C），结构合理。

### 2.4 接入 `CondorManager._try_propose`
```
chain = md.get_chain(...)
if synthetic_greeks_enabled and greeks_missing(chain):     # 全档 delta≈0
    S   = implied_spot(chain)
    σ,r = md.get_option_iv_rate(near_atm_ids)              # 解析 briefs volatility / rates_bonds
    enrich_greeks(chain, S, σ, dte/365, (cfg.condor_risk_free or r))
    iv_for_gate = σ                                        # 平值 IV 直接做闸（替代读 chain 的 0）
else:
    iv_for_gate = atm_iv(chain)                            # 原路径不变
# 之后 build_condor / select_by_delta 照旧（此时 delta 已填好）
```
`greeks_missing` 判据：链中 delta 全为 None/0。**有真 delta 的账户走原路径，零改动零风险。**

### 2.5 配置（新增，OBOT_CONDOR_*）
| 字段 | 默认 | 说明 |
|---|---|---|
| `condor_synthetic_greeks` | `true` | 检测到券商无 delta 时自动启用 BS 兜底；置 false 强制只用券商 greeks |
| `condor_risk_free` | `0.0` | 0=用 briefs `rates_bonds`；>0 覆盖 |

## 3. 改动落点
- `option_bot/strategy/condor.py`：新增纯函数 `_parse_pct / norm_cdf / bs_delta / implied_spot / enrich_greeks / greeks_missing`；`_try_propose` 接入兜底分支。
- `option_bot/adapters/market_data.py`：新增 `get_option_iv_rate(identifiers)` → `(iv:float, r:float)`，解析 briefs `volatility/rates_bonds`。
- `option_bot/domain/models.py`：`StrategyConfig` 增 `condor_synthetic_greeks`、`condor_risk_free`。
- `option_bot/service.py` + `.env.example`：读 `OBOT_CONDOR_SYNTHETIC_GREEKS / OBOT_CONDOR_RISK_FREE`。
- `option_bot/tests/test_condor.py`：BS delta 标杆（ATM≈±0.5、深 ITM→±1、深 OTM→0）、平价反推还原现价、enrich 填零后能选出 16Δ、`_parse_pct`。

## 4. 诚实与限制
- **平值 IV、无 skew**：真实卖方 put 侧 16Δ 会更价外（put skew 抬 IV）。合成 condor 的 put 短腿会比 skew-aware 略近钱、略多收权利金、略高被touch概率。P1 接受，文档标注；P2 若拿到逐档 IV 再升级。
- **平价现价**依赖近 ATM 流动性与无套利；用中位数抗个别坏报价。深 ITM 档报价陈旧（latest=NaN、点差宽）已实测，故只取近 ATM。
- **只影响选腿**：实际成交仍用真实 bid/ask，定义风险与最大亏损不受影响。
- **IV 闸量纲**：兜底后闸读真实平值 IV（现 SPY≈16.65%）。注意 `condor_min_iv=0.20` 在当前 SPY 下不会过闸；运维须按标的实际 IV 设阈值（现 HK 设 0.05）。
- combo 下单语义（`_OPEN_ACTION`/净价正负）**仍未验证**，本兜底落地后用真实提案 approve 一次性验证。
- 策略 edge 仍是 in-sample 回测、非盈利保证；**paper-first**。

## 5. 待确认
1. 采用「自算 greeks 兜底 + 自动检测启用」？（推荐）
2. r 默认用 briefs `rates_bonds`、允许 `condor_risk_free` 覆盖？（推荐）
3. 平值 IV 无 skew 的近似在 P1 可接受、P2 再升级逐档 IV？（推荐）
4. 落地后即在 paper 走一次真实 approve 验证 combo 语义？
