# -*- coding: utf-8 -*-
"""影子追踪器单测：锁定→盯市→出场（纯观察，不下单）。"""
import unittest
from unittest.mock import MagicMock

from option_bot.domain.models import StrategyConfig
from option_bot import shadow


def _cfg():
    return StrategyConfig(mode='condor', condor_underlying='SPY', condor_min_iv=0.20,
                          condor_profit_target=0.5, condor_stop_mult=2.0, condor_dte_exit=21)


def _entry():
    # 一条已锁定的影子铁鹰：入场信用 1.0/股
    legs = [{'identifier': 'P90', 'put_call': 'PUT', 'side': 'BUY', 'strike': 90.0},
            {'identifier': 'P95', 'put_call': 'PUT', 'side': 'SELL', 'strike': 95.0},
            {'identifier': 'C105', 'put_call': 'CALL', 'side': 'SELL', 'strike': 105.0},
            {'identifier': 'C110', 'put_call': 'CALL', 'side': 'BUY', 'strike': 110.0}]
    return {'ts': 't0', 'symbol': 'SPY', 'expiry': '20260807',
            'expiry_date': '2999-01-01',          # DTE 极大，避免触发时间出场
            'legs': legs, 'entry_credit': 1.0, 'mid_credit': 1.1, 'max_loss': 4.0,
            'iv': 0.25, 'spot': 100.0, 'dte0': 40, 'put_width': 5.0, 'call_width': 5.0}


def _md_with(quotes):
    md = MagicMock()
    md.get_option_quote.side_effect = lambda ident, market='US': quotes.get(ident)
    return md


class TestShadowMarking(unittest.TestCase):
    def test_tracking_records_trajectory(self):
        # 当前结构净值 mid ≈ 0.8（< 入场 1.0）→ 盈利 0.2/股，未到 +50%，不出场
        q = {'P90': {'bid_price': 0.1, 'ask_price': 0.3}, 'P95': {'bid_price': 0.5, 'ask_price': 0.7},
             'C105': {'bid_price': 0.5, 'ask_price': 0.7}, 'C110': {'bid_price': 0.1, 'ask_price': 0.3}}
        st = {'status': 'TRACKING', 'entry': _entry(), 'trajectory': [], 'outcome': None}
        st, msg = shadow.step(st, _cfg(), _md_with(q))
        self.assertEqual(st['status'], 'TRACKING')
        self.assertEqual(len(st['trajectory']), 1)
        # SELL(P95 mid .6 + C105 mid .6) − BUY(P90 mid .2 + C110 mid .2) = 0.8
        self.assertAlmostEqual(st['trajectory'][0]['close_cost'], 0.8, places=3)
        self.assertAlmostEqual(st['trajectory'][0]['pnl'], 0.2, places=3)

    def test_take_profit_closes_shadow(self):
        # 结构净值跌到 ≈0.4 → 盈利 0.6 ≥ 50%×1.0 → 止盈出场 → CLOSED
        q = {k: {'bid_price': 0.1, 'ask_price': 0.1} for k in ('P90', 'C110')}
        q.update({'P95': {'bid_price': 0.2, 'ask_price': 0.2}, 'C105': {'bid_price': 0.2, 'ask_price': 0.2}})
        st = {'status': 'TRACKING', 'entry': _entry(), 'trajectory': [], 'outcome': None}
        st, msg = shadow.step(st, _cfg(), _md_with(q))
        self.assertEqual(st['status'], 'CLOSED')
        self.assertEqual(st['outcome']['reason'], 'TAKE_PROFIT')
        self.assertGreater(st['outcome']['pnl'], 0)

    def test_lock_sets_tracking(self):
        # lock_or_none 返回提案 → WAITING 转 TRACKING 并写入 entry
        cfg = _cfg()
        prop = {'expiry': '20260807', 'expiry_date': '2026-08-07', 'credit': 1.0,
                'mid_credit': 1.1, 'max_loss': 4.0, 'iv': 0.25, 'spot': 100.0, 'dte': 40,
                'put_width': 5.0, 'call_width': 5.0, 'legs': _entry()['legs']}
        orig = shadow.lock_or_none
        shadow.lock_or_none = lambda c, m: prop
        try:
            st, msg = shadow.step(_empty(), cfg, MagicMock())
        finally:
            shadow.lock_or_none = orig
        self.assertEqual(st['status'], 'TRACKING')
        self.assertEqual(st['entry']['entry_credit'], 1.0)
        self.assertEqual(len(st['entry']['legs']), 4)

    def test_wait_stays_when_no_lock(self):
        orig = shadow.lock_or_none
        shadow.lock_or_none = lambda c, m: None
        try:
            st, msg = shadow.step(_empty(), _cfg(), MagicMock())
        finally:
            shadow.lock_or_none = orig
        self.assertEqual(st['status'], 'WAITING')
        self.assertIsNone(st['entry'])


def _empty():
    return {'status': 'WAITING', 'entry': None, 'trajectory': [], 'outcome': None}


if __name__ == '__main__':
    unittest.main()
