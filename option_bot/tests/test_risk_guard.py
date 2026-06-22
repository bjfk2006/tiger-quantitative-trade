# -*- coding: utf-8 -*-
"""RiskGuard 单测：评估优先级 + 开仓预检。对应设计 §10。"""
import unittest

from option_bot.domain.models import CloseReason, StrategyConfig
from option_bot.strategy.risk_guard import RiskGuard, compute_pnl_percent


class TestRiskGuardEvaluate(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig(tp_percent=30, sl_percent=50, close_buffer_minutes=5)
        self.rg = RiskGuard(self.cfg)

    def test_time_force_close_has_highest_priority(self):
        # 即使盈亏在阈值内，距收盘<=buffer 也强平
        self.assertEqual(self.rg.evaluate(0.0, 5), CloseReason.TIME_FORCE_CLOSE)
        self.assertEqual(self.rg.evaluate(10.0, 4.9), CloseReason.TIME_FORCE_CLOSE)

    def test_time_close_beats_stop_loss(self):
        # 同时满足止损与时间强平 -> 时间强平优先
        self.assertEqual(self.rg.evaluate(-80.0, 3), CloseReason.TIME_FORCE_CLOSE)

    def test_stop_loss(self):
        self.assertEqual(self.rg.evaluate(-50.0, 100), CloseReason.STOP_LOSS)
        self.assertEqual(self.rg.evaluate(-60.0, None), CloseReason.STOP_LOSS)

    def test_take_profit(self):
        self.assertEqual(self.rg.evaluate(30.0, 100), CloseReason.TAKE_PROFIT)
        self.assertEqual(self.rg.evaluate(45.0, None), CloseReason.TAKE_PROFIT)

    def test_hold(self):
        self.assertIsNone(self.rg.evaluate(10.0, 100))
        self.assertIsNone(self.rg.evaluate(-10.0, 100))

    def test_none_pnl_only_time_matters(self):
        self.assertIsNone(self.rg.evaluate(None, 100))
        self.assertEqual(self.rg.evaluate(None, 1), CloseReason.TIME_FORCE_CLOSE)

    def test_minutes_none_means_no_time_close(self):
        # 非交易日/已收盘 -> minutes_to_close=None，不触发时间强平
        self.assertIsNone(self.rg.evaluate(5.0, None))


class TestPreOpenCheck(unittest.TestCase):
    def setUp(self):
        self.cfg = StrategyConfig(max_spread_pct=5.0)
        self.rg = RiskGuard(self.cfg)

    def test_reject_when_not_trading(self):
        ok, _ = self.rg.pre_open_check({'bid_price': 1.0, 'ask_price': 1.02}, False)
        self.assertFalse(ok)

    def test_reject_when_no_quote(self):
        ok, _ = self.rg.pre_open_check(None, True)
        self.assertFalse(ok)

    def test_reject_when_spread_too_wide(self):
        # 点差 (1.2-1.0)/1.1 ≈ 18% > 5%
        ok, reason = self.rg.pre_open_check({'bid_price': 1.0, 'ask_price': 1.2}, True)
        self.assertFalse(ok)
        self.assertIn('点差', reason)

    def test_accept_tight_spread(self):
        ok, _ = self.rg.pre_open_check({'bid_price': 1.00, 'ask_price': 1.02}, True)
        self.assertTrue(ok)

    def test_reject_when_open_disabled(self):
        cfg = StrategyConfig(enable_open=False)
        ok, _ = RiskGuard(cfg).pre_open_check({'bid_price': 1, 'ask_price': 1.01}, True)
        self.assertFalse(ok)


class TestComputePnl(unittest.TestCase):
    def test_basic(self):
        self.assertAlmostEqual(compute_pnl_percent(10.0, 13.0), 30.0)
        self.assertAlmostEqual(compute_pnl_percent(10.0, 5.0), -50.0)

    def test_invalid(self):
        self.assertIsNone(compute_pnl_percent(0, 5))
        self.assertIsNone(compute_pnl_percent(10, None))


if __name__ == '__main__':
    unittest.main()
