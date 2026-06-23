# -*- coding: utf-8 -*-
"""已平仓交易盈亏统计（纯逻辑，无 SQL/SDK，易单测）。

数据来自 trades 表的 OPEN/CLOSE 行：同一 (account, identifier) 按 ts 升序
FIFO 配对成单笔 round-trip。设计：docs/design/2026-06-23-trade-history-stats.md
"""
from collections import defaultdict, deque

OPTION_MULTIPLIER = 100  # 美股期权乘数；本 bot 全为期权


def pair_round_trips(trades, multiplier=OPTION_MULTIPLIER):
    """把（已按 ts 升序的）trades 行配对成 round-trips。

    每笔含：account/identifier/symbol/direction/qty/open_ts/close_ts/
            open_price/close_price/reason/pnl_percent/pnl_amount。
    未配到 CLOSE 的 OPEN（仍持仓）不产出；CLOSE 无对应 OPEN 则跳过。
    """
    open_q = defaultdict(deque)
    rts = []
    for t in trades:
        key = (t.get('account'), t.get('identifier'))
        action = t.get('action')
        if action == 'OPEN':
            open_q[key].append(t)
        elif action == 'CLOSE':
            if not open_q[key]:
                continue  # CLOSE 无 OPEN，跳过
            o = open_q[key].popleft()
            op, cp = o.get('price'), t.get('price')
            qty = t.get('qty') or o.get('qty') or 0
            pnl_amt = None
            if op is not None and cp is not None:
                pnl_amt = round((cp - op) * qty * multiplier, 2)
            rts.append({
                'account': t.get('account'),
                'identifier': t.get('identifier'),
                'symbol': t.get('symbol'),
                'direction': o.get('direction') or t.get('direction'),
                'qty': qty,
                'open_ts': o.get('ts'),
                'close_ts': t.get('ts'),
                'open_price': op,
                'close_price': cp,
                'reason': t.get('reason'),
                'pnl_percent': t.get('pnl_percent'),
                'pnl_amount': pnl_amt,
            })
    return rts


def filter_by_close_ts(rts, start_ts=None, end_ts=None):
    """按平仓时间过滤（已实现口径）：start 闭、end 开 [start, end)。"""
    out = []
    for r in rts:
        c = r.get('close_ts')
        if c is None:
            continue
        if start_ts is not None and c < start_ts:
            continue
        if end_ts is not None and c >= end_ts:
            continue
        out.append(r)
    return out


def summarize(rts):
    """汇总：笔数/胜负/胜率/总盈亏$/平均盈亏%/最大盈/最大亏。"""
    n = len(rts)
    amts = [r['pnl_amount'] for r in rts if r.get('pnl_amount') is not None]
    pcts = [r['pnl_percent'] for r in rts if r.get('pnl_percent') is not None]
    wins = [a for a in amts if a > 0]
    losses = [a for a in amts if a < 0]
    return {
        'count': n,
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': round(len(wins) / n, 4) if n else 0.0,
        'total_pnl_amount': round(sum(amts), 2) if amts else 0.0,
        'avg_pnl_percent': round(sum(pcts) / len(pcts), 2) if pcts else 0.0,
        'max_win': round(max(amts), 2) if amts else 0.0,
        'max_loss': round(min(amts), 2) if amts else 0.0,
    }


def downsample(rows, max_points=1000):
    """点过多时均匀抽样到 <= max_points（始终保留最后一点）。"""
    n = len(rows)
    if max_points <= 0 or n <= max_points:
        return rows
    step = (n + max_points - 1) // max_points  # ceil
    out = rows[::step]
    if out and out[-1] is not rows[-1]:
        out.append(rows[-1])
    return out


def equity_curve(rts):
    """按平仓时间升序累计 pnl_amount，返回 [{ts, cum_pnl}]。"""
    s = sorted([r for r in rts if r.get('close_ts') is not None],
               key=lambda r: r['close_ts'])
    cum = 0.0
    out = []
    for r in s:
        if r.get('pnl_amount') is not None:
            cum += r['pnl_amount']
        out.append({'ts': r['close_ts'], 'cum_pnl': round(cum, 2)})
    return out
