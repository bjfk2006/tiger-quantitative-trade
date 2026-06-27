# 设计：铁鹰入场 IV 信号改用「自算活 IV」（BS 反推 ATM 隐含波动率）

日期：2026-06-27 · 能力：building-production-feature · 状态：**已实现**（默认照本设计四点；测试 220 全绿）

> 起因：2026-06-26 夜间影子观察实证，引擎/影子用的 `briefs.volatility` IV 信号**陈旧、粗粒度**，
> 不适合 vol 择时。本设计把入场闸与合成 greeks 用的 IV 从该字段换成**自己从 ATM 期权价
> BS 反推的活 IV**（逐 tick 更新）。**只动"IV 从哪来"，不动入场闸/选腿/风控逻辑。**

## 1. 问题（实证）

2026-06-26 17:00Z–06-27 07:10Z 影子 86 次采样 + 引擎 189 次评估：

| 观察 | 数据 |
|---|---|
| `briefs.volatility`（引擎当前用的 IV） | **整段冻结在精确 16.65%**（189 次读数一字不差，含 3h RTH） |
| 自反推活 IV（ATM call mid → BS 反推） | **≈14.2%**（实测，2026-06-27） |
| 结论 | 该字段是**按日/陈旧的粗粒度值**：① 盘中不更新 ② 较活 IV **偏高约 +2.4pp** |

后果：vol-timing 用一个不随盘动、还偏高的 IV，**抓不到盘中波动率上行**（真出现 IV spike 时该字段可能尚未更新），择时信号质量差。注：`get_option_chain` 的 `implied_vol` 在本账户全为 0（[[condor-account-data-gap]]），也不可用——所以要自算。

## 2. 目标

入场闸与合成 delta 用的 IV 改为**自算 ATM 活 IV**：从期权链已有的 bid/ask 算 ATM mid，用 Black-Scholes 反推隐含波动率，逐 tick 反映真实市场。**无额外行情订阅**（chain 已有价；r 仍取 briefs `rates_bonds`）。

## 3. 设计

### 3.1 BS 正向定价 + 反推（纯函数，复用已有 `norm_cdf`）
```
bs_price(S,K,T,σ,r,put_call):
  d1=(ln(S/K)+(r+σ²/2)T)/(σ√T); d2=d1−σ√T
  call = S·N(d1) − K·e^(−rT)·N(d2)
  put  = K·e^(−rT)·N(−d2) − S·N(−d1)

implied_vol_from_price(price,S,K,T,r,put_call):  # 二分反推，稳健、无 vega 除零问题
  下界 intrinsic：call=max(0,S−K·e^(−rT)), put=max(0,K·e^(−rT)−S)
  若 price ≤ intrinsic 或 price ≥ S（明显坏价）→ 返回 None
  σ∈[1e-3, 3.0] 二分 ~60 次至收敛 → σ
```

### 3.2 ATM 活 IV（噪声鲁棒）
```
atm_iv_live(chain_rows, spot, T, r):
  取最接近 spot 的 1~3 个行权价；对每个 strike 的 call 与 put：
    mid=_mid(row)；跳过 mid 缺失/低于内在价/点差过宽的坏价
    iv=implied_vol_from_price(mid,...)；落在 (1%,300%) 才收集
  返回所有有效 IV 的**中位数**（抗个别坏报价/宽点差）；无有效值→None
```
- 用 **ATM**（vega 最大、反推最稳）；call+put 各算一遍取中位数（平价下二者应一致，互为校验）。
- 现价 `spot` 用既有 `implied_spot`（平价反推，已实测稳定 ±0.1）。
- 实测坑：本账户深 ITM call 报价陈旧（latest=NaN、点差宽），故**只取近 ATM** 且按点差过滤。

### 3.3 接入点（最小改动）
`CondorManager._fetch_iv_rate(chain, spot)` 改为：
```
r = cfg.condor_risk_free or briefs.rates_bonds or 0.04   # r 仍来自 briefs(便宜、真实)，可配置覆盖
iv = atm_iv_live(chain, spot, dte/365, r)                # ← 新：自算活 IV
若 iv is None 且 cfg.condor_iv_source 允许回退 → 用 briefs.volatility（旧行为兜底）
return iv, r
```
- 该 IV 同时供**入场闸**（iv vs min_iv）与 **enrich_greeks**（合成 delta 的 σ）——一处改进、两处受益（择时更真、选腿更准）。
- 仍是**平值单一 IV、无 skew**（skew 为 P2）；只是把"陈旧 16.65%"换成"活 ~14%"这个更对的平值。

### 3.4 配置（新增）
| 字段 | 默认 | 说明 |
|---|---|---|
| `condor_iv_source` | `computed` | `computed`=BS 反推活 IV（默认）；`briefs`=旧 volatility 字段（回退/对照） |

（`condor_risk_free` 已存在，继续作 r 覆盖。）

## 4. 改动落点
- `option_bot/strategy/condor.py`：新增纯函数 `bs_price` / `implied_vol_from_price` / `atm_iv_live`；改 `_fetch_iv_rate` 用活 IV（带 briefs 回退）。
- `option_bot/domain/models.py`：`StrategyConfig` 增 `condor_iv_source`。
- `option_bot/service.py` + `.env.example`：读 `OBOT_CONDOR_IV_SOURCE`。
- `option_bot/tests/test_condor.py`：
  - 反推往返：`price=bs_price(σ)` → `implied_vol_from_price` 还原 σ（call/put、几档 moneyness）。
  - `atm_iv_live`：合成链按已知 σ 定价 → 还原该 σ；含坏价(低于内在/缺失)被剔除、取中位数。
  - 边界：price≤intrinsic→None；深 ITM/OTM 不参与。
  - 集成：manager 在 greeks 缺失时用活 IV 过闸（mock 适配层）。
- `option_bot/shadow.py`：自动受益（复用引擎选结构逻辑，无需改）。

## 5. 诚实与限制
- **仍是平值无 skew 的单一 IV**：只是把陈旧字段换成更准的活平值；逐档 skew 仍 P2。
- **mid 噪声**：ATM 点差/坏价会扰动反推；用近 ATM 多档中位数 + 点差过滤缓解。
- **r 仍来自 briefs**：rates_bonds 较稳；可 `condor_risk_free` 覆盖。
- **阈值需重校**：活 IV(~14%) 比陈旧值(16.65%) 低，`condor_min_iv=0.20` 在当前市仍不过闸（本来也不该开）；上线后按活 IV 的实际分布重设阈值（理想 P2 上 IV-Rank 百分位）。
- 本改动**只提升择时信号质量**，不改变"IV 贵才卖"的策略本质，也不保证盈利；paper/影子先行。

## 6. 待确认
1. 入场 IV 改用**自算活 IV（BS 反推 ATM）**、保留 `briefs` 作配置回退？（推荐）
2. 取 **call+put 近 ATM 多档中位数**做鲁棒估计？（推荐）
3. r 继续用 briefs `rates_bonds`、`condor_risk_free` 可覆盖？（推荐）
4. 上线后按活 IV 重设 `condor_min_iv`（暂不在本设计内定具体值）？
