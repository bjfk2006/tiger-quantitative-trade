# -*- coding: utf-8 -*-
"""铁鹰影子追踪器（纯观察 / 纸面前测，**零下单**）。

用途：等引擎入场条件满足（RTH + IV≥min_iv）时**锁定**当时的铁鹰结构，之后定时给它
做盯市（mark-to-market），记录盈亏走势并按设计的出场规则（+50%止盈/−2×止损/≤21DTE）
判定，验证策略的盈利模式是否如设计预测兑现。

**只读市场数据**：复用 `strategy.condor` 的选腿/计价/出场纯函数与 `MarketDataAdapter`，
**绝不建 TradeClient、绝不下单**。状态机 WAITING→TRACKING→CLOSED，先跟完第一条机会。

用法（容器内）：`python -m option_bot.shadow {sample|report|reset}`。
cron 每 10 分钟跑 `sample`；状态文件默认 /app/data/shadow_condor.json（OBOT_SHADOW_FILE 覆盖）。
"""
import argparse
import datetime as _dt
import json
import logging
import os
import tempfile
import time

logger = logging.getLogger('option_bot.shadow')

SHADOW_FILE = os.environ.get('OBOT_SHADOW_FILE', '/app/data/shadow_condor.json')


def _now_iso():
    return _dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%SZ')


def _empty_state():
    return {'status': 'WAITING', 'entry': None, 'trajectory': [], 'outcome': None}


def load_state(path=SHADOW_FILE):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return _empty_state()


def save_state(st, path=SHADOW_FILE):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix='.shadow_', dir=d)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_cfg():
    """从 OBOT_CONDOR_* 环境变量构造与引擎一致的 StrategyConfig。"""
    from option_bot.domain.models import StrategyConfig
    from option_bot.service import _b, _f, _i
    g = os.environ.get
    return StrategyConfig(
        mode='condor',
        condor_underlying=g('OBOT_CONDOR_UNDERLYING') or 'SPY',
        condor_target_dte=_i(g('OBOT_CONDOR_TARGET_DTE'), 40),
        condor_short_delta=_f(g('OBOT_CONDOR_SHORT_DELTA'), 0.16),
        condor_wing_width=_f(g('OBOT_CONDOR_WING_WIDTH'), 5.0),
        condor_min_iv=_f(g('OBOT_CONDOR_MIN_IV'), 0.20),
        condor_profit_target=_f(g('OBOT_CONDOR_PROFIT_TARGET'), 0.5),
        condor_stop_mult=_f(g('OBOT_CONDOR_STOP_MULT'), 2.0),
        condor_dte_exit=_i(g('OBOT_CONDOR_DTE_EXIT'), 21),
        condor_max_loss_pct=_f(g('OBOT_CONDOR_MAX_LOSS_PCT'), 0.05),
        condor_account_equity=_f(g('OBOT_CONDOR_ACCOUNT_EQUITY'), 0.0),
        condor_synthetic_greeks=_b(g('OBOT_CONDOR_SYNTHETIC_GREEKS'), True),
        condor_risk_free=_f(g('OBOT_CONDOR_RISK_FREE'), 0.0),
        max_qty=_i(g('OBOT_MAX_QTY'), 1),
    )


def build_md():
    """构造只读 MarketDataAdapter（QuoteClient）。"""
    from option_bot.adapters.market_data import MarketDataAdapter
    from option_bot.config.loader import load_client_config_from_env
    from tigeropen.quote.quote_client import QuoteClient
    cfg = load_client_config_from_env(props_path=os.environ.get('TIGEROPEN_PROPS_PATH'))
    return MarketDataAdapter(QuoteClient(cfg))


class _StubTd:
    """占位交易适配器：影子模式只选结构、绝不下单，_try_propose 不会调用它。"""
    account = 'shadow'

    def new_dedup_tag(self):
        return 'shadow'


def lock_or_none(cfg, md):
    """复用引擎 CondorManager._try_propose 选结构（不下单）；满足入场闸返回 proposal，否则 None。"""
    from option_bot.domain.models import BotState
    from option_bot.persistence.sink import NullSink
    from option_bot.strategy.condor import CondorManager
    probe = tempfile.mktemp(suffix='_shadow_probe.json')
    mgr = CondorManager(_StubTd(), md, cfg, None, probe, sink=NullSink())
    try:
        mgr._try_propose()
        return mgr.proposal if mgr.state == BotState.PROPOSED else None
    except Exception as e:  # noqa: BLE001 —— 选结构失败不应中断观察
        logger.warning('影子选结构异常: %s', e)
        return None
    finally:
        if os.path.exists(probe):
            os.remove(probe)


def _dte(expiry_date, tzname):
    import pytz
    tz = pytz.timezone(tzname)
    today = _dt.datetime.now(tz).date()
    exp = _dt.datetime.strptime(expiry_date, '%Y-%m-%d').date()
    return (exp - today).days


def mark(cfg, md, entry):
    """给已锁定结构做一次盯市。返回 {close_cost,pnl,pnl_pct,dte,reason} 或 None（行情缺失）。"""
    from option_bot.adapters.errors import DataUnavailable
    from option_bot.strategy.condor import exit_decision, net_credit
    legs = [{'identifier': l['identifier'], 'side': l['side']} for l in entry['legs']]
    try:
        qbi = {l['identifier']: md.get_option_quote(l['identifier'], market='US') for l in legs}
    except DataUnavailable as e:
        logger.warning('影子取行情失败: %s', e)
        return None
    close_cost = net_credit(legs, qbi, 'mid', closing=True)
    dte = _dte(entry['expiry_date'], cfg.timezone)
    cred = entry['entry_credit']
    pnl = (cred - close_cost) if close_cost is not None else None
    pnl_pct = (pnl / cred * 100.0) if (pnl is not None and cred) else None
    reason = exit_decision(cred, close_cost, dte, cfg.condor_profit_target,
                           cfg.condor_stop_mult, cfg.condor_dte_exit)
    return {'close_cost': close_cost, 'pnl': pnl, 'pnl_pct': pnl_pct,
            'dte': dte, 'reason': reason.value if reason else None}


def step(state, cfg, md):
    """推进一步影子状态机。返回 (new_state, message)。"""
    status = state.get('status', 'WAITING')
    if status == 'CLOSED':
        return state, 'CLOSED（第一条机会已跟完；reset 可重开）'

    if status == 'WAITING':
        prop = lock_or_none(cfg, md)
        if not prop:
            return state, f'{_now_iso()} WAITING …（未满足 RTH+IV≥{cfg.condor_min_iv:.0%}）'
        state['status'] = 'TRACKING'
        state['entry'] = {
            'ts': _now_iso(), 'symbol': cfg.condor_underlying,
            'expiry': prop['expiry'], 'expiry_date': prop['expiry_date'],
            'legs': prop['legs'], 'entry_credit': prop['credit'],
            'mid_credit': prop.get('mid_credit'), 'max_loss': prop['max_loss'],
            'iv': prop['iv'], 'spot': prop.get('spot'), 'dte0': prop['dte'],
            'put_width': prop['put_width'], 'call_width': prop['call_width'],
        }
        return state, (f"{_now_iso()} ★ 锁定影子铁鹰 {cfg.condor_underlying} {prop['expiry']} "
                       f"信用/股≈{prop['credit']:.2f} 最大亏损/股≈{prop['max_loss']:.2f} "
                       f"IV={prop['iv']:.1%} 现价≈{prop.get('spot')} | 腿: "
                       + ' '.join(f"{l['side']}{l['put_call'][0]}{l['strike']:g}" for l in prop['legs']))

    # TRACKING
    m = mark(cfg, md, state['entry'])
    if m is None:
        return state, f'{_now_iso()} 取行情失败，跳过本次盯市'
    state['trajectory'].append({
        'ts': _now_iso(),
        'close_cost': None if m['close_cost'] is None else round(m['close_cost'], 4),
        'pnl': None if m['pnl'] is None else round(m['pnl'], 4),
        'pnl_pct_of_credit': None if m['pnl_pct'] is None else round(m['pnl_pct'], 1),
        'dte': m['dte'], 'exit': m['reason'],
    })
    msg = (f"{_now_iso()} 盯市 dte={m['dte']} "
           f"平仓成本/股≈{'NA' if m['close_cost'] is None else round(m['close_cost'], 2)} "
           f"盈亏/股≈{'NA' if m['pnl'] is None else round(m['pnl'], 2)} "
           f"({'NA' if m['pnl_pct'] is None else round(m['pnl_pct'], 0)}% of credit)")
    if m['reason'] is not None:
        state['status'] = 'CLOSED'
        state['outcome'] = {
            'ts': _now_iso(), 'reason': m['reason'],
            'pnl': None if m['pnl'] is None else round(m['pnl'], 4),
            'pnl_pct_of_credit': None if m['pnl_pct'] is None else round(m['pnl_pct'], 1),
            'dte': m['dte'],
        }
        msg += f"  → 出场 {m['reason']}（影子，未真实平仓）"
    return state, msg


def cmd_sample(path):
    cfg, md = build_cfg(), build_md()
    state = load_state(path)
    state, msg = step(state, cfg, md)
    save_state(state, path)
    print(msg)


def cmd_report(path):
    st = load_state(path)
    print('status:', st['status'])
    e = st.get('entry')
    if e:
        print(f"entry {e['ts']}: {e['symbol']} {e['expiry']} 信用/股≈{e['entry_credit']:.2f} "
              f"最大亏损/股≈{e['max_loss']:.2f} IV={e['iv']:.1%} 现价≈{e.get('spot')} DTE0={e['dte0']}")
        print('legs:', ' '.join(f"{l['side']}{l['put_call'][0]}{l['strike']:g}" for l in e['legs']))
    traj = st.get('trajectory') or []
    print(f'samples: {len(traj)}（盈亏正=权利金衰减获利）')
    for t in traj[-10:]:
        print(f"  {t['ts']} dte={t['dte']} 平仓≈{t['close_cost']} 盈亏/股≈{t['pnl']} "
              f"({t['pnl_pct_of_credit']}%)" + (f"  EXIT {t['exit']}" if t.get('exit') else ''))
    o = st.get('outcome')
    if o:
        print(f"OUTCOME: {o['reason']} 盈亏/股≈{o['pnl']} ({o['pnl_pct_of_credit']}% of credit) dte={o['dte']}")


def cmd_reset(path):
    save_state(_empty_state(), path)
    print('shadow reset → WAITING')


def main(argv=None):
    ap = argparse.ArgumentParser(description='铁鹰影子追踪器（纯观察，不下单）')
    ap.add_argument('cmd', choices=['sample', 'report', 'reset'])
    ap.add_argument('--file', default=SHADOW_FILE)
    args = ap.parse_args(argv)
    {'sample': cmd_sample, 'report': cmd_report, 'reset': cmd_reset}[args.cmd](args.file)


if __name__ == '__main__':
    main()
