# -*- coding: utf-8 -*-
"""回测引擎（纯逻辑，无 dolt/无 SDK，易单测）。

口径（见设计 §3/§7）：
- 入场：entry 日以 ask 买入（多头市价单偏保守），entry_price=ask。
- 逐日盯盘：多头按当日 bid 估 pnl% = (bid-entry_ask)/entry_ask*100，喂策略 decide。
- 平仓：策略返回任一 CloseReason → 当日 bid 平；未触发则到期/末日强平(TIME_FORCE_CLOSE)。
- 入场当日不判仓（仅 -点差），从次日开始盯盘。
"""
import datetime
from dataclasses import dataclass, asdict
from typing import List, Optional

from option_bot.domain.models import CloseReason
from option_bot.strategy.close_strategies import StrategyContext, build_strategy


def _to_ms(date_str: str) -> int:
    d = datetime.datetime.strptime(date_str[:10], '%Y-%m-%d')
    return int(d.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)


def _date(s: str) -> datetime.date:
    return datetime.datetime.strptime(s[:10], '%Y-%m-%d').date()


def _fill_price(row: dict, fill: str) -> Optional[float]:
    bid, ask = row.get('bid'), row.get('ask')
    if fill == 'mid':
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0
    return ask  # 默认 ask 入场


@dataclass
class BacktestResult:
    entry_date: str
    exit_date: str
    entry_price: float
    exit_price: float
    pnl_percent: float
    reason: str
    peak_pnl_percent: float
    days_held: int
    closed: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


def run_backtest(series: List[dict], cfg, strategy_name: str,
                 entry_date: Optional[str] = None, fill: str = 'ask') -> Optional[BacktestResult]:
    """对单合约日线 series=[{date,bid,ask}] 跑一次回测。无有效入场返回 None。

    series 须按 date 升序；缺 bid/ask 的日已在数据层过滤。
    """
    if not series:
        return None
    rows = sorted(series, key=lambda r: r['date'])
    # 定位入场：首个 date>=entry_date 且 ask 有效的日
    start = 0
    if entry_date:
        start = next((i for i, r in enumerate(rows) if r['date'] >= entry_date), len(rows))
    entry = None
    for i in range(start, len(rows)):
        ep = _fill_price(rows[i], fill)
        if ep and ep > 0:
            entry = (i, rows[i], ep)
            break
    if entry is None:
        return None
    ei, erow, entry_price = entry
    entry_ms = _to_ms(erow['date'])
    strat = build_strategy(strategy_name, cfg)
    peak = 0.0
    # 从次日开始盯盘
    for j in range(ei + 1, len(rows)):
        r = rows[j]
        if r.get('bid') is None:
            continue
        pnl = (float(r['bid']) - entry_price) / entry_price * 100.0
        peak = max(peak, pnl)
        ctx = StrategyContext(pnl_percent=pnl, minutes_to_close=None,
                              market_price=float(r['bid']), entry_price=entry_price,
                              now_ts=_to_ms(r['date']), opened_at=entry_ms)
        reason = strat.decide(ctx)
        if reason is not None:
            return BacktestResult(
                entry_date=erow['date'], exit_date=r['date'],
                entry_price=round(entry_price, 4), exit_price=round(float(r['bid']), 4),
                pnl_percent=round(pnl, 2), reason=reason.value,
                peak_pnl_percent=round(peak, 2),
                days_held=(_to_ms(r['date']) - entry_ms) // 86400000)
    # 未触发 → 末日强平（到期强平的日线近似）
    last = next((rows[k] for k in range(len(rows) - 1, ei, -1) if rows[k].get('bid') is not None), None)
    if last is None:
        return None
    pnl = (float(last['bid']) - entry_price) / entry_price * 100.0
    peak = max(peak, pnl)
    return BacktestResult(
        entry_date=erow['date'], exit_date=last['date'],
        entry_price=round(entry_price, 4), exit_price=round(float(last['bid']), 4),
        pnl_percent=round(pnl, 2), reason=CloseReason.TIME_FORCE_CLOSE.value,
        peak_pnl_percent=round(peak, 2),
        days_held=(_to_ms(last['date']) - entry_ms) // 86400000)


def run_batch(series: List[dict], cfg, strategy_name: str, fill: str = 'ask') -> dict:
    """同合约多入场：区间内每个交易日各入场一次，跑到该合约末日。返回 {results, summary}。"""
    rows = sorted(series, key=lambda r: r['date'])
    results = []
    for r in rows:
        res = run_backtest(rows, cfg, strategy_name, entry_date=r['date'], fill=fill)
        if res is not None and res.entry_date == r['date']:
            results.append(res)
    return {'results': results, 'summary': summarize(results)}


def run_rolling_atm(closes: dict, chain_rows: List[dict], cfg, strategy_name: str,
                    target_dte: int = 30, min_dte: int = 3, step_days: int = 1,
                    fill: str = 'ask') -> dict:
    """滚动 ATM 批量：每个交易日按现价选近月平值合约入场，跑策略到退出，汇总。

    closes: {date: close}（stocks）；chain_rows: [{date,expiration,strike,bid,ask}]（options）。
    选合约：DTE≥min_dte 的到期中取 DTE 最接近 target_dte（并列取较小）；该到期下 |strike−spot| 最小为 ATM。
    返回 {results:[BacktestResult], metas:[{expiration,strike,spot,dte}], summary}。
    """
    by_date = {}
    series_by_contract = {}
    for r in chain_rows:
        d = str(r['date'])[:10]
        by_date.setdefault(d, []).append(r)
        key = (str(r['expiration'])[:10], float(r['strike']))
        series_by_contract.setdefault(key, []).append(
            {'date': d, 'bid': r.get('bid'), 'ask': r.get('ask')})

    results, metas = [], []
    for idx, ed in enumerate(sorted(closes.keys())):
        if step_days > 1 and idx % step_days != 0:
            continue
        day_rows = by_date.get(ed)
        if not day_rows:
            continue
        spot = float(closes[ed])
        ed_d = _date(ed)
        exps = {}
        for r in day_rows:
            e = str(r['expiration'])[:10]
            dte = (_date(e) - ed_d).days
            if dte >= min_dte:
                exps[e] = dte
        if not exps:
            continue
        chosen = min(exps, key=lambda e: (abs(exps[e] - target_dte), exps[e]))
        cand = [r for r in day_rows if str(r['expiration'])[:10] == chosen]
        atm = min(cand, key=lambda r: abs(float(r['strike']) - spot))
        strike = float(atm['strike'])
        series = series_by_contract.get((chosen, strike))
        if not series:
            continue
        res = run_backtest(series, cfg, strategy_name, entry_date=ed, fill=fill)
        if res is None or res.entry_date != ed:
            continue
        results.append(res)
        metas.append({'expiration': chosen, 'strike': strike,
                      'spot': round(spot, 4), 'dte': exps[chosen]})
    return {'results': results, 'metas': metas, 'summary': summarize(results)}


def summarize(results: List[BacktestResult]) -> dict:
    n = len(results)
    if n == 0:
        return {'count': 0}
    pnls = [r.pnl_percent for r in results]
    wins = [p for p in pnls if p > 0]
    reasons = {}
    for r in results:
        reasons[r.reason] = reasons.get(r.reason, 0) + 1
    return {
        'count': n,
        'win_rate': round(len(wins) / n, 4),
        'avg_pnl_percent': round(sum(pnls) / n, 2),
        'max_win': round(max(pnls), 2),
        'max_loss': round(min(pnls), 2),
        'avg_days_held': round(sum(r.days_held for r in results) / n, 1),
        'reasons': reasons,
    }
