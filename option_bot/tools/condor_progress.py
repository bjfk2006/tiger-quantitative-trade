# -*- coding: utf-8 -*-
"""只读：铁鹰「theta 填平进度表」——把影子 trajectory 按日下采样，逐日列 pnl%/theta已填/DTE，
末尾附当前 IV/IVP。供每日开盘汇总（cron）。纯读文件、不拉行情、不下单。

跑法（容器内）：python -m option_bot.tools.condor_progress
"""
import argparse
import json
import os

from option_bot.shadow import SHADOW_FILE


def _ivp_now(shadow_path, symbol):
    """从同目录 iv_history_{symbol}.json 算 (live IV, IVP%, IVR%)；缺失返回 None。"""
    path = os.path.join(os.path.dirname(shadow_path) or '.', f'iv_history_{symbol}.json')
    try:
        d = json.load(open(path, 'r', encoding='utf-8'))
    except (OSError, ValueError):
        return None
    rows = sorted((x for x in d if 'iv' in x and 'date' in x), key=lambda x: x['date'])
    ivs = [x['iv'] for x in rows]
    if len(ivs) < 2:
        return None
    cur, hist = ivs[-1], ivs[:-1]
    d = rows  # 用排序后的最后一行做 date/src
    ivp = 100.0 * sum(1 for v in hist if v < cur) / len(hist)
    lo, hi = min(hist), max(hist)
    ivr = 100.0 * (cur - lo) / (hi - lo) if hi > lo else 0.0
    return {'iv': cur, 'ivp': ivp, 'ivr': ivr, 'date': d[-1].get('date'), 'src': d[-1].get('src')}


def progress(path=SHADOW_FILE):
    with open(path, 'r', encoding='utf-8') as f:
        st = json.load(f)
    if st.get('status') != 'TRACKING' or st.get('outcome') is not None or 'entry' not in st:
        return f"当前无在场铁鹰（status={st.get('status')}, outcome={st.get('outcome')}），跳过。"
    e = st['entry']
    ec, mc = e.get('entry_credit'), e.get('mid_credit')
    gap0 = ((ec - mc) / ec * 100.0) if ec and mc else None

    # trajectory 按 UTC 日期下采样：每天取最后一个 tick
    by_day = {}
    for t in st.get('trajectory') or []:
        day = (t.get('ts') or '')[:10]
        if day:
            by_day[day] = t                       # 后写覆盖 → 当日最后一笔

    lines = [f"铁鹰 {e.get('symbol')} {e.get('expiry_date') or e.get('expiry')} | "
             f"信用 收{ec}/中{mc} | 开仓点差缺口 {gap0:+.1f}%" if gap0 is not None
             else f"铁鹰 {e.get('symbol')}"]
    lines.append("  日期        pnl%(of credit)   theta已填   DTE")
    for day in sorted(by_day):
        t = by_day[day]
        pnl = t.get('pnl_pct_of_credit')
        filled = (pnl - gap0) if (pnl is not None and gap0 is not None) else None
        lines.append(f"  {day}   {('%+.1f' % pnl) if pnl is not None else '  —':>8}%"
                     f"      {('%+.1f' % filled + 'pt') if filled is not None else '—':>7}"
                     f"     {t.get('dte')}")

    iv = _ivp_now(path, e.get('symbol') or 'SPY')
    if iv:
        lines.append(f"  当前 IV {iv['iv']*100:.2f}% | IVP {iv['ivp']:.1f}% | IVR {iv['ivr']:.1f}% "
                     f"（{iv['date']} {iv['src']}）")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description='铁鹰 theta 填平进度表（只读）')
    ap.add_argument('--file', default=SHADOW_FILE, help='影子状态文件路径')
    print(progress(ap.parse_args().file))


if __name__ == '__main__':
    main()
