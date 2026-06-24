# -*- coding: utf-8 -*-
"""期权日线回测 CLI（宿主机运行）：

  cd /root/tiger-quantitative-trade
  python3 -m option_bot.backtest --symbol AMD --expiration 2024-12-20 --strike 150 \
    --put-call Call --from 2024-09-01 --to 2024-12-20 \
    --strategy trailing --trail-activation 20 --trail-giveback 10

只 import 纯模块（不依赖 tigeropen/click），便于在装了 dolt 的宿主机直接跑。
"""
import argparse
import json
import sys

from option_bot.domain.models import StrategyConfig
from option_bot.backtest.dolt_source import DEFAULT_REPO, DoltError, load_option_series
from option_bot.backtest.engine import run_backtest, run_batch


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


def main(argv=None):
    p = argparse.ArgumentParser(prog='python3 -m option_bot.backtest',
                                description='期权日线回测（复用实盘平仓策略；日线近似）')
    p.add_argument('--repo', default=DEFAULT_REPO, help=f'dolt options 仓库路径（默认 {DEFAULT_REPO}）')
    p.add_argument('--symbol', required=True)
    p.add_argument('--expiration', required=True, help='到期日 YYYY-MM-DD')
    p.add_argument('--strike', required=True, type=float)
    p.add_argument('--put-call', dest='put_call', default='Call', help='Call/Put')
    p.add_argument('--from', dest='from_date', required=True, help='起始日 YYYY-MM-DD')
    p.add_argument('--to', dest='to_date', required=True, help='结束日 YYYY-MM-DD')
    p.add_argument('--entry-date', default=None, help='指定入场日（默认区间首个有效日）')
    p.add_argument('--fill', choices=['ask', 'mid'], default='ask', help='入场价口径（默认 ask）')
    p.add_argument('--batch-entries', action='store_true',
                   help='同合约多入场：区间内每个交易日各入场一次并汇总胜率')
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

    cfg = _build_cfg(a)
    cfg.validate()
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
