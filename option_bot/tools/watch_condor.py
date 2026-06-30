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
import json

from option_bot.shadow import SHADOW_FILE, build_md
from option_bot.strategy.condor import implied_spot
from option_bot.web.strategy_status import compute_condor_view


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
    """把 compute_condor_view 的 dict 渲染成可读文本。"""
    sp, sc, spot = view['put_strike'], view['call_strike'], view['spot']
    head = (f"{view['symbol']} 现价≈{_fmt(spot)}(链反推) | 开仓 {_fmt(view['open_spot'])} | "
            f"区间 [{_fmt(sp, '{:.0f}')},{_fmt(sc, '{:.0f}')}] 中点 {_fmt(view['mid_strike'], '{:.1f}')}")
    if view['pnl_pct'] is not None:
        head += f" | 浮盈亏 {view['pnl_pct']}% (DTE {view['dte']})"
    lines = [head]
    if spot is not None:
        lines.append(f"  距 short put {sp:.0f}:  {view['d_put']:+.2f} ({view['buf_put_pct']:+.2f}%)  "
                     f"{'!!被击穿' if view['d_put'] < 0 else '安全'}")
        lines.append(f"  距 short call {sc:.0f}: {view['d_call']:+.2f} ({view['buf_call_pct']:+.2f}%)  "
                     f"{'!!被击穿' if view['d_call'] < 0 else '安全'}")
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
    if view['put_strike'] is None or view['call_strike'] is None:
        return "影子 entry 缺少 short put/call 腿，无法盯盘。"
    return _render(view)


def main():
    ap = argparse.ArgumentParser(description='在场铁鹰现价-短腿距离盯盘（只读）')
    ap.add_argument('--file', default=SHADOW_FILE, help='影子状态文件路径')
    args = ap.parse_args()
    print(watch(args.file))


if __name__ == '__main__':
    main()
