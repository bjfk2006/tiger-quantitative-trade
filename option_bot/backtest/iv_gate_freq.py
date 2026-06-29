# -*- coding: utf-8 -*-
"""IV 入场闸触发频率回测：用 CBOE VIX 历史日线估算「ATM IV ≥ 阈值」的经验概率。

文档：docs/backtest/2026-06-29-iv-entry-gate-frequency.md

为什么用 VIX：本账户 chain.implied_vol 全 0、briefs.volatility 陈旧，没有历史 ATM IV 序列；
VIX（SPX 30 天 IV）是 SPY ATM IV 的标准公开代理，CBOE 提供 1990 至今全量日线（OHLC）。
**口径修正**：VIX 含 OTM put 偏斜，结构性**高于** ATM IV 约 2–4 点（`--gap`），故
  「ATM IV ≥ G」 ≈ 「VIX ≥ G + gap」。
脚本对每个 VIX 阈值标注其隐含的 ATM IV（=阈值−gap），并按收盘/盘中高两种口径统计。

纯标准库（urllib/csv/datetime），可在本机或 HK 宿主机直接跑，不依赖 tigeropen/pandas。

用法：
  # 1) 下载数据（CBOE 官方，约 0.5MB）
  python3 -m option_bot.backtest.iv_gate_freq --download
  # 2) 跑统计（默认阈值 20,22,24,25；gap=4 → 标注隐含 ATM IV）
  python3 -m option_bot.backtest.iv_gate_freq --csv VIX_History.csv --gap 4
  # 自定义阈值/历史窗口/累计月数
  python3 -m option_bot.backtest.iv_gate_freq --thresholds 22,24 --windows 1,3,5 --cum-months 1,3,6
"""
import argparse
import csv
import datetime as dt
import sys
import urllib.request
from collections import defaultdict

CBOE_URL = 'https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'


def download(path):
    """从 CBOE 拉取 VIX 全量日线 CSV 到 path。"""
    print(f'下载 {CBOE_URL} → {path} …', file=sys.stderr)
    urllib.request.urlretrieve(CBOE_URL, path)


def load(path):
    """读 VIX_History.csv → 排序后的 [(date, high, close)]。坏行跳过。"""
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                d = dt.datetime.strptime(r['DATE'], '%m/%d/%Y').date()
                rows.append((d, float(r['HIGH']), float(r['CLOSE'])))
            except (ValueError, KeyError, TypeError):
                continue
    rows.sort()
    return rows


def window(rows, years):
    """取最近 years 年（None=全史）。闰日安全：replace 失败则按 365.25 天回退。"""
    if years is None or not rows:
        return rows
    last = rows[-1][0]
    try:
        cut = last.replace(year=last.year - years)
    except ValueError:                       # 2/29 等边界
        cut = last - dt.timedelta(days=round(365.25 * years))
    return [x for x in rows if x[0] >= cut]


def stats(sub, thr):
    """某窗口、某阈值的触达统计。返回 dict。"""
    n = len(sub) or 1
    close_hit = sum(1 for _, h, c in sub if c >= thr)
    high_hit = sum(1 for _, h, c in sub if h >= thr)
    months = defaultdict(bool)               # 每月是否盘中摸到一次（=那月有没有进场机会）
    for d, h, c in sub:
        months[(d.year, d.month)] |= (h >= thr)
    mtot = len(months) or 1
    mhit = sum(1 for v in months.values() if v)
    return {'n': len(sub), 'close_pct': close_hit / n * 100, 'high_pct': high_hit / n * 100,
            'month_hit': mhit, 'month_tot': mtot, 'month_pct': mhit / mtot * 100}


def cum_at_least_once(month_pct, n_months):
    """按月独立近似：n 个月内至少出现一次窗口的概率（成簇会使实际更集中，此为粗估）。"""
    p = month_pct / 100.0
    return (1 - (1 - p) ** n_months) * 100.0


def parse_list(s, cast):
    return [cast(x) for x in s.split(',') if x.strip()]


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog='python3 -m option_bot.backtest.iv_gate_freq',
        description='IV 入场闸触发频率回测（VIX 代理 SPY ATM IV；统计 ≥阈值 的经验概率）')
    ap.add_argument('--csv', default='VIX_History.csv', help='VIX 历史 CSV 路径（默认 ./VIX_History.csv）')
    ap.add_argument('--download', action='store_true', help='先从 CBOE 下载到 --csv 再统计')
    ap.add_argument('--thresholds', default='20,22,24,25', help='VIX 阈值列表（逗号分隔）')
    ap.add_argument('--gap', type=float, default=4.0,
                    help='VIX 高于 ATM IV 的点数（偏斜溢价）；标注隐含 ATM IV=阈值−gap。默认 4')
    ap.add_argument('--windows', default='full,5,3,1',
                    help='历史窗口（年；full=全史，逗号分隔）。默认 full,5,3,1')
    ap.add_argument('--cum-months', default='3,6,12', help='累计"至少一次"的月数（逗号分隔）')
    a = ap.parse_args(argv)

    if a.download:
        download(a.csv)
    try:
        rows = load(a.csv)
    except FileNotFoundError:
        print(f"找不到 {a.csv}；先用 --download 下载，或用 --csv 指定路径。", file=sys.stderr)
        return 2
    if not rows:
        print(f"{a.csv} 无有效数据。", file=sys.stderr)
        return 2

    thrs = parse_list(a.thresholds, float)
    wins = [(None if w == 'full' else int(w)) for w in a.windows.split(',') if w.strip()]
    cmonths = parse_list(a.cum_months, int)

    print(f"数据: {rows[0][0]} → {rows[-1][0]}  共 {len(rows)} 交易日（CBOE VIX 日线）")
    print(f"口径: 「ATM IV ≥ G」≈「VIX ≥ G+{a.gap:g}」（VIX 含 put 偏斜，高于 ATM IV）\n")

    for w in wins:
        sub = window(rows, w)
        title = '全史(1990–)' if w is None else f'近{w}年'
        print(f"=== {title}  (n={len(sub)}) ===")
        print(f"{'VIX阈值':>7} | {'≈ATM IV':>7} | {'收盘≥':>7} | {'盘中摸到≥':>9} | {'有机会月份':>16}")
        for thr in thrs:
            s = stats(sub, thr)
            atm = thr - a.gap
            print(f"{thr:>7g} | {atm:>6g}% | {s['close_pct']:>6.1f}% | {s['high_pct']:>8.1f}% | "
                  f"{s['month_pct']:>6.1f}% ({s['month_hit']}/{s['month_tot']}月)")
        print()

    # 累计"至少一次"——用最近窗口(取 wins 里最小的非 full 年数；否则全史)的月度命中率
    base_years = min([w for w in wins if w is not None], default=None)
    base = window(rows, base_years)
    base_name = '全史' if base_years is None else f'近{base_years}年'
    print(f"=== 累计「至少一次进场窗口」（基于{base_name}月度命中率，按月独立粗估）===")
    print(f"{'VIX阈值':>7} | {'≈ATM IV':>7} | " + ' | '.join(f'{m}月内' for m in cmonths))
    for thr in thrs:
        mp = stats(base, thr)['month_pct']
        cells = ' | '.join(f"{cum_at_least_once(mp, m):>5.0f}%" for m in cmonths)
        print(f"{thr:>7g} | {thr - a.gap:>6g}% | {cells}")

    d, hi, cl = rows[-1]
    avg20 = sum(c for _, _, c in rows[-20:]) / min(20, len(rows))
    print(f"\n当前水位: VIX 收 {cl:.1f} / 盘中高 {hi:.1f}（{d}）| 近20日收盘均值 {avg20:.1f}")
    print("注：VIX→ATM IV 的 gap 用单点近似（含噪声），方向(VIX>ATM)确定、幅度按区间看；"
          "机会高度成簇于波动率脉冲，平静段会空等。")
    return 0


if __name__ == '__main__':
    sys.exit(main())
