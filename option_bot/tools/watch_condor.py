# -*- coding: utf-8 -*-
"""只读盯盘：在场铁鹰的标的现价离两侧短腿的距离（开盘后人工/定时巡检用）。

跑法（容器内，源码已打进镜像）：
  python -m option_bot.tools.watch_condor          # 默认读 OBOT_SHADOW_FILE
  python -m option_bot.tools.watch_condor --file /app/data/shadow_condor.json

现价取法：paper 账户**无美股 stock-brief 行情权限**（get_underlying_price 会被拒），
故走 bot 同款 `implied_spot`（看跌看涨平价）从期权链反推——与引擎/影子口径一致。
纯只读：只拉行情、读影子状态文件，不下单、不写状态。
"""
import argparse
import json

from option_bot.shadow import SHADOW_FILE, build_md
from option_bot.strategy.condor import implied_spot


def _short_strikes(legs):
    """从影子 entry 的 4 条腿里取 short put / short call 行权价。"""
    sp = sc = None
    for leg in legs:
        if leg.get('side') == 'SELL' and leg.get('put_call') == 'PUT':
            sp = leg.get('strike')
        if leg.get('side') == 'SELL' and leg.get('put_call') == 'CALL':
            sc = leg.get('strike')
    return sp, sc


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


def watch(path=SHADOW_FILE):
    """返回一段可读盯盘文本（无在场铁鹰时返回说明）。"""
    with open(path, 'r', encoding='utf-8') as f:
        st = json.load(f)
    if st.get('status') != 'TRACKING' or st.get('outcome') is not None or 'entry' not in st:
        return f"当前无在场铁鹰（status={st.get('status')}, outcome={st.get('outcome')}），跳过。"
    e = st['entry']
    sp, sc = _short_strikes(e.get('legs', []))
    if sp is None or sc is None:
        return "影子 entry 缺少 short put/call 腿，无法盯盘。"
    spot = _spot_via_chain(build_md(), e['symbol'], e)
    if not spot:
        return "无法反推现价（期权链不可用），跳过本次。"

    mid = (sp + sc) / 2.0
    d_put, d_call = spot - sp, sc - spot          # >0 即在短腿安全侧
    buf_put, buf_call = d_put / spot * 100, d_call / spot * 100
    near = 'CALL(上)' if d_call < d_put else 'PUT(下)'

    # 最新浮盈亏（trajectory 尾）
    traj = st.get('trajectory') or []
    pnl_txt = ''
    if traj:
        last = traj[-1]
        pnl_txt = f" | 浮盈亏 {last.get('pnl_pct_of_credit')}% (DTE {last.get('dte')})"
    armed = (e.get('strategy_state') or {}).get('armed')

    # 预警：任一缓冲 <2% 视为接近击穿
    warns = []
    if buf_put < 2.0:
        warns.append(f"⚠ short put 缓冲仅 {buf_put:+.2f}%")
    if buf_call < 2.0:
        warns.append(f"⚠ short call 缓冲仅 {buf_call:+.2f}%")
    if d_put < 0 or d_call < 0:
        warns.append("⚠ 已有短腿被击穿")
    if armed:
        warns.append("● 移动止盈已 armed")

    lines = [
        f"{e['symbol']} 现价≈{spot:.2f}(链反推) | 开仓 {e.get('spot'):.2f} | "
        f"区间 [{sp:.0f},{sc:.0f}] 中点 {mid:.1f}{pnl_txt}",
        f"  距 short put {sp:.0f}:  {d_put:+.2f} ({buf_put:+.2f}%)  {'!!被击穿' if d_put < 0 else '安全'}",
        f"  距 short call {sc:.0f}: {d_call:+.2f} ({buf_call:+.2f}%)  {'!!被击穿' if d_call < 0 else '安全'}",
        f"  更近一侧: {near} | 现价偏 {'call(上)' if spot > mid else 'put(下)'}侧",
    ]
    if warns:
        lines.append("  " + " | ".join(warns))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description='在场铁鹰现价-短腿距离盯盘（只读）')
    ap.add_argument('--file', default=SHADOW_FILE, help='影子状态文件路径')
    args = ap.parse_args()
    print(watch(args.file))


if __name__ == '__main__':
    main()
