# -*- coding: utf-8 -*-
"""只读盯盘：在场铁鹰的标的现价离两侧短腿的距离（开盘后人工/定时巡检用）。

跑法（容器内，源码已打进镜像）：
  python -m option_bot.tools.watch_condor          # 默认读 OBOT_SHADOW_FILE
  python -m option_bot.tools.watch_condor --file /app/data/shadow_condor.json

现价取法：paper 账户**无美股 stock-brief 行情权限**（get_underlying_price 会被拒），
故走 bot 同款 `implied_spot`（看跌看涨平价）从期权链反推——与引擎/影子口径一致。
派生字段（点差/theta/短腿距离）复用 web.strategy_status.compute_condor_view，CLI 与看板同源。
纯只读：只拉行情、读影子状态文件，不下单、不写状态。
"""
import argparse
import datetime
import json
import os

import pytz

from option_bot.shadow import SHADOW_FILE, build_md
from option_bot.strategy.condor import (condor_pnl_percent, implied_spot,
                                        net_credit)
from option_bot.tools.condor_progress import _ivp_now
from option_bot.web.strategy_status import compute_condor_view


def _engine_state_path():
    base = os.environ.get('OBOT_STATE_FILE', '/app/data/option_bot_state.json')
    return base.rsplit('.json', 1)[0] + '_condor.json'


def _dte_from_expiry(expiry):
    if not expiry:
        return None
    s = str(expiry).replace('-', '')
    try:
        exp = datetime.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (ValueError, IndexError):
        return None
    today = datetime.datetime.now(pytz.timezone('America/New_York')).date()
    return (exp - today).days


def _spot_via_chain(md, symbol, entry):
    """期权链平价反推现价；兼容 expiry_date(2026-08-07) / expiry(20260807) 两种格式。"""
    for exp in (entry.get('expiry_date'), entry.get('expiry')):
        if not exp:
            continue
        try:
            spot = implied_spot(md.get_chain(symbol, exp))
            if spot:
                return spot
        except Exception:
            continue
    return None


def _fmt(v, pat='{:.2f}', dash='—'):
    return pat.format(v) if v is not None else dash


def _render(view):
    """把 compute_condor_view 的 dict 渲染成可读文本（兼容单边 bear call / bull put）。"""
    sp, sc, spot = view['put_strike'], view['call_strike'], view['spot']
    side = view.get('side')
    tag = {'call': ' [bear call]', 'put': ' [bull put]'}.get(side, '')
    if side == 'both':
        rng = f"区间 [{_fmt(sp, '{:.0f}')},{_fmt(sc, '{:.0f}')}] 中点 {_fmt(view['mid_strike'], '{:.1f}')}"
    elif side == 'call':
        rng = f"short call {_fmt(sc, '{:.0f}')}"
    else:
        rng = f"short put {_fmt(sp, '{:.0f}')}"
    head = (f"{view['symbol']}{tag} 现价≈{_fmt(spot)}(链反推) | 开仓 {_fmt(view['open_spot'])} | {rng}")
    if view['pnl_pct'] is not None:
        head += f" | 浮盈亏 {view['pnl_pct']}% (DTE {view['dte']})"
    lines = [head]
    if spot is not None:
        if view['d_put'] is not None:
            lines.append(f"  距 short put {sp:.0f}:  {view['d_put']:+.2f} ({view['buf_put_pct']:+.2f}%)  "
                         f"{'!!被击穿' if view['d_put'] < 0 else '安全'}")
        if view['d_call'] is not None:
            lines.append(f"  距 short call {sc:.0f}: {view['d_call']:+.2f} ({view['buf_call_pct']:+.2f}%)  "
                         f"{'!!被击穿' if view['d_call'] < 0 else '安全'}")
        if side == 'both':
            lines.append(f"  更近一侧: {'CALL(上)' if view['near'] == 'call' else 'PUT(下)'} | "
                         f"现价偏 {'call(上)' if view['spot_side'] == 'call' else 'put(下)'}侧")
    if view['gap0_pct'] is not None:
        ec, mc = view['entry_credit'], view['mid_credit']
        spread = f"  点差: 收 {ec:.2f}(保守) vs 中间价 {mc:.2f} | 开仓缺口 {view['gap0_pct']:+.1f}%"
        if view['pnl_pct'] is not None:
            spread += f" → 现 {view['pnl_pct']:+.1f}% (theta已填 {view['theta_filled_pt']:+.1f}pt)"
        lines.append(spread)
    if view['warns']:
        lines.append("  " + " | ".join(view['warns']))
    return "\n".join(lines)


def watch(path=SHADOW_FILE):
    """返回一段可读盯盘文本（无在场铁鹰时返回说明）。"""
    with open(path, 'r', encoding='utf-8') as f:
        st = json.load(f)
    if st.get('status') != 'TRACKING' or st.get('outcome') is not None or 'entry' not in st:
        return f"当前无在场铁鹰（status={st.get('status')}, outcome={st.get('outcome')}），跳过。"
    e = st['entry']
    if e.get('symbol') is None or not e.get('legs'):
        return "影子 entry 不完整，无法盯盘。"
    spot = _spot_via_chain(build_md(), e['symbol'], e)
    if not spot:
        return "无法反推现价（期权链不可用），跳过本次。"
    traj = st.get('trajectory') or []
    view = compute_condor_view(e, traj[-1] if traj else None, spot)
    if view['put_strike'] is None and view['call_strike'] is None:
        return "影子 entry 缺少短腿，无法盯盘。"
    return _render(view)


def watch_engine(path=None):
    """盯**实盘/引擎**在场铁鹰：读引擎状态文件取腿，现取行情算 close_cost/pnl/距离（只读）。"""
    path = path or _engine_state_path()
    with open(path, 'r', encoding='utf-8') as f:
        st = json.load(f)
    if st.get('state') != 'MONITORING' or not st.get('legs') or not st.get('qty'):
        return f"当前无在场实盘铁鹰（state={st.get('state')}, qty={st.get('qty')}），跳过。"
    legs = st['legs']
    md = build_md()
    try:
        qbi = {l['identifier']: md.get_option_quote(l['identifier'], market='US') for l in legs}
    except Exception:
        return "实盘取腿行情失败，跳过本次。"
    close_cost = net_credit(legs, qbi, 'mid', closing=True)
    if close_cost is None:
        return "实盘腿行情不全，跳过本次。"
    if close_cost < 0:                       # 与引擎同护栏：负成本=脏点
        return f"实盘平仓成本异常({close_cost:.2f}<0，疑似脏报价)，跳过本次。"
    exp = str(st.get('expiry') or '')
    exp_dash = f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}" if len(exp) == 8 and '-' not in exp else exp
    spot = _spot_via_chain(md, st['symbol'], {'expiry_date': exp_dash, 'expiry': exp})
    dte = _dte_from_expiry(exp)
    entry = {'symbol': st['symbol'], 'expiry_date': exp_dash, 'legs': legs,
             'entry_credit': st.get('entry_credit'), 'mid_credit': None,
             'spot': None, 'dte0': dte, 'strategy_state': st.get('strategy_state')}
    pnl_pct = condor_pnl_percent(st.get('entry_credit'), close_cost)
    last_tick = {'close_cost': close_cost,
                 'pnl_pct_of_credit': None if pnl_pct is None else round(pnl_pct, 1),
                 'dte': dte}
    view = compute_condor_view(entry, last_tick, spot)
    out = "[实盘 " + str(st.get('account')) + "] " + _render(view)
    iv = _ivp_now(path, st.get('symbol') or 'SPY')   # iv_history 与状态文件同目录
    if iv:
        out += (f"\n  当前 IV {iv['iv']*100:.2f}% | IVP {iv['ivp']:.1f}% | IVR {iv['ivr']:.1f}%"
                f"（{iv['date']} {iv['src']}）")
    return out


def main():
    ap = argparse.ArgumentParser(description='在场铁鹰现价-短腿距离盯盘（只读）')
    ap.add_argument('--source', choices=['shadow', 'engine'], default='shadow',
                    help='shadow=影子文件(默认) / engine=实盘引擎在场仓')
    ap.add_argument('--file', default=None, help='状态文件路径（缺省按 source 自动定位）')
    args = ap.parse_args()
    if args.source == 'engine':
        print(watch_engine(args.file))
    else:
        print(watch(args.file or SHADOW_FILE))


if __name__ == '__main__':
    main()
