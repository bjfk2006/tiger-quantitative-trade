# -*- coding: utf-8 -*-
"""铁鹰卖方策略纯决策核心单测。设计：2026-06-26-condor-premium-selling-engine.md。"""
import unittest

from option_bot.domain.models import CloseReason, PositionView, StrategyConfig
from option_bot.strategy.close_strategies import (StrategyContext,
                                                  build_condor_close_strategy)
from option_bot.strategy.condor import (atm_iv, atm_iv_live, bs_delta, bs_price,
                                        build_condor, condor_max_loss,
                                        condor_pnl_percent, enrich_greeks,
                                        greeks_missing, implied_spot,
                                        implied_vol_from_price, net_credit,
                                        nearest_strike_row, norm_cdf, _parse_pct,
                                        _reverse_legs, passes_entry_gate,
                                        select_by_delta, size_by_max_loss)


def _pos_side_effect(long_strikes):
    """构造 get_option_position 的 side_effect：long_strikes 内为多头(+1)，其余空头(-1)。"""
    def _pos(pick, *a, **k):
        q = 1 if float(pick.strike) in long_strikes else -1
        return PositionView(q, abs(q), 1.0, 1.0, 0.0, 0.0)
    return _pos


def row(ident, pc, strike, delta, iv=0.3, bid=1.0, ask=1.2):
    return {'identifier': ident, 'put_call': pc, 'strike': strike, 'delta': delta,
            'implied_vol': iv, 'bid_price': bid, 'ask_price': ask, 'latest_price': (bid + ask) / 2}


class TestEntryGate(unittest.TestCase):
    def test_blocks_when_has_position(self):
        ok, _ = passes_entry_gate(0.4, 0.2, True, True)
        self.assertFalse(ok)

    def test_blocks_outside_rth(self):
        self.assertFalse(passes_entry_gate(0.4, 0.2, False, False)[0])

    def test_blocks_low_iv(self):
        self.assertFalse(passes_entry_gate(0.15, 0.20, True, False)[0])

    def test_blocks_iv_none(self):
        self.assertFalse(passes_entry_gate(None, 0.20, True, False)[0])

    def test_passes_when_iv_high_and_flat_rth(self):
        self.assertTrue(passes_entry_gate(0.35, 0.20, True, False)[0])


class TestEntryGateModes(unittest.TestCase):
    """IV-Rank 入场闸：rank/both 模式 + 暖机回退（设计 2026-06-29）。"""

    def test_absolute_unchanged(self):
        # 默认 absolute：等于旧行为，IVP 不参与
        self.assertTrue(passes_entry_gate(0.25, 0.20, True, False, mode='absolute', ivp=0)[0])
        self.assertFalse(passes_entry_gate(0.15, 0.20, True, False, mode='absolute', ivp=99)[0])

    def test_rank_mode(self):
        # 纯分位：IVP≥阈值即过，绝对 IV 低也行
        self.assertTrue(passes_entry_gate(0.14, 0.20, True, False,
                                          mode='rank', ivp=70, min_rank=50)[0])
        self.assertFalse(passes_entry_gate(0.30, 0.20, True, False,
                                           mode='rank', ivp=40, min_rank=50)[0])

    def test_both_mode_needs_floor_and_rank(self):
        # both：IV≥地板 且 IVP≥阈值
        self.assertTrue(passes_entry_gate(0.14, 0.20, True, False, mode='both', ivp=70,
                                          min_rank=50, rank_floor=0.12)[0])
        # IVP 高但绝对 IV 低于地板 → 拦
        self.assertFalse(passes_entry_gate(0.10, 0.20, True, False, mode='both', ivp=90,
                                           min_rank=50, rank_floor=0.12)[0])
        # 绝对够但 IVP 低 → 拦
        self.assertFalse(passes_entry_gate(0.18, 0.20, True, False, mode='both', ivp=30,
                                           min_rank=50, rank_floor=0.12)[0])

    def test_warmup_falls_back_to_absolute(self):
        # 历史不足(history_ok=False)：rank/both 一律回退 absolute(用 iv_min=0.20)
        # 活 IV 0.14 在 absolute 下会被 0.20 拦（即便 IVP 高）——保证数据不足不乱开
        ok, reason = passes_entry_gate(0.14, 0.20, True, False, mode='both', ivp=99,
                                       min_rank=50, rank_floor=0.12, history_ok=False)
        self.assertFalse(ok)
        # 活 IV 0.25 在回退 absolute 下过闸
        self.assertTrue(passes_entry_gate(0.25, 0.20, True, False, mode='rank', ivp=0,
                                          min_rank=50, history_ok=False)[0])

    def test_rank_mode_blocks_when_ivp_missing(self):
        self.assertFalse(passes_entry_gate(0.30, 0.20, True, False,
                                           mode='rank', ivp=None, history_ok=True)[0])


class TestSelection(unittest.TestCase):
    def setUp(self):
        # 现价 ~100；puts delta 负、calls delta 正
        self.puts = [row('P90', 'PUT', 90, -0.10), row('P95', 'PUT', 95, -0.16),
                     row('P100', 'PUT', 100, -0.50)]
        self.calls = [row('C100', 'CALL', 100, 0.50), row('C105', 'CALL', 105, 0.16),
                      row('C110', 'CALL', 110, 0.09)]

    def test_atm_iv_picks_delta_half(self):
        self.assertEqual(atm_iv(self.puts + self.calls), 0.3)

    def test_select_put_by_delta(self):
        r = select_by_delta(self.puts, 0.16, 'PUT')
        self.assertEqual(r['strike'], 95)

    def test_select_call_by_delta(self):
        r = select_by_delta(self.calls, 0.16, 'CALL')
        self.assertEqual(r['strike'], 105)

    def test_nearest_strike(self):
        self.assertEqual(nearest_strike_row(self.puts, 89, 'PUT')['strike'], 90)


class TestBuildCondor(unittest.TestCase):
    def setUp(self):
        self.puts = [row('P85', 'PUT', 85, -0.06), row('P90', 'PUT', 90, -0.10),
                     row('P95', 'PUT', 95, -0.16), row('P100', 'PUT', 100, -0.50)]
        self.calls = [row('C100', 'CALL', 100, 0.50), row('C105', 'CALL', 105, 0.16),
                      row('C110', 'CALL', 110, 0.10), row('C115', 'CALL', 115, 0.06)]

    def test_builds_valid_condor(self):
        c = build_condor(self.calls, self.puts, short_delta=0.16, wing_width=5)
        legs = c['legs']
        self.assertEqual([l['strike'] for l in legs], [90, 95, 105, 110])
        self.assertEqual([l['side'] for l in legs], ['BUY', 'SELL', 'SELL', 'BUY'])
        self.assertEqual(c['put_width'], 5)
        self.assertEqual(c['call_width'], 5)

    def test_returns_none_when_no_short(self):
        self.assertIsNone(build_condor([], [], 0.16, 5))

    def test_side_call_only_bear_call(self):
        c = build_condor(self.calls, self.puts, 0.16, 5, side='call')
        legs = c['legs']
        self.assertEqual([l['strike'] for l in legs], [105, 110])        # 卖105 / 买110
        self.assertEqual([l['side'] for l in legs], ['SELL', 'BUY'])
        self.assertEqual([l['put_call'] for l in legs], ['CALL', 'CALL'])
        self.assertEqual(c['call_width'], 5)
        self.assertEqual(c['put_width'], 0.0)                            # 单边另一侧 width=0
        # condor_max_loss 单边 = call_width − credit
        self.assertAlmostEqual(condor_max_loss(c['put_width'], c['call_width'], 1.5), 3.5)

    def test_side_put_only_bull_put(self):
        c = build_condor(self.calls, self.puts, 0.16, 5, side='put')
        legs = c['legs']
        self.assertEqual([l['strike'] for l in legs], [90, 95])          # 买90 / 卖95
        self.assertEqual([l['side'] for l in legs], ['BUY', 'SELL'])
        self.assertEqual([l['put_call'] for l in legs], ['PUT', 'PUT'])
        self.assertEqual(c['put_width'], 5)
        self.assertEqual(c['call_width'], 0.0)

    def test_side_call_ignores_missing_puts(self):
        # 单边 call 时，put 链为空也能成（只看 call 侧）
        c = build_condor(self.calls, [], 0.16, 5, side='call')
        self.assertIsNotNone(c)
        self.assertEqual(len(c['legs']), 2)

    def test_invalid_side_falls_back_both(self):
        c = build_condor(self.calls, self.puts, 0.16, 5, side='xyz')
        self.assertEqual(len(c['legs']), 4)                              # 非法值回退 both

    def test_nan_delta_skipped(self):
        nan = float('nan')
        # 闭市/冷门：delta 为 NaN 的行应被跳过，不参与选腿
        puts = [row('Pnan', 'PUT', 95, nan), row('P95', 'PUT', 95, -0.16),
                row('P90', 'PUT', 90, -0.10)]
        self.assertEqual(select_by_delta(puts, 0.16, 'PUT')['identifier'], 'P95')
        self.assertIsNone(select_by_delta([row('Pnan', 'PUT', 95, nan)], 0.16, 'PUT'))

    def test_atm_iv_skips_nan(self):
        nan = float('nan')
        rows = [row('A', 'CALL', 100, nan, iv=0.9), row('B', 'CALL', 100, 0.5, iv=0.33)]
        self.assertEqual(atm_iv(rows), 0.33)


class TestCreditAndRisk(unittest.TestCase):
    def _legs_quotes(self):
        # 卖 P95(bid1.0/ask1.2) 卖 C105 ; 买 P90 买 C110
        legs = [{'identifier': 'P90', 'side': 'BUY', 'put_call': 'PUT', 'strike': 90},
                {'identifier': 'P95', 'side': 'SELL', 'put_call': 'PUT', 'strike': 95},
                {'identifier': 'C105', 'side': 'SELL', 'put_call': 'CALL', 'strike': 105},
                {'identifier': 'C110', 'side': 'BUY', 'put_call': 'CALL', 'strike': 110}]
        q = {'P90': {'bid_price': 0.4, 'ask_price': 0.6},
             'P95': {'bid_price': 1.0, 'ask_price': 1.2},
             'C105': {'bid_price': 1.0, 'ask_price': 1.2},
             'C110': {'bid_price': 0.4, 'ask_price': 0.6}}
        return legs, q

    def test_net_credit_mid(self):
        legs, q = self._legs_quotes()
        # mid: sell 1.1+1.1 - buy 0.5+0.5 = 1.2
        self.assertAlmostEqual(net_credit(legs, q, 'mid'), 1.2)

    def test_net_credit_conservative_open_lower(self):
        legs, q = self._legs_quotes()
        # 开仓保守: 卖吃bid(1.0+1.0) - 买付ask(0.6+0.6) = 0.8 < mid
        self.assertAlmostEqual(net_credit(legs, q, 'conservative', closing=False), 0.8)

    def test_net_credit_none_when_missing(self):
        legs, q = self._legs_quotes()
        del q['P95']
        self.assertIsNone(net_credit(legs, q, 'mid'))

    def test_max_loss(self):
        # 翼宽5, 权利金1.2 → 最大亏损 3.8
        self.assertAlmostEqual(condor_max_loss(5, 5, 1.2), 3.8)

    def test_max_loss_floor_zero(self):
        self.assertEqual(condor_max_loss(5, 5, 10), 0.0)

    def test_size_by_max_loss(self):
        # 每股最大亏损3.8 ×100 = 380/张；账户1万 ×5% =500 → 1 张
        self.assertEqual(size_by_max_loss(3.8, 100, 10000, 0.05, 1), 1)

    def test_size_fallback_when_no_equity(self):
        self.assertEqual(size_by_max_loss(3.8, 100, 0, 0.05, 2), 2)


def _condor_decide(entry_credit, close_cost, dte, **cfgkw):
    """构建铁鹰默认(threshold)平仓策略并判一次，等价旧 exit_decision 的薄封装。"""
    cfg = StrategyConfig(mode='condor', condor_underlying='SPY', **cfgkw)
    strat = build_condor_close_strategy(cfg)
    ctx = StrategyContext(pnl_percent=condor_pnl_percent(entry_credit, close_cost),
                          minutes_to_close=None, dte=dte)
    return strat.decide(ctx)


class TestExitDecision(unittest.TestCase):
    """默认 threshold 策略须逐条等价于旧 exit_decision（用户已确认"符合预期"）。"""

    def test_take_profit_at_50pct(self):
        # 收1.0, 现在平仓只需付0.5 → pnl=0.5 = 50% → 止盈
        self.assertEqual(_condor_decide(1.0, 0.5, 30), CloseReason.TAKE_PROFIT)

    def test_stop_loss_at_2x(self):
        # 收1.0, 平仓要付3.0 → pnl=-2.0 = -2× → 止损
        self.assertEqual(_condor_decide(1.0, 3.0, 30), CloseReason.STOP_LOSS)

    def test_dte_exit(self):
        self.assertEqual(_condor_decide(1.0, 0.9, 21), CloseReason.TIME_FORCE_CLOSE)

    def test_hold(self):
        self.assertIsNone(_condor_decide(1.0, 0.8, 30))

    def test_dte_exit_even_when_value_missing(self):
        # close_cost 不可得 → 只剩 DTE 强平（pnl_percent=None，与旧语义一致）
        self.assertEqual(_condor_decide(None, None, 20), CloseReason.TIME_FORCE_CLOSE)

    def test_profit_priority_over_dte(self):
        self.assertEqual(_condor_decide(1.0, 0.4, 21), CloseReason.TAKE_PROFIT)

    def test_hold_when_value_missing_and_dte_far(self):
        self.assertIsNone(_condor_decide(None, None, 30))


class TestCondorPluggableClose(unittest.TestCase):
    """铁鹰可插拔平仓：归一化、trailing、状态往返、force_close_dte 不污染 straddle。"""

    def test_pnl_percent_normalization(self):
        self.assertAlmostEqual(condor_pnl_percent(1.0, 0.5), 50.0)
        self.assertAlmostEqual(condor_pnl_percent(2.0, 5.0), -150.0)
        self.assertIsNone(condor_pnl_percent(1.0, None))
        self.assertIsNone(condor_pnl_percent(0.0, 0.5))      # 入场权利金≤0 → None(防除零)

    def test_trailing_arms_and_locks_on_giveback(self):
        # 武装 +30%、回撤 15%(占权利金)：峰值 +40% 回落到 +25% → TRAILING_STOP
        cfg = StrategyConfig(mode='condor', condor_underlying='SPY',
                             condor_close_strategy='trailing',
                             condor_trail_activation=30.0, condor_trail_giveback=15.0)
        strat = build_condor_close_strategy(cfg)
        mk = lambda pct, dte=30: StrategyContext(pnl_percent=pct, minutes_to_close=None, dte=dte)
        self.assertIsNone(strat.decide(mk(20)))    # 未到武装阈值
        self.assertIsNone(strat.decide(mk(40)))    # 武装并记峰值 40
        self.assertIsNone(strat.decide(mk(30)))    # 回撤 10 < 15，持有
        self.assertEqual(strat.decide(mk(25)), CloseReason.TRAILING_STOP)  # 回撤 15 → 锁盈

    def test_trailing_state_roundtrip(self):
        cfg = StrategyConfig(mode='condor', condor_underlying='SPY',
                             condor_close_strategy='trailing',
                             condor_trail_activation=30.0, condor_trail_giveback=15.0)
        s1 = build_condor_close_strategy(cfg)
        mk = lambda pct: StrategyContext(pnl_percent=pct, minutes_to_close=None, dte=30)
        s1.decide(mk(40))                          # 武装、峰值 40
        snap = s1.state()
        s2 = build_condor_close_strategy(cfg)      # 模拟重启
        s2.load_state(snap)
        self.assertEqual(s2.decide(mk(25)), CloseReason.TRAILING_STOP)  # 峰值已还原→直接触发

    def test_trailing_hard_stop_still_applies(self):
        # 即便用 trailing，硬止损(stop_mult×100=200%)仍在基类强制生效
        cfg = StrategyConfig(mode='condor', condor_underlying='SPY',
                             condor_close_strategy='trailing', condor_stop_mult=2.0,
                             condor_trail_activation=30.0, condor_trail_giveback=15.0)
        strat = build_condor_close_strategy(cfg)
        ctx = StrategyContext(pnl_percent=-200.0, minutes_to_close=None, dte=30)
        self.assertEqual(strat.decide(ctx), CloseReason.STOP_LOSS)

    def test_force_close_dte_does_not_affect_straddle(self):
        # straddle 路径(build_strategy)不设 force_close_dte → DTE 不触发强平(回归保护)
        from option_bot.strategy.close_strategies import build_strategy
        cfg = StrategyConfig(mode='straddle', tp_percent=30.0, sl_percent=50.0)
        strat = build_strategy('threshold', cfg)
        self.assertIsNone(strat.force_close_dte)
        ctx = StrategyContext(pnl_percent=5.0, minutes_to_close=None, dte=0)
        self.assertIsNone(strat.decide(ctx))   # dte=0 也不强平(straddle 靠 minutes_to_close)

    def test_unknown_strategy_raises(self):
        cfg = StrategyConfig(mode='condor', condor_underlying='SPY',
                             condor_close_strategy='bogus')
        with self.assertRaises(ValueError):
            build_condor_close_strategy(cfg)


import datetime as _dt
import os
import tempfile
from unittest.mock import MagicMock

import pytz

from option_bot.domain.models import BotState, StrategyConfig
from option_bot.strategy.condor import CondorManager

NOW = 1768000000000  # 固定 now_ms，便于算 DTE


def _date_offset(days):
    tz = pytz.timezone('America/New_York')
    today = _dt.datetime.fromtimestamp(NOW / 1000.0, tz).date()
    return (today + _dt.timedelta(days=days)).strftime('%Y-%m-%d')


def _chain(atm_iv_val=0.30):
    puts = [row('P85', 'PUT', 85, -0.06, bid=0.2, ask=0.4),
            row('P90', 'PUT', 90, -0.10, bid=0.4, ask=0.6),
            row('P95', 'PUT', 95, -0.16, bid=1.0, ask=1.2),
            row('P100', 'PUT', 100, -0.50, iv=atm_iv_val, bid=3.0, ask=3.2)]
    calls = [row('C100', 'CALL', 100, 0.50, iv=atm_iv_val, bid=3.0, ask=3.2),
             row('C105', 'CALL', 105, 0.16, bid=1.0, ask=1.2),
             row('C110', 'CALL', 110, 0.10, bid=0.4, ask=0.6),
             row('C115', 'CALL', 115, 0.06, bid=0.2, ask=0.4)]
    return calls + puts


def _make_mgr(tmp, atm_iv_val=0.30, quotes=None, sink=None):
    md = MagicMock()
    md.is_market_trading.return_value = True
    md.list_expirations.return_value = [{'date': _date_offset(40)}]
    md.get_chain.return_value = _chain(atm_iv_val)
    md.get_underlying_price.return_value = 100.0
    qmap = quotes or {r['identifier']: {'bid_price': r['bid_price'], 'ask_price': r['ask_price']}
                      for r in _chain(atm_iv_val)}
    md.get_option_quote.side_effect = lambda ident, market='US': qmap.get(ident)
    td = MagicMock()
    td.account = 'paper-1'
    td.new_dedup_tag.return_value = 'tag'
    cfg = StrategyConfig(mode='condor', condor_underlying='SPY', condor_min_iv=0.20,
                         fill_timeout=0.05, fill_poll_interval=0.0,
                         condor_iv_history_file=tmp + '.ivh.json')   # 隔离每测的 IV 历史
    mgr = CondorManager(td, md, cfg, MagicMock(), tmp, sleep=lambda *_: None,
                        now_ms=lambda: NOW, sink=sink)
    return mgr, md, td


class TestSyntheticGreeks(unittest.TestCase):
    def test_parse_pct(self):
        self.assertAlmostEqual(_parse_pct('16.65%'), 0.1665)
        self.assertAlmostEqual(_parse_pct('0%'), 0.0)
        self.assertAlmostEqual(_parse_pct(0.1665), 0.1665)
        self.assertAlmostEqual(_parse_pct('0.2'), 0.2)
        self.assertIsNone(_parse_pct(None))
        self.assertIsNone(_parse_pct('abc'))

    def test_norm_cdf(self):
        self.assertAlmostEqual(norm_cdf(0.0), 0.5)
        self.assertGreater(norm_cdf(1.0), 0.84)
        self.assertLess(norm_cdf(-1.0), 0.16)

    def test_bs_delta_atm_and_wings(self):
        # ATM call delta 略 >0.5（含漂移）；ATM put 略 >-0.5
        cd = bs_delta(100, 100, 0.1, 0.2, 0.04, 'CALL')
        pd = bs_delta(100, 100, 0.1, 0.2, 0.04, 'PUT')
        self.assertTrue(0.5 < cd < 0.62)
        self.assertTrue(-0.5 < pd < -0.38)
        # 深 ITM call → ~1；深 OTM call → ~0
        self.assertGreater(bs_delta(100, 50, 0.5, 0.2, 0.0, 'CALL'), 0.98)
        self.assertLess(bs_delta(100, 200, 0.5, 0.2, 0.0, 'CALL'), 0.02)
        # put 恒为负
        self.assertLess(bs_delta(100, 90, 0.3, 0.25, 0.0, 'PUT'), 0)

    def test_bs_delta_bad_inputs(self):
        self.assertIsNone(bs_delta(0, 100, 0.1, 0.2, 0.0, 'CALL'))
        self.assertIsNone(bs_delta(100, 100, 0.0, 0.2, 0.0, 'CALL'))
        self.assertIsNone(bs_delta(100, 100, 0.1, 0.0, 0.0, 'CALL'))
        self.assertIsNone(bs_delta(100, 100, 0.1, None, 0.0, 'CALL'))

    def test_greeks_missing(self):
        self.assertTrue(greeks_missing([row('A', 'CALL', 100, 0.0), row('B', 'PUT', 95, 0.0)]))
        self.assertTrue(greeks_missing([{'identifier': 'A', 'delta': None}]))
        self.assertFalse(greeks_missing([row('A', 'CALL', 100, 0.0), row('B', 'CALL', 100, 0.3)]))

    def test_implied_spot_parity(self):
        # 构造 spot=732 的链：call_mid - put_mid + K ≈ 732
        rows = []
        spot = 732.0
        for k in range(700, 765, 5):
            cmid = max(0.5, spot - k) + 8.0   # 粗略：内在 + 时间价值
            pmid = max(0.5, k - spot) + 8.0
            rows.append({'identifier': f'C{k}', 'put_call': 'CALL', 'strike': k,
                         'bid_price': cmid - 0.1, 'ask_price': cmid + 0.1})
            rows.append({'identifier': f'P{k}', 'put_call': 'PUT', 'strike': k,
                         'bid_price': pmid - 0.1, 'ask_price': pmid + 0.1})
        self.assertAlmostEqual(implied_spot(rows), spot, delta=1.0)

    def test_enrich_greeks_enables_delta_selection(self):
        # 全 0 delta 的链，enrich 后能按 16Δ 选出合理短腿
        spot, iv, t, r = 732.0, 0.1665, 42 / 365.0, 0.04
        rows = []
        for k in range(680, 790, 5):
            rows.append({'identifier': f'C{k}', 'put_call': 'CALL', 'strike': float(k),
                         'delta': 0.0, 'implied_vol': 0.0, 'bid_price': 1.0, 'ask_price': 1.2})
            rows.append({'identifier': f'P{k}', 'put_call': 'PUT', 'strike': float(k),
                         'delta': 0.0, 'implied_vol': 0.0, 'bid_price': 1.0, 'ask_price': 1.2})
        n = enrich_greeks(rows, spot, iv, t, r)
        self.assertEqual(n, len(rows))
        puts = [r for r in rows if r['put_call'] == 'PUT']
        calls = [r for r in rows if r['put_call'] == 'CALL']
        ps = select_by_delta(puts, 0.16, 'PUT')
        cs = select_by_delta(calls, 0.16, 'CALL')
        # 16Δ put 在现价下方、16Δ call 在上方，且大致对称价外
        self.assertLess(ps['strike'], spot)
        self.assertGreater(cs['strike'], spot)
        self.assertTrue(685 <= ps['strike'] <= 710)
        self.assertTrue(765 <= cs['strike'] <= 790)

    def test_enrich_preserves_real_delta(self):
        rows = [row('C', 'CALL', 100, 0.30), {'identifier': 'P', 'put_call': 'PUT',
                'strike': 95.0, 'delta': 0.0, 'bid_price': 1, 'ask_price': 1.2}]
        enrich_greeks(rows, 100, 0.2, 0.1, 0.0)
        self.assertEqual(rows[0]['delta'], 0.30)   # 真 delta 不被覆盖
        self.assertLess(rows[1]['delta'], 0)       # 缺失的 put 被填为负 delta


def _bs_chain(spot, sigma, t, r, lo=80, hi=120, step=5, spread=0.05):
    """按已知 σ 用 BS 给每个 strike 的 call/put 定价、delta 置 0（模拟无逐档 greeks 的链）。"""
    rows = []
    for k in range(lo, hi + 1, step):
        for pc in ('CALL', 'PUT'):
            px = bs_price(spot, k, t, sigma, r, pc)
            rows.append({'identifier': f'{pc[0]}{k}', 'put_call': pc, 'strike': float(k),
                         'delta': 0.0, 'implied_vol': 0.0,
                         'bid_price': max(0.0, px - spread), 'ask_price': px + spread})
    return rows


class TestLiveIV(unittest.TestCase):
    """自算活 IV：BS 正向定价 + 反推 + 近 ATM 鲁棒估计（设计 2026-06-27-condor-live-iv-signal）。"""

    def test_bs_price_parity(self):
        # put-call 平价：C − P = S − K·e^(−rT)
        s, k, t, sig, r = 100.0, 100.0, 0.1, 0.2, 0.04
        c = bs_price(s, k, t, sig, r, 'CALL')
        p = bs_price(s, k, t, sig, r, 'PUT')
        self.assertAlmostEqual(c - p, s - k * __import__('math').exp(-r * t), places=6)
        self.assertGreater(c, 0)
        self.assertGreater(p, 0)

    def test_bs_price_bad_inputs(self):
        self.assertIsNone(bs_price(0, 100, 0.1, 0.2, 0.0, 'CALL'))
        self.assertIsNone(bs_price(100, 100, 0.0, 0.2, 0.0, 'CALL'))
        self.assertIsNone(bs_price(100, 100, 0.1, 0.0, 0.0, 'CALL'))

    def test_implied_vol_roundtrip(self):
        # price=bs_price(σ) → 反推应还原 σ（call/put、多档 moneyness）
        s, t, r = 100.0, 40 / 365.0, 0.04
        for sig in (0.10, 0.18, 0.35, 0.80):
            for k in (90, 100, 110):
                for pc in ('CALL', 'PUT'):
                    px = bs_price(s, k, t, sig, r, pc)
                    got = implied_vol_from_price(px, s, k, t, r, pc)
                    self.assertIsNotNone(got)
                    self.assertAlmostEqual(got, sig, places=4)

    def test_implied_vol_rejects_bad_price(self):
        s, k, t, r = 100.0, 100.0, 0.1, 0.04
        intrinsic = max(0.0, s - k * __import__('math').exp(-r * t))
        self.assertIsNone(implied_vol_from_price(intrinsic - 0.01, s, k, t, r, 'CALL'))  # 低于内在
        self.assertIsNone(implied_vol_from_price(s + 1, s, k, t, r, 'CALL'))             # 高于上界
        self.assertIsNone(implied_vol_from_price(None, s, k, t, r, 'CALL'))

    def test_atm_iv_live_recovers_sigma(self):
        # 整条链按 σ=0.27 定价 → 近 ATM 反推中位数 ≈ 0.27
        s, sig, t, r = 100.0, 0.27, 40 / 365.0, 0.04
        chain = _bs_chain(s, sig, t, r)
        got = atm_iv_live(chain, s, t, r)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, sig, places=2)

    def test_atm_iv_live_skips_bad_quotes(self):
        # ATM 一档报价缺失/坏价被剔除，仍能从其余近 ATM 档恢复 σ
        s, sig, t, r = 100.0, 0.22, 40 / 365.0, 0.04
        chain = _bs_chain(s, sig, t, r)
        for rw in chain:
            if rw['strike'] == 100.0:           # 把 ATM 两腿打成无效报价
                rw['bid_price'], rw['ask_price'] = None, None
        got = atm_iv_live(chain, s, t, r)
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, sig, delta=0.02)

    def test_atm_iv_live_none_when_no_valid(self):
        self.assertIsNone(atm_iv_live([], 100.0, 0.1, 0.04))
        self.assertIsNone(atm_iv_live([{'strike': 100.0, 'put_call': 'CALL'}], None, 0.1, 0.04))


class TestReverseLegs(unittest.TestCase):
    def test_reverse_flips_each_side(self):
        legs = [{'identifier': 'P90', 'side': 'BUY', 'put_call': 'PUT', 'strike': 90},
                {'identifier': 'P95', 'side': 'SELL', 'put_call': 'PUT', 'strike': 95}]
        rev = _reverse_legs(legs)
        self.assertEqual([l['side'] for l in rev], ['SELL', 'BUY'])
        self.assertEqual(legs[0]['side'], 'BUY')           # 原列表不被改
        self.assertEqual([l['identifier'] for l in rev], ['P90', 'P95'])


class TestLegView(unittest.TestCase):
    def test_sell_leg_profit_when_price_drops(self):
        from option_bot.domain.models import CondorLeg
        from option_bot.strategy.condor import CondorManager
        leg = CondorLeg(identifier='P95', put_call='PUT', side='SELL',
                        strike=95, qty=2, entry_price=1.0)
        v = CondorManager._leg_view(leg, mid=0.6)   # 卖腿现价跌 → 盈利
        self.assertAlmostEqual(v.unrealized_pnl, (1.0 - 0.6) * 2 * 100)   # +80
        self.assertGreater(v.unrealized_pnl_percent, 0)

    def test_buy_leg_loss_when_price_drops(self):
        from option_bot.domain.models import CondorLeg
        from option_bot.strategy.condor import CondorManager
        leg = CondorLeg(identifier='P90', put_call='PUT', side='BUY',
                        strike=90, qty=2, entry_price=1.0)
        v = CondorManager._leg_view(leg, mid=0.6)   # 买腿现价跌 → 亏损
        self.assertAlmostEqual(v.unrealized_pnl, (0.6 - 1.0) * 2 * 100)   # -80
        self.assertLess(v.unrealized_pnl_percent, 0)


class TestCondorManager(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mktemp(suffix='_condor.json')

    def tearDown(self):
        for p in (self._tmp, self._tmp + '.ivh.json'):
            if os.path.exists(p):
                os.remove(p)

    def test_proposes_when_iv_high(self):
        mgr, _, _ = _make_mgr(self._tmp, atm_iv_val=0.30)
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.PROPOSED)
        self.assertIsNotNone(mgr.proposal)
        self.assertEqual([l['strike'] for l in mgr.proposal['legs']], [90, 95, 105, 110])
        self.assertGreater(mgr.proposal['credit'], 0)

    def test_no_proposal_when_iv_low(self):
        mgr, _, _ = _make_mgr(self._tmp, atm_iv_val=0.10)
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.IDLE)
        self.assertIsNone(mgr.proposal)

    def _synth_mgr(self, sigma, iv_source='computed', briefs_vol='16.65%', **cfg_extra):
        # 无逐档 greeks 的 BS 定价链（delta=0）→ 走合成路径，入场 IV 由 condor_iv_source 决定
        s, t, r = 100.0, 40 / 365.0, 0.04
        chain = _bs_chain(s, sigma, t, r)
        md = MagicMock()
        md.is_market_trading.return_value = True
        md.list_expirations.return_value = [{'date': _date_offset(40)}]
        md.get_chain.return_value = chain
        md.get_underlying_price.side_effect = lambda *_a, **_k: 100.0
        qmap = {rw['identifier']: {'bid_price': rw['bid_price'], 'ask_price': rw['ask_price'],
                                   'volatility': briefs_vol, 'rates_bonds': 0.04} for rw in chain}
        md.get_option_quote.side_effect = lambda ident, market='US': qmap.get(ident)
        td = MagicMock()
        td.account = 'paper-1'
        td.new_dedup_tag.return_value = 'tag'
        cfg = StrategyConfig(mode='condor', condor_underlying='SPY', condor_min_iv=0.20,
                             condor_synthetic_greeks=True, condor_iv_source=iv_source,
                             condor_risk_free=0.0, fill_timeout=0.05, fill_poll_interval=0.0,
                             condor_iv_history_file=self._tmp + '.ivh.json', **cfg_extra)
        return CondorManager(td, md, cfg, MagicMock(), self._tmp, sleep=lambda *_: None,
                             now_ms=lambda: NOW)

    def test_iv_rank_both_mode_blocks_low_percentile(self):
        # both 模式 + 暖机已满：活 IV(~0.30)高于地板，但预置历史使 IVP 低 → 不开
        mgr = self._synth_mgr(0.30, condor_iv_gate_mode='both', condor_iv_rank_floor=0.12,
                              condor_min_iv_rank=50.0, condor_iv_rank_min_history=3)
        # 预置一段"更高 IV"历史 → 当前 0.30 分位低
        for i, v in enumerate([0.40, 0.45, 0.50, 0.55]):
            mgr._iv_store.append_daily(f'2026-05-0{i+1}', v)
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.IDLE)
        self.assertIsNone(mgr.proposal)

    def test_iv_rank_both_mode_proposes_when_relatively_high(self):
        # 预置一段"更低 IV"历史 → 当前 0.30 分位高 + 高于地板 → 提案
        mgr = self._synth_mgr(0.30, condor_iv_gate_mode='both', condor_iv_rank_floor=0.12,
                              condor_min_iv_rank=50.0, condor_iv_rank_min_history=3)
        for i, v in enumerate([0.10, 0.12, 0.14, 0.16]):
            mgr._iv_store.append_daily(f'2026-05-0{i+1}', v)
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.PROPOSED)
        self.assertIsNotNone(mgr.proposal['ivp'])
        self.assertGreaterEqual(mgr.proposal['ivp'], 50.0)

    def test_iv_rank_warmup_falls_back_to_absolute(self):
        # rank 模式但历史不足(min_history 高) → 回退 absolute；活 IV~0.30≥0.20 → 提案
        mgr = self._synth_mgr(0.30, condor_iv_gate_mode='rank', condor_min_iv_rank=99.0,
                              condor_iv_rank_min_history=60)
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.PROPOSED)   # 回退 absolute 放行（否则 IVP 不足会拦）

    def test_live_iv_proposes_when_computed_iv_high(self):
        # 链按 σ=0.30 定价 > min_iv 0.20 → 自算活 IV 过闸 → 提案
        mgr = self._synth_mgr(0.30)
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.PROPOSED)
        self.assertIsNotNone(mgr.proposal)
        self.assertAlmostEqual(mgr.proposal['iv'], 0.30, delta=0.03)

    def test_live_iv_blocks_when_computed_iv_low(self):
        # 链按 σ=0.10 定价 < min_iv 0.20 → 活 IV 拦下，即便 briefs 显示 16.65%
        mgr = self._synth_mgr(0.10, briefs_vol='16.65%')
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.IDLE)
        self.assertIsNone(mgr.proposal)

    def test_iv_source_briefs_uses_stale_field(self):
        # 链按 σ=0.30 定价（活 IV 会过闸），但 source=briefs 用陈旧 16.65%<0.20 → 不提案
        mgr = self._synth_mgr(0.30, iv_source='briefs', briefs_vol='16.65%')
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.IDLE)
        self.assertIsNone(mgr.proposal)

    def test_approve_custom_single_atomic_combo(self):
        # 默认 CUSTOM：单笔 4 腿原子单
        mgr, _, td = _make_mgr(self._tmp)
        td.place_combo.side_effect = [777]
        td.get_order_status.return_value = {'status': 'FILLED', 'filled': 1,
                                            'remaining': 0, 'avg_fill_price': 0}
        mgr.run_once()
        ok, _ = mgr.approve()
        self.assertTrue(ok)
        self.assertEqual(mgr.state, BotState.MONITORING)
        self.assertEqual(td.place_combo.call_count, 1)        # 一单四腿
        self.assertEqual(len(mgr.legs), 4)
        self.assertEqual(mgr.combo_order_ids, [777])
        # combo_type=CUSTOM、四腿、净价=marketable 保守信用(非 mid)
        args = td.place_combo.call_args
        self.assertEqual(args.args[3], 'CUSTOM')
        self.assertEqual(len(args.args[2]), 4)
        # _chain: 保守开仓信用 = (P95.bid1.0+C105.bid1.0) − (P90.ask0.6+C110.ask0.6) = 0.8
        # （mid 会是 1.2；用保守=可成交）→ limit = -0.8
        self.assertAlmostEqual(args.args[6], -0.8, places=2)

    def test_approve_vertical_fallback_two_combos(self):
        mgr, _, td = _make_mgr(self._tmp)
        mgr._cfg.condor_open_combo_type = 'VERTICAL'
        td.place_combo.side_effect = [111, 222]
        td.get_order_status.return_value = {'status': 'FILLED', 'filled': 1,
                                            'remaining': 0, 'avg_fill_price': 0}
        mgr.run_once()
        ok, _ = mgr.approve()
        self.assertTrue(ok)
        self.assertEqual(mgr.state, BotState.MONITORING)
        self.assertEqual(td.place_combo.call_count, 2)        # 两个垂直
        self.assertEqual(len(mgr.legs), 4)
        self.assertEqual(mgr.combo_order_ids, [111, 222])

    def test_approve_custom_not_filled_cancels_and_idles(self):
        # CUSTOM 未成交 → 撤单 + 回 IDLE，无孤儿
        mgr, _, td = _make_mgr(self._tmp)
        td.place_combo.side_effect = [777]
        td.get_order_status.return_value = {'status': 'HELD', 'filled': 0,
                                            'remaining': 1, 'avg_fill_price': 0}
        mgr.run_once()
        ok, msg = mgr.approve()
        self.assertFalse(ok)
        self.assertEqual(mgr.state, BotState.IDLE)
        td.cancel_order.assert_called_once_with(777)
        self.assertEqual(len(mgr.legs), 0)

    def test_approve_vertical_partial_fill_rolls_back(self):
        # VERTICAL：put 成交、call 未成交 → 撤 call + 逐腿回滚已成交 put + 回 IDLE
        mgr, _, td = _make_mgr(self._tmp)
        mgr._cfg.condor_open_combo_type = 'VERTICAL'
        td.place_combo.side_effect = [111, 222]
        statuses = [{'status': 'FILLED', 'filled': 1, 'remaining': 0, 'avg_fill_price': 0},
                    {'status': 'HELD', 'filled': 0, 'remaining': 1, 'avg_fill_price': 0}]
        td.get_order_status.side_effect = lambda oid: statuses[0] if oid == 111 else statuses[1]
        mgr.run_once()
        ok, _ = mgr.approve()
        self.assertFalse(ok)
        self.assertEqual(mgr.state, BotState.IDLE)
        td.cancel_order.assert_called_once_with(222)
        # 已成交的 put 垂直两腿被逐腿反向市价回滚
        self.assertEqual(td.flatten_leg.call_count, 2)

    def test_reject_returns_idle(self):
        mgr, _, _ = _make_mgr(self._tmp)
        mgr.run_once()
        ok, _ = mgr.reject()
        self.assertTrue(ok)
        self.assertEqual(mgr.state, BotState.IDLE)
        self.assertIsNone(mgr.proposal)

    def test_approve_stale_proposal_voids(self):
        mgr, _, _ = _make_mgr(self._tmp)
        mgr.run_once()
        mgr.proposal['created_ms'] = NOW - 999 * 60000  # 远超 TTL
        ok, _ = mgr.approve()
        self.assertFalse(ok)
        self.assertEqual(mgr.state, BotState.IDLE)

    def test_monitor_take_profit_closes(self):
        # 现价让平仓成本很低 → 止盈 → 自动平两腿 → CLOSED
        cheap = {'P85': {'bid_price': 0.0, 'ask_price': 0.1},
                 'P90': {'bid_price': 0.0, 'ask_price': 0.1},
                 'P95': {'bid_price': 0.1, 'ask_price': 0.2},
                 'P100': {'bid_price': 0.1, 'ask_price': 0.2},
                 'C100': {'bid_price': 0.1, 'ask_price': 0.2},
                 'C105': {'bid_price': 0.1, 'ask_price': 0.2},
                 'C110': {'bid_price': 0.0, 'ask_price': 0.1},
                 'C115': {'bid_price': 0.0, 'ask_price': 0.1}}
        mgr, md, td = _make_mgr(self._tmp)
        td.place_combo.side_effect = [111, 222]
        td.get_order_status.return_value = {'status': 'FILLED', 'filled': 1,
                                            'remaining': 0, 'avg_fill_price': 0}
        mgr.run_once()
        mgr.approve()
        # 切换到便宜的平仓行情
        md.get_option_quote.side_effect = lambda ident, market='US': cheap.get(ident)
        td.place_combo.side_effect = [333, 444]
        mgr.run_once()                       # 监控 → 止盈 → 平仓
        self.assertEqual(mgr.state, BotState.CLOSED)

    def test_propose_throttled(self):
        # market_state 限流：IDLE 时 60s 内只评估一次
        mgr, md, _ = _make_mgr(self._tmp, atm_iv_val=0.10)  # 低 IV → 不提案、保持 IDLE
        t = [NOW]
        mgr._now_ms = lambda: t[0]
        mgr.run_once()
        mgr.run_once()
        self.assertEqual(md.is_market_trading.call_count, 1)  # 第二次被限频
        t[0] += 61000
        mgr.run_once()
        self.assertEqual(md.is_market_trading.call_count, 2)  # 过 60s 后再评估

    def test_sink_called_on_open_position_close(self):
        sink = MagicMock()
        mgr, md, td = _make_mgr(self._tmp, sink=sink)
        td.place_combo.side_effect = [111, 222]
        td.get_order_status.return_value = {'status': 'FILLED', 'filled': 1,
                                            'remaining': 0, 'avg_fill_price': 0}
        mgr.run_once()
        mgr.approve()
        # 开仓(默认 CUSTOM 单笔)：4 腿各落一条，全部归唯一 combo 111
        self.assertEqual(sink.on_open.call_count, 4)
        oids = {c.args[1].put_call: c.args[5] for c in sink.on_open.call_args_list}
        self.assertEqual(oids['PUT'], 111)
        self.assertEqual(oids['CALL'], 111)
        # 监控一轮：每腿一条持仓走势，view 盈亏符号正确（卖腿现价跌则盈）
        sink.reset_mock()
        mgr.run_once()
        self.assertEqual(sink.on_position.call_count, 4)
        # 平仓：止盈后每腿 on_close + on_position_closed
        cheap = {k: {'bid_price': 0.0, 'ask_price': 0.1} for k in
                 ('P85', 'P90', 'P95', 'P100', 'C100', 'C105', 'C110', 'C115')}
        md.get_option_quote.side_effect = lambda ident, market='US': cheap.get(ident)
        td.place_combo.side_effect = [333, 444]
        sink.reset_mock()
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.CLOSED)
        self.assertEqual(sink.on_close.call_count, 4)
        self.assertEqual(sink.on_position_closed.call_count, 4)

    def test_synthetic_greeks_path_proposes(self):
        # 券商无逐档 delta（chain.delta 全 0）+ 股票行情被拒 → 走 BS 合成兜底出提案
        from option_bot.adapters.errors import DataUnavailable
        spot = 100.0
        ch = []
        for k in range(75, 130, 5):
            tv = round(max(0.2, 5.0 - 0.18 * abs(k - spot)), 2)        # 时间价值，越价外越小
            cmid = tv + max(0.0, spot - k)                              # 平价：call=tv+内在
            pmid = tv + max(0.0, k - spot)
            ch.append({'identifier': f'C{k}', 'put_call': 'CALL', 'strike': float(k),
                       'delta': 0.0, 'implied_vol': 0.0,
                       'bid_price': round(cmid - 0.1, 2), 'ask_price': round(cmid + 0.1, 2),
                       'latest_price': cmid})
            ch.append({'identifier': f'P{k}', 'put_call': 'PUT', 'strike': float(k),
                       'delta': 0.0, 'implied_vol': 0.0,
                       'bid_price': round(pmid - 0.1, 2), 'ask_price': round(pmid + 0.1, 2),
                       'latest_price': pmid})
        md = MagicMock()
        md.is_market_trading.return_value = True
        md.list_expirations.return_value = [{'date': _date_offset(40)}]
        md.get_chain.return_value = ch
        md.get_underlying_price.side_effect = DataUnavailable('no stock perm')
        qmap = {r['identifier']: {'bid_price': r['bid_price'], 'ask_price': r['ask_price'],
                                  'volatility': '30%', 'rates_bonds': 0.04} for r in ch}
        md.get_option_quote.side_effect = lambda ident, market='US': qmap.get(ident)
        td = MagicMock(); td.account = 'paper-1'; td.new_dedup_tag.return_value = 'tag'
        # 本例验证合成 delta 管线（非 IV 来源），固定走 briefs 源使 iv=30% 可断言
        cfg = StrategyConfig(mode='condor', condor_underlying='SPY', condor_min_iv=0.20,
                             condor_synthetic_greeks=True, condor_iv_source='briefs')
        mgr = CondorManager(td, md, cfg, MagicMock(), self._tmp,
                            sleep=lambda *_: None, now_ms=lambda: NOW)
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.PROPOSED)
        self.assertEqual(len(mgr.proposal['legs']), 4)
        self.assertAlmostEqual(mgr.proposal['iv'], 0.30, places=4)
        self.assertAlmostEqual(mgr.proposal['spot'], spot, delta=1.0)   # 平价反推现价
        self.assertGreater(mgr.proposal['credit'], 0)

    def test_synthetic_disabled_keeps_idle_when_no_greeks(self):
        # 关闭合成 + 券商无 delta → 选不出腿，保持 IDLE（验证开关）
        ch = []
        for k in range(80, 125, 5):
            ch.append({'identifier': f'C{k}', 'put_call': 'CALL', 'strike': float(k),
                       'delta': 0.0, 'implied_vol': 0.0, 'bid_price': 1.0, 'ask_price': 1.2})
            ch.append({'identifier': f'P{k}', 'put_call': 'PUT', 'strike': float(k),
                       'delta': 0.0, 'implied_vol': 0.0, 'bid_price': 1.0, 'ask_price': 1.2})
        md = MagicMock()
        md.is_market_trading.return_value = True
        md.list_expirations.return_value = [{'date': _date_offset(40)}]
        md.get_chain.return_value = ch
        md.get_underlying_price.return_value = 100.0
        md.get_option_quote.side_effect = lambda ident, market='US': {'bid_price': 1.0, 'ask_price': 1.2}
        td = MagicMock(); td.account = 'paper-1'
        cfg = StrategyConfig(mode='condor', condor_underlying='SPY', condor_min_iv=0.20,
                             condor_synthetic_greeks=False)
        mgr = CondorManager(td, md, cfg, MagicMock(), self._tmp,
                            sleep=lambda *_: None, now_ms=lambda: NOW)
        mgr.run_once()
        self.assertEqual(mgr.state, BotState.IDLE)
        self.assertIsNone(mgr.proposal)

    def test_resume_restores_monitoring(self):
        mgr, _, td = _make_mgr(self._tmp)
        td.place_combo.side_effect = [777]
        td.get_order_status.return_value = {'status': 'FILLED', 'filled': 1,
                                            'remaining': 0, 'avg_fill_price': 0}
        mgr.run_once(); mgr.approve()
        # 新建一个 manager 从快照恢复；券商持仓与快照一致(90/110多、95/105空) → MONITORING
        mgr2, _, td2 = _make_mgr(self._tmp)
        td2.get_option_position.side_effect = _pos_side_effect({90.0, 110.0})
        self.assertTrue(mgr2.resume())
        self.assertEqual(mgr2.state, BotState.MONITORING)
        self.assertEqual(len(mgr2.legs), 4)

    def test_resume_reconcile_mismatch_halts_to_error(self):
        mgr, _, td = _make_mgr(self._tmp)
        td.place_combo.side_effect = [777]
        td.get_order_status.return_value = {'status': 'FILLED', 'filled': 1,
                                            'remaining': 0, 'avg_fill_price': 0}
        mgr.run_once(); mgr.approve()
        mgr2, _, td2 = _make_mgr(self._tmp)
        # 短腿 95 在券商缺失 → 对账不符 → ERROR(待人工)，不自动 MONITORING
        def pos(pick, *a, **k):
            if float(pick.strike) == 95.0:
                return None
            q = 1 if float(pick.strike) in (90.0, 110.0) else -1
            return PositionView(q, abs(q), 1.0, 1.0, 0.0, 0.0)
        td2.get_option_position.side_effect = pos
        self.assertTrue(mgr2.resume())
        self.assertEqual(mgr2.state, BotState.ERROR)
        # ERROR 态 run_once 不做任何自动动作
        mgr2.run_once()
        self.assertEqual(td2.place_combo.call_count, 0)


if __name__ == '__main__':
    unittest.main()
