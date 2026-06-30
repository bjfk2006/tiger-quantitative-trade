# -*- coding: utf-8 -*-
"""铁鹰盈亏回测器（设计：docs/design/2026-06-29-condor-pnl-backtester.md）。

逐历史日重放 live 的开仓/持仓/出场决策，输出真·盈亏统计。**纯编排，复用 live 纯函数**
（build_condor / net_credit / condor_max_loss / size_by_max_loss / condor_pnl_percent /
build_condor_close_strategy / passes_entry_gate / atm_iv_live / implied_spot / enrich_greeks /
iv_percentile），不重写任何策略判定。历史链只有 bid/ask（无 greeks）→ 走合成路径（同 live）。

run_condor_backtest 入参为已载入的数据（不碰 dolt），便于单测；CLI 在 __main__.py --condor。
"""
import datetime as _dt
import math

from option_bot.domain.models import CloseReason
from option_bot.strategy.close_strategies import (StrategyContext,
                                                  build_condor_close_strategy)
from option_bot.strategy.condor import (atm_iv_live, bs_price, build_condor,
                                        condor_max_loss, condor_pnl_percent,
                                        enrich_greeks, greeks_missing,
                                        implied_spot, net_credit,
                                        passes_entry_gate, size_by_max_loss)
from option_bot.strategy.iv_history import iv_percentile


def _d(s):
    return _dt.datetime.strptime(str(s)[:10], '%Y-%m-%d').date()


def _normalize(chain_rows):
    """dolt 行 → (dates 升序, snap_by_date{date:[row]}, quote_idx{(date,id):{bid_price,ask_price}})。

    row 归一为 live 函数所需键：identifier/put_call/strike/expiration/bid_price/ask_price/delta=0。
    identifier = 'PC|EXP|STRIKE'（唯一标识一张合约，跨日查腿报价用）。
    """
    snap, qidx = {}, {}
    for r in chain_rows:
        try:
            d = str(r['date'])[:10]
            exp = str(r['expiration'])[:10]
            k = float(r['strike'])
        except (KeyError, TypeError, ValueError):
            continue
        pc = str(r.get('put_call', '')).upper()
        if pc not in ('CALL', 'PUT'):
            continue
        bid = r.get('bid') if r.get('bid') is not None else r.get('bid_price')
        ask = r.get('ask') if r.get('ask') is not None else r.get('ask_price')
        ident = f"{pc}|{exp}|{k:g}"
        row = {'identifier': ident, 'put_call': pc, 'strike': k, 'expiration': exp,
               'bid_price': bid, 'ask_price': ask, 'delta': 0.0, 'implied_vol': 0.0}
        snap.setdefault(d, []).append(row)
        qidx[(d, ident)] = {'bid_price': bid, 'ask_price': ask}
    return sorted(snap), snap, qidx


def _pick_expiry(rows_today, today, target_dte, dte_exit):
    """当日可用到期中选最接近 target_dte 且 DTE>dte_exit 的。无则 None。"""
    best, bd = None, None
    for e in {x['expiration'] for x in rows_today}:
        dte = (_d(e) - today).days
        if dte <= dte_exit:
            continue
        dist = abs(dte - target_dte)
        if best is None or dist < bd:
            best, bd = e, dist
    return best


def _daily_iv(rows_today, today, target_dte, dte_exit, r):
    """当日 ~target_dte 到期的 ATM 活 IV（平价反推现价 + BS 反推），与 live 一致。"""
    exp = _pick_expiry(rows_today, today, target_dte, dte_exit)
    if not exp:
        return None
    exp_rows = [x for x in rows_today if x['expiration'] == exp]
    spot = implied_spot(exp_rows)
    if spot is None:
        return None
    dte = (_d(exp) - today).days
    return atm_iv_live(exp_rows, spot, dte / 365.0, r)


def run_condor_backtest(chain_rows, cfg, *, multiplier=100, entry_to=None,
                        independent=False, risk_free=0.04):
    """回测铁鹰盈亏。返回 {summary, trades}。

    chain_rows: [{date, expiration, strike, put_call(Call/Put), bid, ask}]。
    单仓顺序（默认，镜像 live"平了再开"）；independent=True 则每日独立入场（可重叠，看胜率分布）。
    """
    r = cfg.condor_risk_free or risk_free
    dates, snap, qidx = _normalize(chain_rows)
    if not dates:
        return {'summary': _summarize([], multiplier), 'trades': []}
    target_dte, dte_exit = cfg.condor_target_dte, cfg.condor_dte_exit
    lookback, min_hist = cfg.condor_iv_rank_lookback_days, cfg.condor_iv_rank_min_history
    entry_to = entry_to or dates[-1]

    iv_by_date = {d: _daily_iv(snap[d], _d(d), target_dte, dte_exit, r) for d in dates}
    n = len(dates)
    trades = []
    i = 0
    while i < n:
        d = dates[i]
        if d > entry_to:
            break
        iv = iv_by_date[d]
        hist = [iv_by_date[dates[j]] for j in range(max(0, i - lookback), i)
                if iv_by_date[dates[j]] is not None]
        ivp = iv_percentile(hist, iv)
        ok, _reason = passes_entry_gate(
            iv, cfg.condor_min_iv, True, False, mode=cfg.condor_iv_gate_mode, ivp=ivp,
            min_rank=cfg.condor_min_iv_rank, rank_floor=cfg.condor_iv_rank_floor,
            history_ok=len(hist) >= min_hist)
        if not ok:
            i += 1
            continue
        tr = _try_one_trade(d, i, dates, snap, qidx, cfg, r, iv, ivp, multiplier)
        if tr is None:
            i += 1
            continue
        trades.append(tr)
        i = (i + 1) if independent else (tr['_exit_i'] + 1)
    return {'summary': _summarize(trades, multiplier), 'trades': [_clean(t) for t in trades]}


def _try_one_trade(d, i, dates, snap, qidx, cfg, r, iv, ivp, multiplier):
    """在日 d 建仓并持有到出场。返回 trade dict（含内部 _exit_i）或 None（建不成/无出场行情）。"""
    today = _d(d)
    exp = _pick_expiry(snap[d], today, cfg.condor_target_dte, cfg.condor_dte_exit)
    if not exp:
        return None
    exp_rows = [dict(x) for x in snap[d] if x['expiration'] == exp]   # 复制：enrich 会就地改
    spot = implied_spot(exp_rows)
    if spot is None:
        return None
    dte0 = (_d(exp) - today).days
    if greeks_missing(exp_rows):
        enrich_greeks(exp_rows, spot, iv, dte0 / 365.0, r)
    calls = [x for x in exp_rows if x['put_call'] == 'CALL']
    puts = [x for x in exp_rows if x['put_call'] == 'PUT']
    structure = build_condor(calls, puts, cfg.condor_short_delta, cfg.condor_wing_width, getattr(cfg, "condor_side", "both"))
    if not structure:
        return None
    legs = structure['legs']
    qd = {lg['identifier']: qidx.get((d, lg['identifier'])) for lg in legs}
    credit = net_credit(legs, qd, 'conservative', closing=False)
    if credit is None or credit <= 0:
        return None
    maxloss = condor_max_loss(structure['put_width'], structure['call_width'], credit)
    qty = size_by_max_loss(maxloss, multiplier, cfg.condor_account_equity,
                           cfg.condor_max_loss_pct, cfg.max_qty)
    if qty < 1:
        return None

    strat = build_condor_close_strategy(cfg)
    exit_i = exit_cost = reason = None
    peak = 0.0
    last_valid = None        # (idx, cc) 最后一个有效平仓成本，供到期兜底结算
    j = i + 1
    n = len(dates)
    while j < n and _d(dates[j]) <= _d(exp):
        qj = {lg['identifier']: qidx.get((dates[j], lg['identifier'])) for lg in legs}
        cc = net_credit(legs, qj, 'mid', closing=True)
        dte_j = (_d(exp) - _d(dates[j])).days
        if cc is None:
            j += 1
            continue          # 缺腿报价 → 持有跳过
        last_valid = (j, cc)
        pnl_pct = condor_pnl_percent(credit, cc)
        if pnl_pct is not None and pnl_pct > peak:
            peak = pnl_pct
        rsn = strat.decide(StrategyContext(pnl_percent=pnl_pct, minutes_to_close=None, dte=dte_j))
        if rsn is not None:
            exit_i, exit_cost, reason = j, cc, rsn
            break
        j += 1
    if exit_i is None:
        if last_valid is None:
            return None       # 建仓后完全无平仓行情（数据缺口）→ 跳过此笔
        exit_i, exit_cost, reason = last_valid[0], last_valid[1], CloseReason.TIME_FORCE_CLOSE

    pnl_ps = credit - exit_cost
    return {
        '_exit_i': exit_i,
        'entry_date': d, 'exit_date': dates[exit_i], 'expiration': exp,
        'strikes': [lg['strike'] for lg in legs],
        'sides': [f"{lg['side']}{lg['put_call'][0]}{lg['strike']:g}" for lg in legs],
        'entry_credit': round(credit, 4), 'exit_cost': round(exit_cost, 4),
        'qty': qty, 'pnl_per_share': round(pnl_ps, 4),
        'pnl_usd': round(pnl_ps * qty * multiplier, 2),
        'pnl_pct_credit': round(condor_pnl_percent(credit, exit_cost), 1),
        'pnl_pct_maxloss': round(pnl_ps / maxloss * 100, 1) if maxloss > 0 else None,
        'max_loss': round(maxloss, 4), 'reason': reason.value if reason else None,
        'days_held': (_d(dates[exit_i]) - today).days, 'peak_pct_credit': round(peak, 1),
        'iv_entry': round(iv, 4) if iv is not None else None,
        'ivp_entry': round(ivp, 1) if ivp is not None else None,
    }


def _clean(t):
    return {k: v for k, v in t.items() if not k.startswith('_')}


def _summarize(trades, multiplier):
    n = len(trades)
    if n == 0:
        return {'count': 0}
    pnls = [t['pnl_usd'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = sum(losses)
    # 顺序权益曲线最大回撤（单仓顺序时最有意义）
    eq = 0.0
    peak_eq = 0.0
    max_dd = 0.0
    for p in pnls:
        eq += p
        peak_eq = max(peak_eq, eq)
        max_dd = min(max_dd, eq - peak_eq)
    reasons = {}
    for t in trades:
        reasons[t['reason']] = reasons.get(t['reason'], 0) + 1
    return {
        'count': n,
        'win_rate': round(len(wins) / n * 100, 1),
        'total_pnl_usd': round(sum(pnls), 2),
        'avg_pnl_usd': round(sum(pnls) / n, 2),
        'avg_pnl_pct_credit': round(sum(t['pnl_pct_credit'] for t in trades) / n, 1),
        'max_win_usd': round(max(pnls), 2),
        'max_loss_usd': round(min(pnls), 2),
        'avg_days_held': round(sum(t['days_held'] for t in trades) / n, 1),
        'max_drawdown_usd': round(max_dd, 2),
        'profit_factor': round(gross_win / abs(gross_loss), 2) if gross_loss < 0 else None,
        'reasons': reasons,
    }


# ==================== B 方案：BS 重定价回测（不依赖 option_chain）====================
# 设计：docs/design/2026-06-29-condor-bs-repriced-backtester.md
# 只用连续的「日收盘价 + 波动率指数(VIX/VXN)」合成整条铁鹰 P&L；模型价、平 IV 无 skew，
# 会低估下行/尾部损失（详见设计 §6），仅作相对比较，非精确实盘损益。

def _strike_grid(spot, spacing, span_pct=0.30):
    """以 spot 为中心、按 spacing 铺 ±span_pct 的合成行权价网格。"""
    lo, hi = spot * (1 - span_pct), spot * (1 + span_pct)
    k = math.floor(lo / spacing) * spacing
    out = []
    while k <= hi:
        if k > 0:
            out.append(round(k, 4))
        k += spacing
    return out


def _leg_price(spot, strike, t_years, iv, r, pc):
    """BS 理论价；t≤0 或 BS 不可得 → 内在价。"""
    if t_years > 0:
        p = bs_price(spot, strike, t_years, iv, r, pc)
        if p is not None:
            return p
    return max(0.0, spot - strike) if pc == 'CALL' else max(0.0, strike - spot)


def _bs_qbi(legs, spot, t_years, iv, r):
    """给四腿构造 BS 定价的 quote_by_id（bid=ask=理论价，故 mid=理论价）。"""
    q = {}
    for lg in legs:
        px = _leg_price(spot, lg['strike'], t_years, iv, r, lg['put_call'])
        q[lg['identifier']] = {'bid_price': px, 'ask_price': px}
    return q


def run_condor_bs_backtest(spot_series, iv_series, cfg, *, multiplier=100, entry_to=None,
                           independent=False, strike_spacing=1.0, slippage=0.0, risk_free=0.04):
    """BS 重定价铁鹰回测。spot_series/iv_series: {date(YYYY-MM-DD): float}（iv 为小数）。

    单仓顺序（默认，镜像 live）；independent=True 每日独立入场。返回 {summary, trades}。
    """
    r = cfg.condor_risk_free or risk_free
    dates = sorted(set(spot_series) & set(iv_series))
    if not dates:
        return {'summary': _summarize([], multiplier), 'trades': []}
    target_dte, dte_exit = cfg.condor_target_dte, cfg.condor_dte_exit
    lookback, min_hist = cfg.condor_iv_rank_lookback_days, cfg.condor_iv_rank_min_history
    entry_to = entry_to or dates[-1]
    n = len(dates)
    trades = []
    i = 0
    while i < n:
        d = dates[i]
        if d > entry_to:
            break
        iv_t = iv_series[d]
        hist = [iv_series[dates[j]] for j in range(max(0, i - lookback), i)]
        ivp = iv_percentile(hist, iv_t)
        ok, _reason = passes_entry_gate(
            iv_t, cfg.condor_min_iv, True, False, mode=cfg.condor_iv_gate_mode, ivp=ivp,
            min_rank=cfg.condor_min_iv_rank, rank_floor=cfg.condor_iv_rank_floor,
            history_ok=len(hist) >= min_hist)
        if not ok:
            i += 1
            continue
        tr = _try_one_bs_trade(i, dates, spot_series, iv_series, cfg, r, ivp,
                               multiplier, strike_spacing, slippage)
        if tr is None:
            i += 1
            continue
        trades.append(tr)
        i = (i + 1) if independent else (tr['_exit_i'] + 1)
    return {'summary': _summarize(trades, multiplier), 'trades': [_clean(t) for t in trades]}


def _try_one_bs_trade(i, dates, spot_series, iv_series, cfg, r, ivp, multiplier,
                      strike_spacing, slippage):
    """在日 i 用 BS 合成建仓并逐日重定价持有到出场。返回 trade dict（含 _exit_i）或 None。"""
    d = dates[i]
    today = _d(d)
    s0, iv0 = spot_series[d], iv_series[d]
    expiry = today + _dt.timedelta(days=cfg.condor_target_dte)
    t0 = cfg.condor_target_dte / 365.0
    rows = [{'identifier': f'{pc}|{k:g}', 'put_call': pc, 'strike': k, 'delta': 0.0}
            for k in _strike_grid(s0, strike_spacing) for pc in ('CALL', 'PUT')]
    enrich_greeks(rows, s0, iv0, t0, r)
    calls = [x for x in rows if x['put_call'] == 'CALL']
    puts = [x for x in rows if x['put_call'] == 'PUT']
    structure = build_condor(calls, puts, cfg.condor_short_delta, cfg.condor_wing_width, getattr(cfg, "condor_side", "both"))
    if not structure:
        return None
    legs = structure['legs']
    credit = net_credit(legs, _bs_qbi(legs, s0, t0, iv0, r), 'mid', closing=False) - slippage
    if credit is None or credit <= 0:
        return None
    maxloss = condor_max_loss(structure['put_width'], structure['call_width'], credit)
    qty = size_by_max_loss(maxloss, multiplier, cfg.condor_account_equity,
                           cfg.condor_max_loss_pct, cfg.max_qty)
    if qty < 1:
        return None

    strat = build_condor_close_strategy(cfg)
    exit_i = exit_cost = reason = None
    peak = 0.0
    last_valid = None
    n = len(dates)
    j = i + 1
    while j < n:
        dte_j = (expiry - _d(dates[j])).days
        if dte_j < 0:
            break
        cc = net_credit(legs, _bs_qbi(legs, spot_series[dates[j]], max(dte_j, 0) / 365.0,
                                      iv_series[dates[j]], r), 'mid', closing=True) + slippage
        last_valid = (j, cc)
        pnl_pct = condor_pnl_percent(credit, cc)
        if pnl_pct is not None and pnl_pct > peak:
            peak = pnl_pct
        rsn = strat.decide(StrategyContext(pnl_percent=pnl_pct, minutes_to_close=None, dte=dte_j))
        if rsn is not None:
            exit_i, exit_cost, reason = j, cc, rsn
            break
        j += 1
    if exit_i is None:
        if last_valid is None:
            return None
        exit_i, exit_cost, reason = last_valid[0], last_valid[1], CloseReason.TIME_FORCE_CLOSE

    pnl_ps = credit - exit_cost
    return {
        '_exit_i': exit_i,
        'entry_date': d, 'exit_date': dates[exit_i], 'expiration': expiry.isoformat(),
        'strikes': [lg['strike'] for lg in legs],
        'sides': [f"{lg['side']}{lg['put_call'][0]}{lg['strike']:g}" for lg in legs],
        'entry_credit': round(credit, 4), 'exit_cost': round(exit_cost, 4),
        'qty': qty, 'pnl_per_share': round(pnl_ps, 4),
        'pnl_usd': round(pnl_ps * qty * multiplier, 2),
        'pnl_pct_credit': round(condor_pnl_percent(credit, exit_cost), 1),
        'pnl_pct_maxloss': round(pnl_ps / maxloss * 100, 1) if maxloss > 0 else None,
        'max_loss': round(maxloss, 4), 'reason': reason.value if reason else None,
        'days_held': (_d(dates[exit_i]) - today).days, 'peak_pct_credit': round(peak, 1),
        'spot_entry': round(s0, 2), 'iv_entry': round(iv0, 4),
        'ivp_entry': round(ivp, 1) if ivp is not None else None,
    }
