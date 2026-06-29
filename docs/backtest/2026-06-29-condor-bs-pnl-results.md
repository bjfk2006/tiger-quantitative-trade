# 回测结果：铁鹰盈亏（BS 重定价，SPY vs QQQ）

日期：2026-06-29 · 引擎：`option_bot/backtest/condor_engine.py: run_condor_bs_backtest`（B 方案）
· 设计：`docs/design/2026-06-29-condor-bs-repriced-backtester.md`

> ⚠️ **先读第 4 节**：本结果用"平 IV 无 skew"的 BS 模型，**系统性低估下行/尾部损失**，
> 数字偏乐观。仅适合**相对比较**（闸/平仓/标的/滑点的优劣），**不是绝对收益预测**。

## 1. 设置

| 项 | 值 |
|---|---|
| 标的/IV 代理 | SPY→VIX，QQQ→VXN（`iv=(指数收盘−gap)/100`，gap=4） |
| 数据 | `stocks.ohlcv` 日收盘（连续）+ CBOE 波动率指数日线；**不依赖 option_chain** |
| 区间 | 2019-01-01 → 2026-05-01（约 7.3 年） |
| 结构 | 目标 DTE 40、到期前 21 天强平、短腿 16Δ、翼宽 $5、单仓顺序、1 张、multiplier 100 |
| 出场安全底座 | 硬止损 2×权利金、DTE≤21 强平（始终生效） |

复现命令（HK 宿主机，`data/` 下需有 VIX_History.csv / VXN_History.csv）：
```bash
python3 -m option_bot.backtest --condor-bs --symbol SPY --vix-csv data/VIX_History.csv --gap 4 \
  --from 2019-01-01 --to 2026-05-01 --target-dte 40 --dte-exit 21 --short-delta 0.16 --wing 5 \
  --gate-mode absolute --min-iv 0.20 --close-strategy threshold
# QQQ 改 --symbol QQQ --vix-csv data/VXN_History.csv；相对闸改 --gate-mode both --rank-floor 0.12 --min-iv-rank 50
```

## 2. 结果

| 标的 | 入场闸 / 平仓 | 笔数 | 胜率 | 总盈亏 | 均值/笔 | 占权利金 | 止损次数 |
|---|---|---|---|---|---|---|---|
| **SPY** | absolute IV≥0.20 / threshold | 53 | **81.1%** | **$2092** | $39.5 | **27.4%** | 1 |
| SPY | both(地板0.12, IVP≥50) / threshold | 87 | 70.1% | $1458 | $16.8 | 11.7% | 2 |
| SPY | both / **trailing** 40-15 | 82 | 76.8% | $1902 | $23.2 | 16.5% | 1 |
| SPY | both / threshold **+滑点$0.05/股** | 81 | 72.8% | $1173 | $14.5 | 10.3% | 1 |
| **QQQ** | absolute IV≥0.20 / threshold | 77 | 76.6% | $1622 | $21.1 | 15.0% | 2 |
| QQQ | both / threshold | 84 | 75.0% | $974 | $11.6 | 7.7% | 4 |
| QQQ | both / **trailing** 40-15 | 81 | 77.8% | **$243** | $3.0 | 1.5% | 4 |

（出场原因分布略，见运行输出；普遍 TAKE_PROFIT / TIME_FORCE 为主，STOP_LOSS 极少——见第 4 节警告。）

## 3. 结论（相对比较，有效）

1. **"只在 IV 绝对高时开"(absolute) 每笔更肥**：SPY 吃到 27% 权利金 vs 相对闸 12%。相对闸(both)
   开得更勤（87 vs 53 笔）但每笔薄、总收益反而更低。**机会多 ≠ 赚得多。**
2. **SPY > QQQ**（反直觉）：QQQ 波动大→权利金多，但**更易被大动击穿/回吐**，多收的没盖住多亏的。
   直接回答"QQQ 频率高是否换来更高收益"——**没有**。
3. **trailing 非万能**：SPY 上帮忙（$1458→$1902）；**QQQ 上崩到 $243**（频繁被时间强平、抓不到衰减）。
   好坏取决于标的/行情。
4. **滑点很伤薄权利金单**：SPY both 仅加 $0.05/股往返 → 总收益 −20%（$1458→$1173）。
5. **绝对金额不大**：1 张、7 年最好 ~$2092 ≈ $285/年（单张风险上限 ~$400/笔），随张数线性放大。
6. 方向上：**SPY + 绝对高 IV 入场 + 固定止盈** 在模型里最稳——与现网默认起点一致；相对闸/QQQ/trailing 未占便宜。

## 4. ⚠️ 关键局限：数字偏乐观，低估亏损

- 53–87 笔里仅 **1–4 次止损**、胜率 70–81%——这是 **平 IV 无 skew 模型在低估下行/尾部**：
  真实指数看跌期权有 skew，**崩盘时 OTM put 的 IV 涨得比 ATM 猛**，真实平仓成本更高
  → **真实止损/最大亏损会更频繁、更大**。
- **"急跌/跳空击穿"那种最坏情形，正是本模型算得最不准的地方**（方向性击穿能算对，vol 维度的额外损失偏小）。
- 还忽略：真实买卖价差（仅 `slippage` 粗略近似）、股息、期限结构、盘中触发（出场晚一拍）。
- ⇒ 当**相对比较**用有效；当**绝对收益/安全性**预测会高估。逼近真实需 **A 方案**（连续逐合约 EOD、含 skew 的期权数据）。

## 5. 数据可得性备注
- `stocks.ohlcv` 有 SPY/QQQ（2011→2026，~3895 日）。
- dolt `option_chain` 仅 SPY、且为孤立日切快照（98% 建仓日无连续盯市），**撑不起市场报价版回测**
  （见 `docs/backtest/2026-06-29-iv-entry-gate-frequency.md` 与市场报价版设计）；故走 B 方案。
- QQQ 无 option_chain，但 BS 方案靠 stocks+VXN 仍可回测。
