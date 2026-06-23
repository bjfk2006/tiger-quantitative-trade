# -*- coding: utf-8 -*-
"""跨式多腿单测：纯决策 helpers + StraddleManager(mock adapter)。"""
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from option_bot.domain.models import (BotState, CloseReason, OptionPick,
                                      PositionView, StraddleLeg, StrategyConfig)
from option_bot.strategy.straddle import (StraddleManager, combined_pnl_percent,
                                          decide_combined_close, legs_to_stop)


def leg(ident, pc, entry, qty=1, closed=False, realized=0.0):
    return StraddleLeg(identifier=ident, put_call=pc, qty=qty, entry_price=entry,
                       closed=closed, realized_pnl=realized)


class TestCombinedPnl(unittest.TestCase):
    def test_both_open(self):
        legs = [leg('C', 'CALL', 5), leg('P', 'PUT', 5)]   # 总成本 1000
        # call 6, put 4 -> (1-1)*100=0 -> 0%
        self.assertAlmostEqual(combined_pnl_percent(legs, {'C': 6, 'P': 4}), 0.0)
        # call 8, put 4 -> (3-1)*100=200 -> 20%
        self.assertAlmostEqual(combined_pnl_percent(legs, {'C': 8, 'P': 4}), 20.0)

    def test_one_closed_realized(self):
        legs = [leg('C', 'CALL', 5, closed=True, realized=-50.0), leg('P', 'PUT', 5)]
        # realized -50 + put未实现 (10-5)*100=500 -> 450/1000=45%
        self.assertAlmostEqual(combined_pnl_percent(legs, {'P': 10}), 45.0)

    def test_missing_market_returns_none(self):
        legs = [leg('C', 'CALL', 5), leg('P', 'PUT', 5)]
        self.assertIsNone(combined_pnl_percent(legs, {'C': 6}))   # 缺 P

    def test_zero_cost_none(self):
        self.assertIsNone(combined_pnl_percent([leg('C', 'CALL', None)], {}))


class TestDecideCombined(unittest.TestCase):
    def test_time_force(self):
        st = {'armed': False, 'peak': None}
        self.assertEqual(decide_combined_close(0, 3, 5, 'trailing', 10, 10, 10, st),
                         CloseReason.TIME_FORCE_CLOSE)

    def test_fixed(self):
        st = {}
        self.assertEqual(decide_combined_close(10, 100, 5, 'fixed', 10, 0, 0, st),
                         CloseReason.TAKE_PROFIT)
        self.assertIsNone(decide_combined_close(8, 100, 5, 'fixed', 10, 0, 0, st))

    def test_trailing(self):
        st = {'armed': False, 'peak': None}
        self.assertIsNone(decide_combined_close(12, 100, 5, 'trailing', 0, 10, 5, st))
        self.assertTrue(st['armed'])
        self.assertEqual(st['peak'], 12)
        self.assertIsNone(decide_combined_close(20, 100, 5, 'trailing', 0, 10, 5, st))
        self.assertEqual(st['peak'], 20)
        # 回撤到 15 = 20-5 -> 触发
        self.assertEqual(decide_combined_close(15, 100, 5, 'trailing', 0, 10, 5, st),
                         CloseReason.TRAILING_STOP)


class TestLegStop(unittest.TestCase):
    def test_stop_loser(self):
        legs = [leg('C', 'CALL', 5), leg('P', 'PUT', 5)]
        # call 4.5 -> -10% 触发; put 6 -> +20% 不触发
        self.assertEqual(legs_to_stop(legs, {'C': 4.5, 'P': 6}, 10), ['C'])

    def test_skip_closed_and_missing(self):
        legs = [leg('C', 'CALL', 5, closed=True), leg('P', 'PUT', 5)]
        self.assertEqual(legs_to_stop(legs, {}, 10), [])


class TestStraddleManager(unittest.TestCase):
    def setUp(self):
        self.td = MagicMock()
        self.td.account = 'paper-1'
        self.td.new_dedup_tag.return_value = 'obot-x'
        self.td.open_market.return_value = 9001
        self.td.close_market.return_value = 9002
        self.td.get_order_status.return_value = {
            'status': 'FILLED', 'filled': 1, 'remaining': 0, 'avg_fill_price': 5.0}
        self.td.get_option_position.return_value = PositionView(
            quantity=1, salable_qty=1, average_cost=5.0, market_price=6.0,
            unrealized_pnl=100.0, unrealized_pnl_percent=20.0)
        self.md = MagicMock()
        self.md.is_market_trading.return_value = True
        self.md.get_option_quote.return_value = {'bid_price': 5.0, 'ask_price': 5.1}
        self.md.resolve_option.side_effect = lambda sym, exp, strike, pc, market=None: OptionPick(
            symbol=sym, expiry='20260626', strike=float(strike), put_call=pc,
            identifier=f'{sym} 260626{"C" if pc=="CALL" else "P"}00210000')
        self.clock = MagicMock()
        self.clock.minutes_to_close.return_value = 100
        self.cfg = StrategyConfig(mode='straddle', max_qty=1, fill_timeout=1,
                                  fill_poll_interval=0, straddle_tp_mode='fixed',
                                  straddle_tp=10, leg_stop=10)
        self.dir = tempfile.mkdtemp()
        self.mgr = StraddleManager(self.td, self.md, self.cfg, self.clock,
                                   os.path.join(self.dir, 's.json'),
                                   sleep=lambda *_: None, now_ms=lambda: 1000)

    def test_open_two_legs(self):
        ids = self.mgr.open('NVDA', '2026-06-26', 210, 1)
        self.assertEqual(len(ids), 2)
        self.assertEqual(self.mgr.state, BotState.MONITORING)
        self.assertEqual(self.td.open_market.call_count, 2)
        self.assertTrue(all(l.entry_price == 5.0 for l in self.mgr.legs))

    def test_combined_tp_closes_all(self):
        self.mgr.open('NVDA', '2026-06-26', 210, 1)
        # 两腿 entry5 market6 -> 组合 20% ≥ tp10 -> 平所有
        self.mgr.run_once()
        self.assertEqual(self.mgr.state, BotState.CLOSED)
        self.assertEqual(self.td.close_market.call_count, 2)


if __name__ == '__main__':
    unittest.main()
