# -*- coding: utf-8 -*-
"""铁鹰卖方策略纯决策核心单测。设计：2026-06-26-condor-premium-selling-engine.md。"""
import unittest

from option_bot.domain.models import CloseReason
from option_bot.strategy.condor import (atm_iv, build_condor, condor_max_loss,
                                        exit_decision, net_credit,
                                        nearest_strike_row, passes_entry_gate,
                                        select_by_delta, size_by_max_loss)


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


class TestExitDecision(unittest.TestCase):
    def test_take_profit_at_50pct(self):
        # 收1.0, 现在平仓只需付0.5 → pnl=0.5 = 50% → 止盈
        self.assertEqual(exit_decision(1.0, 0.5, 30), CloseReason.TAKE_PROFIT)

    def test_stop_loss_at_2x(self):
        # 收1.0, 平仓要付3.0 → pnl=-2.0 = -2× → 止损
        self.assertEqual(exit_decision(1.0, 3.0, 30), CloseReason.STOP_LOSS)

    def test_dte_exit(self):
        self.assertEqual(exit_decision(1.0, 0.9, 21), CloseReason.TIME_FORCE_CLOSE)

    def test_hold(self):
        self.assertIsNone(exit_decision(1.0, 0.8, 30))

    def test_dte_exit_even_when_value_missing(self):
        self.assertEqual(exit_decision(None, None, 20), CloseReason.TIME_FORCE_CLOSE)

    def test_profit_priority_over_dte(self):
        self.assertEqual(exit_decision(1.0, 0.4, 21), CloseReason.TAKE_PROFIT)


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


def _make_mgr(tmp, atm_iv_val=0.30, quotes=None):
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
    cfg = StrategyConfig(mode='condor', condor_underlying='SPY', condor_min_iv=0.20)
    mgr = CondorManager(td, md, cfg, MagicMock(), tmp, sleep=lambda *_: None,
                        now_ms=lambda: NOW)
    return mgr, md, td


class TestCondorManager(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mktemp(suffix='_condor.json')

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.remove(self._tmp)

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

    def test_approve_submits_two_verticals_and_monitors(self):
        mgr, _, td = _make_mgr(self._tmp)
        td.place_combo.side_effect = [111, 222]
        td.get_order_status.return_value = {'status': 'FILLED', 'filled': 1,
                                            'remaining': 0, 'avg_fill_price': 0}
        mgr.run_once()                       # → PROPOSED
        ok, _ = mgr.approve()
        self.assertTrue(ok)
        self.assertEqual(mgr.state, BotState.MONITORING)
        self.assertEqual(td.place_combo.call_count, 2)   # 两个垂直
        self.assertEqual(len(mgr.legs), 4)
        self.assertEqual(mgr.combo_order_ids, [111, 222])

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

    def test_resume_restores_monitoring(self):
        mgr, _, td = _make_mgr(self._tmp)
        td.place_combo.side_effect = [111, 222]
        td.get_order_status.return_value = {'status': 'FILLED', 'filled': 1,
                                            'remaining': 0, 'avg_fill_price': 0}
        mgr.run_once(); mgr.approve()
        # 新建一个 manager 从快照恢复
        mgr2, _, _ = _make_mgr(self._tmp)
        self.assertTrue(mgr2.resume())
        self.assertEqual(mgr2.state, BotState.MONITORING)
        self.assertEqual(len(mgr2.legs), 4)


if __name__ == '__main__':
    unittest.main()
