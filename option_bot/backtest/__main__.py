# -*- coding: utf-8 -*-
"""期权日线回测 CLI（宿主机运行）：

  cd /root/tiger-quantitative-trade
  python3 -m option_bot.backtest --symbol AMD --expiration 2024-12-20 --strike 150 \
    --put-call Call --from 2024-09-01 --to 2024-12-20 \
    --strategy trailing --trail-activation 20 --trail-giveback 10

只 import 纯模块（不依赖 tigeropen/click），便于在装了 dolt 的宿主机直接跑。
"""
import argparse
import datetime
import json
import sys

from option_bot.domain.models import StrategyConfig
from option_bot.backtest.dolt_source import (DEFAULT_REPO, DEFAULT_STOCKS_REPO,
                                             DoltError, load_option_series,
                                             load_symbol_chain, load_underlying_closes)
from option_bot.backtest.engine import run_backtest, run_batch, run_rolling_atm
from option_bot.backtest.condor_engine import (run_condor_backtest,
                                               run_condor_bs_backtest)
from option_bot.backtest.iv_gate_freq import load as load_vix


def _build_cfg(a) -> StrategyConfig:
    return StrategyConfig(
        strategy_name=a.strategy,
        tp_percent=a.tp, sl_percent=a.sl,
        trail_activation=a.trail_activation, trail_giveback=a.trail_giveback,
        trail_relative_ratio=a.trail_relative_ratio,
        trail_relative_threshold=a.trail_relative_threshold,
        breakeven_activation=a.breakeven_activation, breakeven_lock=a.breakeven_lock,
        max_hold_minutes=a.max_hold_minutes,
    )


def _print_result(r, contract):
    print(f"\n合约: {contract}")
    if r is None:
        print("  无有效入场/数据。")
        return
    sign = '+' if r.pnl_percent >= 0 else ''
    print(f"  入场 {r.entry_date} @ {r.entry_price}  →  平仓 {r.exit_date} @ {r.exit_price}")
    print(f"  原因 {r.reason} | 持有 {r.days_held} 天 | 峰值 +{r.peak_pnl_percent}% | "
          f"结果 {sign}{r.pnl_percent}%")


def _run_rolling(a, cfg):
    # 期权链加载区间需覆盖到入场日所选合约的到期：to + target_dte + buffer
    horizon = (datetime.datetime.strptime(a.to_date, '%Y-%m-%d')
               + datetime.timedelta(days=a.target_dte + 10)).strftime('%Y-%m-%d')
    try:
        closes = load_underlying_closes(a.symbol, a.from_date, a.to_date, repo=a.stocks_repo)
        if not closes:
            print(f"无 stocks 现价：{a.symbol} {a.from_date}~{a.to_date}", file=sys.stderr)
            return 2
        chain = load_symbol_chain(a.symbol, a.put_call, a.from_date, horizon, repo=a.repo)
        if not chain:
            print(f"无期权链：{a.symbol} {a.put_call} {a.from_date}~{horizon}", file=sys.stderr)
            return 2
    except DoltError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    out = run_rolling_atm(closes, chain, cfg, a.strategy, target_dte=a.target_dte,
                          min_dte=a.min_dte, step_days=a.step_days, fill=a.fill)
    if a.json:
        print(json.dumps({'summary': out['summary'],
                          'entries': [{**r.to_dict(), **m}
                                      for r, m in zip(out['results'], out['metas'])]},
                         ensure_ascii=False, indent=2))
        return 0
    s = out['summary']
    print(f"\n滚动 ATM 回测: {a.symbol.upper()} {a.put_call} | 策略 {a.strategy} | "
          f"目标DTE {a.target_dte} | {a.from_date}~{a.to_date}")
    if s.get('count', 0) == 0:
        print("  无有效入场（检查现价/期权链覆盖与 DTE 设置）。")
    else:
        print(f"  入场数 {s['count']} | 胜率 {s['win_rate']*100:.1f}% | 均值 {s['avg_pnl_percent']:+.2f}% | "
              f"最大盈 {s['max_win']:+.2f}% / 最大亏 {s['max_loss']:+.2f}% | 平均持有 {s['avg_days_held']} 天")
        print(f"  平仓原因分布: {s['reasons']}")
        # 展示最好/最差各 3 笔
        rows = sorted(zip(out['results'], out['metas']), key=lambda x: x[0].pnl_percent)
        def fmt(r, m):
            sg = '+' if r.pnl_percent >= 0 else ''
            return (f"    {r.entry_date} 现价{m['spot']} → {m['strike']}C(到期{m['expiration']},DTE{m['dte']}) "
                    f"@ {r.entry_price} → {r.exit_date} @ {r.exit_price} | {r.reason} | {sg}{r.pnl_percent}%")
        print("  最差3笔:");  [print(fmt(r, m)) for r, m in rows[:3]]
        print("  最好3笔:");  [print(fmt(r, m)) for r, m in rows[-3:][::-1]]
    print("\n注：日线近似——无法复现盘中每 2 秒 trailing 与收盘前强平；ATM 按当日 close 选最近行权价。")
    return 0


def _build_condor_cfg(a) -> StrategyConfig:
    return StrategyConfig(
        mode='condor', condor_underlying=a.symbol.upper(),
        condor_target_dte=a.target_dte, condor_dte_exit=a.dte_exit,
        condor_short_delta=a.short_delta, condor_wing_width=a.wing,
        condor_min_iv=a.min_iv, condor_profit_target=a.profit_target,
        condor_stop_mult=a.stop_mult, condor_max_loss_pct=a.max_loss_pct,
        condor_account_equity=a.account_equity, max_qty=a.max_qty,
        condor_close_strategy=a.close_strategy,
        condor_trail_activation=a.trail_activation, condor_trail_giveback=a.trail_giveback,
        condor_iv_gate_mode=a.gate_mode, condor_min_iv_rank=a.min_iv_rank,
        condor_iv_rank_floor=a.rank_floor, condor_iv_rank_lookback_days=a.iv_rank_lookback,
        condor_iv_rank_min_history=a.iv_rank_min_history, condor_risk_free=a.risk_free)


def _run_condor(a):
    horizon = (datetime.datetime.strptime(a.to_date, '%Y-%m-%d')
               + datetime.timedelta(days=a.target_dte + 15)).strftime('%Y-%m-%d')
    cfg = _build_condor_cfg(a)
    try:
        calls = load_symbol_chain(a.symbol, 'Call', a.from_date, horizon, repo=a.repo)
        puts = load_symbol_chain(a.symbol, 'Put', a.from_date, horizon, repo=a.repo)
    except DoltError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    rows = ([{**r, 'put_call': 'CALL'} for r in calls]
            + [{**r, 'put_call': 'PUT'} for r in puts])
    if not rows:
        print(f"无期权链：{a.symbol} {a.from_date}~{horizon}", file=sys.stderr)
        return 2
    out = run_condor_backtest(rows, cfg, entry_to=a.to_date, independent=a.independent)
    s = out['summary']
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"\n铁鹰盈亏回测: {a.symbol.upper()} | 闸 {a.gate_mode} | 平仓 {a.close_strategy} | "
          f"目标DTE {a.target_dte} 短腿Δ{a.short_delta} 翼{a.wing} | {a.from_date}~{a.to_date} | "
          f"{'独立入场' if a.independent else '单仓顺序'}")
    if s.get('count', 0) == 0:
        print("  无有效入场（检查闸/数据覆盖与 DTE 设置）。")
        return 0
    print(f"  入场数 {s['count']} | 胜率 {s['win_rate']}% | 总盈亏 ${s['total_pnl_usd']} | "
          f"均值 ${s['avg_pnl_usd']}（{s['avg_pnl_pct_credit']}%权利金）")
    print(f"  最大盈 ${s['max_win_usd']} / 最大亏 ${s['max_loss_usd']} | 平均持有 {s['avg_days_held']}天 | "
          f"最大回撤 ${s['max_drawdown_usd']} | profit_factor {s['profit_factor']}")
    print(f"  出场原因: {s['reasons']}")
    rows_sorted = sorted(out['trades'], key=lambda t: t['pnl_usd'])
    def fmt(t):
        return (f"    {t['entry_date']}→{t['exit_date']} ({t['days_held']}天,DTE0~{a.target_dte}) "
                f"{' '.join(t['sides'])} | 信用{t['entry_credit']}→平{t['exit_cost']} | "
                f"{t['reason']} | ${t['pnl_usd']}（{t['pnl_pct_credit']}%）IVP{t['ivp_entry']}")
    print("  最差3笔:"); [print(fmt(t)) for t in rows_sorted[:3]]
    print("  最好3笔:"); [print(fmt(t)) for t in rows_sorted[-3:][::-1]]
    print("\n注：日线近似——出场按当日 mid 净价、比盘中触发晚一拍，跳空体现为次日跳变；"
          "历史链无 greeks→合成选腿/自算IV(无skew)；月度到期、行权价偏稀，翼为近似。")
    return 0


def _run_condor_bs(a):
    cfg = _build_condor_cfg(a)
    hor = (datetime.datetime.strptime(a.to_date, '%Y-%m-%d')
           + datetime.timedelta(days=a.target_dte + 15)).strftime('%Y-%m-%d')
    try:
        closes = load_underlying_closes(a.symbol, a.from_date, hor, repo=a.stocks_repo)
    except DoltError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    if not closes:
        print(f"无标的日线：{a.symbol} {a.from_date}~{hor}（检查 stocks 仓库）", file=sys.stderr)
        return 2
    try:
        vix = load_vix(a.vix_csv)               # [(date, high, close)]
    except FileNotFoundError:
        print(f"找不到波动率指数 CSV：{a.vix_csv}（用 iv_gate_freq --download 或指定 --vix-csv）",
              file=sys.stderr)
        return 2
    iv_series = {}
    for dt, _hi, close in vix:
        iv = (close - a.gap) / 100.0
        if iv > 0:
            iv_series[dt.strftime('%Y-%m-%d')] = iv
    out = run_condor_bs_backtest(closes, iv_series, cfg, entry_to=a.to_date,
                                 independent=a.independent, strike_spacing=a.strike_spacing,
                                 slippage=a.slippage)
    s = out['summary']
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0
    print(f"\n铁鹰 BS 重定价回测: {a.symbol.upper()} | 闸 {a.gate_mode} | 平仓 {a.close_strategy} | "
          f"目标DTE {a.target_dte} 短腿Δ{a.short_delta} 翼{a.wing} | gap{a.gap} 滑点{a.slippage} | "
          f"{a.from_date}~{a.to_date} | {'独立入场' if a.independent else '单仓顺序'}")
    if s.get('count', 0) == 0:
        print("  无有效入场（检查闸/数据/参数）。")
        return 0
    print(f"  入场数 {s['count']} | 胜率 {s['win_rate']}% | 总盈亏 ${s['total_pnl_usd']} | "
          f"均值 ${s['avg_pnl_usd']}（{s['avg_pnl_pct_credit']}%权利金）")
    print(f"  最大盈 ${s['max_win_usd']} / 最大亏 ${s['max_loss_usd']} | 平均持有 {s['avg_days_held']}天 | "
          f"最大回撤 ${s['max_drawdown_usd']} | profit_factor {s['profit_factor']}")
    print(f"  出场原因: {s['reasons']}")
    rs = sorted(out['trades'], key=lambda t: t['pnl_usd'])
    def fmt(t):
        return (f"    {t['entry_date']}→{t['exit_date']} ({t['days_held']}天) 现价{t['spot_entry']} "
                f"{' '.join(t['sides'])} | 信用{t['entry_credit']}→平{t['exit_cost']} | "
                f"{t['reason']} | ${t['pnl_usd']}（{t['pnl_pct_credit']}%）IVP{t['ivp_entry']}")
    print("  最差3笔:"); [print(fmt(t)) for t in rs[:3]]
    print("  最好3笔:"); [print(fmt(t)) for t in rs[-3:][::-1]]
    print("\n注：BS 模型价非市场成交；**平 IV 无 skew → 低估下行/尾部损失**（崩盘时 OTM put IV 涨更多）；"
          "VIX→ATM IV 用 gap 近似；跳空体现为次日跳变。仅作相对比较，非精确实盘损益。")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='python3 -m option_bot.backtest',
                                description='期权日线回测（复用实盘平仓策略；日线近似）')
    p.add_argument('--repo', default=DEFAULT_REPO, help=f'dolt options 仓库路径（默认 {DEFAULT_REPO}）')
    p.add_argument('--symbol', required=True)
    p.add_argument('--expiration', help='到期日 YYYY-MM-DD（单合约/多入场模式必填）')
    p.add_argument('--strike', type=float, help='行权价（单合约/多入场模式必填）')
    p.add_argument('--put-call', dest='put_call', default='Call', help='Call/Put')
    p.add_argument('--from', dest='from_date', required=True, help='起始日 YYYY-MM-DD')
    p.add_argument('--to', dest='to_date', required=True, help='结束日 YYYY-MM-DD')
    p.add_argument('--entry-date', default=None, help='指定入场日（默认区间首个有效日）')
    p.add_argument('--fill', choices=['ask', 'mid'], default='ask', help='入场价口径（默认 ask）')
    p.add_argument('--batch-entries', action='store_true',
                   help='同合约多入场：区间内每个交易日各入场一次并汇总胜率')
    # 滚动 ATM（见设计附录 B）
    p.add_argument('--rolling-atm', action='store_true',
                   help='滚动 ATM 批量：每日按现价选近月平值合约入场（不需 --expiration/--strike）')
    p.add_argument('--target-dte', type=int, default=30, help='滚动 ATM：目标到期天数（默认30）')
    p.add_argument('--min-dte', type=int, default=3, help='滚动 ATM：最小到期天数（默认3）')
    p.add_argument('--step-days', type=int, default=1, help='滚动 ATM：入场节奏（默认每个交易日）')
    p.add_argument('--stocks-repo', default=DEFAULT_STOCKS_REPO,
                   help=f'滚动 ATM：stocks 仓库（默认 {DEFAULT_STOCKS_REPO}）')
    # 铁鹰盈亏回测（见 docs/design/2026-06-29-condor-pnl-backtester.md）
    p.add_argument('--condor', action='store_true',
                   help='铁鹰盈亏回测：逐历史日重放开/持/平，输出真盈亏（不需 --expiration/--strike）')
    p.add_argument('--short-delta', type=float, default=0.16, help='condor 短腿目标 |delta|')
    p.add_argument('--wing', type=float, default=5.0, help='condor 翼宽($)')
    p.add_argument('--dte-exit', type=int, default=21, help='condor 到期前 N 天强平')
    p.add_argument('--min-iv', type=float, default=0.20, help='condor 绝对入场闸/暖机回退')
    p.add_argument('--profit-target', type=float, default=0.5, help='condor 止盈(权利金占比)')
    p.add_argument('--stop-mult', type=float, default=2.0, help='condor 止损(权利金倍数)')
    p.add_argument('--max-loss-pct', type=float, default=0.05, help='condor 单仓最大亏损占账户比例')
    p.add_argument('--account-equity', type=float, default=0.0, help='condor 账户净值(0→max_qty 定张)')
    p.add_argument('--max-qty', type=int, default=1, help='张数上限(account-equity=0 时用)')
    p.add_argument('--close-strategy', default='threshold', help='condor 平仓: threshold/trailing')
    p.add_argument('--gate-mode', default='absolute', help='condor 入场闸: absolute/rank/both')
    p.add_argument('--min-iv-rank', type=float, default=50.0, help='condor IVP 入场阈值')
    p.add_argument('--rank-floor', type=float, default=0.0, help='condor both 模式绝对地板(IV小数)')
    p.add_argument('--iv-rank-lookback', type=int, default=252, help='condor IV 分位回看(交易日)')
    p.add_argument('--iv-rank-min-history', type=int, default=60, help='condor 暖机最小历史(不足回退 absolute)')
    p.add_argument('--risk-free', type=float, default=0.04, help='condor BS 无风险利率')
    p.add_argument('--independent', action='store_true', help='condor 每日独立入场(默认单仓顺序)')
    # 铁鹰 BS 重定价回测（B 方案，见 docs/design/2026-06-29-condor-bs-repriced-backtester.md）
    p.add_argument('--condor-bs', dest='condor_bs', action='store_true',
                   help='铁鹰 BS 重定价回测：连续日close+波动率指数合成,不依赖期权链(SPY/VIX,QQQ/VXN)')
    p.add_argument('--vix-csv', default='VIX_History.csv', help='波动率指数 CSV(SPY→VIX,QQQ→VXN)')
    p.add_argument('--gap', type=float, default=4.0, help='VIX 高于 ATM IV 的点数(偏斜溢价)')
    p.add_argument('--strike-spacing', type=float, default=1.0, help='合成行权价网格间距($)')
    p.add_argument('--slippage', type=float, default=0.0, help='入场/平仓滑点(每股,近似成交摩擦)')
    # 策略参数（与 .env/CLI 同义）
    p.add_argument('--strategy', default='trailing',
                   help='threshold/trailing/breakeven/time_in_trade/bracket')
    p.add_argument('--tp', type=float, default=30.0)
    p.add_argument('--sl', type=float, default=50.0)
    p.add_argument('--trail-activation', type=float, default=20.0)
    p.add_argument('--trail-giveback', type=float, default=10.0)
    p.add_argument('--trail-relative-ratio', type=float, default=0.0)
    p.add_argument('--trail-relative-threshold', type=float, default=50.0)
    p.add_argument('--breakeven-activation', type=float, default=0.0)
    p.add_argument('--breakeven-lock', type=float, default=0.0)
    p.add_argument('--max-hold-minutes', type=float, default=0.0)
    p.add_argument('--json', action='store_true', help='输出 JSON')
    p.add_argument('--verbose', action='store_true', help='打印逐日盈亏轨迹')
    a = p.parse_args(argv)

    # ---- 铁鹰回测（自建 condor cfg，不走 straddle 校验）----
    if a.condor_bs:
        return _run_condor_bs(a)
    if a.condor:
        return _run_condor(a)

    cfg = _build_cfg(a)
    cfg.validate()

    # ---- 滚动 ATM 模式 ----
    if a.rolling_atm:
        return _run_rolling(a, cfg)

    if a.expiration is None or a.strike is None:
        print("错误: 单合约/多入场模式需 --expiration 和 --strike（或用 --rolling-atm）", file=sys.stderr)
        return 1
    contract = f"{a.symbol.upper()} {a.expiration} {a.strike} {a.put_call}"
    try:
        series = load_option_series(a.symbol, a.expiration, a.strike, a.put_call,
                                    a.from_date, a.to_date, repo=a.repo)
    except DoltError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 1
    if not series:
        print(f"无数据：{contract} 在 {a.from_date}~{a.to_date} 区间无 bid/ask 记录。", file=sys.stderr)
        return 2

    if a.verbose:
        e0 = series[0]['ask']
        print(f"逐日轨迹（{len(series)} 天，入场基准 ask={e0}）:")
        for r in series:
            pnl = (r['bid'] - e0) / e0 * 100.0 if e0 else 0.0
            print(f"  {r['date']}  bid={r['bid']:<8} ask={r['ask']:<8} pnl≈{pnl:+.1f}%")

    if a.batch_entries:
        out = run_batch(series, cfg, a.strategy, fill=a.fill)
        if a.json:
            print(json.dumps({'summary': out['summary'],
                              'results': [r.to_dict() for r in out['results']]},
                             ensure_ascii=False, indent=2))
        else:
            s = out['summary']
            print(f"\n合约: {contract} | 策略 {a.strategy} | 同合约多入场回测")
            if s.get('count', 0) == 0:
                print("  无有效入场。")
            else:
                print(f"  入场数 {s['count']} | 胜率 {s['win_rate']*100:.1f}% | "
                      f"均值 {s['avg_pnl_percent']:+.2f}% | 最大盈 {s['max_win']:+.2f}% / "
                      f"最大亏 {s['max_loss']:+.2f}% | 平均持有 {s['avg_days_held']} 天")
                print(f"  平仓原因分布: {s['reasons']}")
    else:
        r = run_backtest(series, cfg, a.strategy, entry_date=a.entry_date, fill=a.fill)
        if a.json:
            print(json.dumps(r.to_dict() if r else None, ensure_ascii=False, indent=2))
        else:
            print(f"策略: {a.strategy}")
            _print_result(r, contract)

    print("\n注：日线近似——无法复现盘中每 2 秒 trailing 与收盘前强平；峰值按逐日 bid 计、偏保守。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
