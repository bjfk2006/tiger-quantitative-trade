# -*- coding: utf-8 -*-
"""策略状态聚合（设计 docs/design/2026-06-30-dashboard-strategy-nav-condor-panel.md §5.1）。

纯函数、**不依赖 flask、无 IO 副作用**（行情/文件 IO 由调用方传入），便于单测，
并被 `tools/watch_condor`（CLI 文本）与看板 `/api/strategy_status`（JSON）共用，口径同源。

- compute_condor_view(entry, last_tick, spot): 由入场信用/中间价/最新 close_cost 派生
  点差缺口、theta 已填、pnl；给 spot 时再算现价离两侧短腿的距离与缓冲。
- build_strategy_status(engine_status, shadow_state): 按数据源优先级 + 单 mode 语义
  产出看板用统一负载。
"""

ALL_MODES = ('condor', 'straddle', 'single')


def short_strikes(legs):
    """从 4 条腿里取 (short_put, short_call) 行权价；缺失返回 (None, None)。"""
    sp = sc = None
    for leg in legs or []:
        if leg.get('side') == 'SELL' and leg.get('put_call') == 'PUT':
            sp = leg.get('strike')
        if leg.get('side') == 'SELL' and leg.get('put_call') == 'CALL':
            sc = leg.get('strike')
    return sp, sc


def compute_condor_view(entry, last_tick=None, spot=None):
    """派生铁鹰展示字段（纯计算）。

    entry: {symbol, expiry/expiry_date, legs, entry_credit, mid_credit, spot(开仓), dte0, strategy_state}
    last_tick: {close_cost, pnl_pct_of_credit, dte} 或 None
    spot: 当前现价或 None（None 时不算短腿距离，留给二期）。
    返回纯可序列化 dict；缺数据的字段为 None。
    """
    entry = entry or {}
    last_tick = last_tick or {}
    legs = entry.get('legs', [])
    sp, sc = short_strikes(legs)
    ec, mc = entry.get('entry_credit'), entry.get('mid_credit')
    close_cost = last_tick.get('close_cost')
    pnl_pct = last_tick.get('pnl_pct_of_credit')
    dte = last_tick.get('dte', entry.get('dte0'))
    armed = bool((entry.get('strategy_state') or {}).get('armed'))

    # 点差缺口 gap0=(ec−mc)/ec（≈负）；theta 已填 = 现 pnl − gap0
    gap0_pct = ((ec - mc) / ec * 100.0) if ec and mc else None
    theta_filled_pt = (pnl_pct - gap0_pct) if (pnl_pct is not None and gap0_pct is not None) else None

    view = {
        'symbol': entry.get('symbol'),
        'expiry': entry.get('expiry_date') or entry.get('expiry'),
        'dte': dte,
        'put_strike': sp, 'call_strike': sc,
        'mid_strike': ((sp + sc) / 2.0) if (sp is not None and sc is not None) else None,
        'open_spot': entry.get('spot'),
        'entry_credit': ec, 'mid_credit': mc, 'close_cost': close_cost,
        'gap0_pct': gap0_pct, 'pnl_pct': pnl_pct, 'theta_filled_pt': theta_filled_pt,
        'armed': armed,
        'spot': None, 'd_put': None, 'd_call': None,
        'buf_put_pct': None, 'buf_call_pct': None, 'near': None, 'spot_side': None,
    }

    warns = []
    if spot and sp is not None and sc is not None:
        d_put, d_call = spot - sp, sc - spot          # >0 即在短腿安全侧
        buf_put, buf_call = d_put / spot * 100.0, d_call / spot * 100.0
        view.update({
            'spot': spot, 'd_put': d_put, 'd_call': d_call,
            'buf_put_pct': buf_put, 'buf_call_pct': buf_call,
            'near': 'call' if d_call < d_put else 'put',
            'spot_side': 'call' if spot > view['mid_strike'] else 'put',
        })
        if buf_put < 2.0:
            warns.append(f"short put 缓冲仅 {buf_put:+.2f}%")
        if buf_call < 2.0:
            warns.append(f"short call 缓冲仅 {buf_call:+.2f}%")
        if d_put < 0 or d_call < 0:
            warns.append("已有短腿被击穿")
    if armed:
        warns.append("移动止盈已 armed")
    view['warns'] = warns
    return view


def _engine_entry(es):
    """把引擎 status() 适配成 compute_condor_view 的 entry 形状（二期引擎补字段后生效）。"""
    return {
        'symbol': es.get('symbol'), 'expiry': es.get('expiry'),
        'legs': [{'side': l.get('side'), 'put_call': l.get('pc') or l.get('put_call'),
                  'strike': l.get('strike')} for l in es.get('legs', [])],
        'entry_credit': es.get('entry_credit'), 'mid_credit': es.get('mid_credit'),
        'spot': es.get('spot'), 'strategy_state': es.get('strategy_state'),
    }


def _engine_tick(es):
    return {'close_cost': es.get('close_cost'), 'pnl_pct_of_credit': es.get('pnl_percent'),
            'dte': es.get('dte')}


def _engine_open(es):
    """引擎是否有在场真实仓（且已具备盯市字段，二期）。"""
    return bool(es.get('qty')) and es.get('close_cost') is not None


def build_strategy_status(engine_status, shadow_state):
    """看板统一负载。engine_status=supervisor.status()；shadow_state=影子 JSON（可 None）。

    单 mode 语义：active_mode 来自引擎；只有 active 策略可能 live，其余 live=False。
    铁鹰数据源优先级：引擎在场 > 影子 TRACKING > 等待入场（仅 iv/ivp）。
    """
    es = engine_status or {}
    active = (es.get('mode') or 'single').lower()
    strategies = {m: {'live': False, 'active': m == active} for m in ALL_MODES}

    condor = strategies['condor']
    # 闸/IV 基础信息（condor 模式下引擎始终可提供，即便在等待）
    if active == 'condor':
        for k in ('iv', 'ivp', 'ivr', 'gate_mode', 'symbol', 'state'):
            if es.get(k) is not None:
                condor[k] = es.get(k)

    sh = shadow_state or {}
    sh_tracking = (sh.get('status') == 'TRACKING' and sh.get('outcome') is None and 'entry' in sh)

    if active == 'condor' and _engine_open(es):
        condor.update(compute_condor_view(_engine_entry(es), _engine_tick(es), es.get('spot')))
        condor['live'] = True
        condor['source'] = 'engine'
    elif sh_tracking:
        traj = sh.get('trajectory') or []
        condor.update(compute_condor_view(sh['entry'], traj[-1] if traj else None, None))
        condor['live'] = True
        condor['source'] = 'shadow'
    elif sh.get('outcome') is not None:
        condor['outcome'] = sh.get('outcome')
        condor['source'] = 'shadow'
    else:
        condor['source'] = 'none'   # 等待入场（iv/ivp 已在上面带出）

    # 引擎 _last_iv 在重启后/proposal 待批期间可能暂为 None；用影子开仓 IV 兜底显示，
    # 避免卡片长时间显示「IV —」。IVP 影子没有，保持引擎口径（采样后自然补上）。
    if condor.get('iv') is None and sh.get('entry', {}).get('iv') is not None:
        condor['iv'] = sh['entry']['iv']

    return {
        'active_mode': active,
        'bot_alive': bool(es.get('bot_alive', False)),
        'strategies': strategies,
    }
