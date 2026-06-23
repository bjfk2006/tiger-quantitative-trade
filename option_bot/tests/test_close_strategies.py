# -*- coding: utf-8 -*-
"""可插拔平仓策略单测（纯逻辑）。设计：2026-06-23-pluggable-close-strategy.md。"""
import unittest

from option_bot.domain.models import CloseReason, StrategyConfig
from option_bot.strategy.close_strategies import (BracketStrategy,
                                                  BreakevenStrategy,
                                                  StrategyContext,
                                                  ThresholdStrategy,
                                                  TimeInTradeStrategy,
                                                  TrailingStrategy,
                                                  build_strategy)


def ctx(pnl, mtc=None, opened_at=None, now_ts=None):
    return StrategyContext(pnl_percent=pnl, minutes_to_close=mtc,
                           opened_at=opened_at, now_ts=now_ts)


class TestThreshold(unittest.TestCase):
    def setUp(self):
        self.s = ThresholdStrategy(close_buffer_minutes=5, sl_percent=50, tp_percent=30)

    def test_take_profit(self):
        self.assertEqual(self.s.decide(ctx(30, 100)), CloseReason.TAKE_PROFIT)

    def test_stop_loss(self):
        self.assertEqual(self.s.decide(ctx(-50, 100)), CloseReason.STOP_LOSS)

    def test_time_force_highest(self):
        # 即使盈亏在阈值内，距收盘<=buffer 也强平；亏到止损也是时间优先
        self.assertEqual(self.s.decide(ctx(0, 5)), CloseReason.TIME_FORCE_CLOSE)
        self.assertEqual(self.s.decide(ctx(-80, 3)), CloseReason.TIME_FORCE_CLOSE)

    def test_hold(self):
        self.assertIsNone(self.s.decide(ctx(10, 100)))

    def test_none_pnl_only_time(self):
        self.assertIsNone(self.s.decide(ctx(None, 100)))
        self.assertEqual(self.s.decide(ctx(None, 1)), CloseReason.TIME_FORCE_CLOSE)


class TestTrailing(unittest.TestCase):
    def setUp(self):
        # 武装 20%，从峰值回撤 10 个点平仓；硬止损 50%；收盘前 5 分钟强平
        self.s = TrailingStrategy(close_buffer_minutes=5, sl_percent=50,
                                  trail_activation=20, trail_giveback=10)

    def test_not_armed_below_activation(self):
        self.assertIsNone(self.s.decide(ctx(15, 100)))
        self.assertFalse(self.s.armed)

    def test_arms_and_holds(self):
        self.assertIsNone(self.s.decide(ctx(22, 100)))   # 武装, 峰值22
        self.assertTrue(self.s.armed)
        self.assertEqual(self.s.peak, 22)
        self.assertIsNone(self.s.decide(ctx(25, 100)))   # 峰值上移到25, 未回撤够

    def test_trailing_exit_after_giveback(self):
        self.s.decide(ctx(20, 100))   # arm, peak=20
        self.s.decide(ctx(35, 100))   # peak=35
        # 回撤到 25 = peak35-10 -> 触发
        self.assertEqual(self.s.decide(ctx(25, 100)), CloseReason.TRAILING_STOP)

    def test_example_20_then_back_to_10(self):
        # 你的场景：涨破+20%武装，峰值20回落到10(=20-10)即平
        self.s.decide(ctx(20, 100))   # arm, peak=20
        self.assertEqual(self.s.decide(ctx(10, 100)), CloseReason.TRAILING_STOP)

    def test_hard_stop_still_works_before_arm(self):
        self.assertEqual(self.s.decide(ctx(-50, 100)), CloseReason.STOP_LOSS)

    def test_time_force_still_works(self):
        self.s.decide(ctx(30, 100))  # armed
        self.assertEqual(self.s.decide(ctx(28, 3)), CloseReason.TIME_FORCE_CLOSE)

    def test_state_roundtrip(self):
        self.s.decide(ctx(25, 100))  # arm peak=25
        st = self.s.state()
        self.assertEqual(st, {'armed': True, 'peak': 25})
        s2 = TrailingStrategy(5, 50, 20, 10)
        s2.load_state(st)
        self.assertTrue(s2.armed)
        self.assertEqual(s2.peak, 25)
        # 恢复后继续：回撤达标即平
        self.assertEqual(s2.decide(ctx(15, 100)), CloseReason.TRAILING_STOP)


class TestBreakeven(unittest.TestCase):
    def setUp(self):
        # 冲过 +20% 武装；回吐到 +5%(lock) 即平
        self.s = BreakevenStrategy(close_buffer_minutes=5, sl_percent=50,
                                   activation=20, lock=5)

    def test_not_armed_no_exit(self):
        self.assertIsNone(self.s.decide(ctx(8, 100)))   # 没到武装，回到8也不触发保本
        self.assertFalse(self.s.armed)

    def test_arm_then_lock_exit(self):
        self.assertIsNone(self.s.decide(ctx(25, 100)))  # 武装
        self.assertTrue(self.s.armed)
        self.assertEqual(self.s.decide(ctx(5, 100)), CloseReason.BREAKEVEN)  # 回吐到 lock

    def test_hard_stop_before_arm(self):
        self.assertEqual(self.s.decide(ctx(-50, 100)), CloseReason.STOP_LOSS)

    def test_state_roundtrip(self):
        self.s.decide(ctx(22, 100))
        s2 = BreakevenStrategy(5, 50, 20, 5)
        s2.load_state(self.s.state())
        self.assertTrue(s2.armed)
        self.assertEqual(s2.decide(ctx(4, 100)), CloseReason.BREAKEVEN)


class TestTimeInTrade(unittest.TestCase):
    def test_exit_after_max_hold(self):
        s = TimeInTradeStrategy(close_buffer_minutes=5, sl_percent=50, max_hold_minutes=30)
        # 持有 10 分钟 -> 不平
        self.assertIsNone(s.decide(ctx(5, 100, opened_at=0, now_ts=10 * 60000)))
        # 持有 30 分钟 -> 平
        self.assertEqual(s.decide(ctx(5, 100, opened_at=0, now_ts=30 * 60000)),
                         CloseReason.TIME_IN_TRADE)

    def test_safety_still_works(self):
        s = TimeInTradeStrategy(5, 50, 30)
        self.assertEqual(s.decide(ctx(-60, 100, opened_at=0, now_ts=0)), CloseReason.STOP_LOSS)


class TestBracket(unittest.TestCase):
    def test_priority_breakeven_over_trailing_and_tp(self):
        # 保本+移动止盈+固定止盈 同时配置；保本优先级最高
        s = BracketStrategy(close_buffer_minutes=5, sl_percent=50, tp_percent=40,
                            breakeven_activation=20, breakeven_lock=5,
                            trail_activation=20, trail_giveback=10, max_hold_minutes=0)
        s.decide(ctx(30, 100))   # 两者都武装, peak=30
        # 回到 5：保本(≤5)与移动止盈(≤30-10=20)都满足 → 保本优先
        self.assertEqual(s.decide(ctx(5, 100)), CloseReason.BREAKEVEN)

    def test_trailing_then_tp(self):
        s = BracketStrategy(5, 50, tp_percent=40, breakeven_activation=0, breakeven_lock=0,
                            trail_activation=20, trail_giveback=10, max_hold_minutes=0)
        s.decide(ctx(30, 100))  # trailing 武装 peak=30
        self.assertEqual(s.decide(ctx(20, 100)), CloseReason.TRAILING_STOP)  # 30-10
        s2 = BracketStrategy(5, 50, 40, 0, 0, 20, 10, 0)
        self.assertEqual(s2.decide(ctx(40, 100)), CloseReason.TAKE_PROFIT)   # 直接到止盈

    def test_components_disabled_when_zero(self):
        # 全部盈利组件关闭 → 只剩硬止损/时间强平
        s = BracketStrategy(5, 50, tp_percent=0, breakeven_activation=0, breakeven_lock=0,
                            trail_activation=0, trail_giveback=10, max_hold_minutes=0)
        self.assertIsNone(s.decide(ctx(100, 100)))          # 涨到100也不平(无止盈组件)
        self.assertEqual(s.decide(ctx(-50, 100)), CloseReason.STOP_LOSS)
        self.assertEqual(s.decide(ctx(0, 3)), CloseReason.TIME_FORCE_CLOSE)

    def test_time_in_trade_component(self):
        s = BracketStrategy(5, 50, tp_percent=0, breakeven_activation=0, breakeven_lock=0,
                            trail_activation=0, trail_giveback=10, max_hold_minutes=30)
        self.assertEqual(s.decide(ctx(5, 100, opened_at=0, now_ts=30 * 60000)),
                         CloseReason.TIME_IN_TRADE)

    def test_state_roundtrip(self):
        s = BracketStrategy(5, 50, 40, 20, 5, 20, 10, 0)
        s.decide(ctx(33, 100))
        s2 = BracketStrategy(5, 50, 40, 20, 5, 20, 10, 0)
        s2.load_state(s.state())
        self.assertTrue(s2.trail_armed)
        self.assertEqual(s2.peak, 33)


class TestBuild(unittest.TestCase):
    def test_build_threshold(self):
        s = build_strategy('threshold', StrategyConfig())
        self.assertIsInstance(s, ThresholdStrategy)

    def test_build_trailing(self):
        s = build_strategy('trailing', StrategyConfig(trail_activation=15, trail_giveback=5))
        self.assertIsInstance(s, TrailingStrategy)
        self.assertEqual(s.trail_activation, 15)

    def test_default_when_none(self):
        self.assertIsInstance(build_strategy(None, StrategyConfig()), ThresholdStrategy)

    def test_unknown_raises(self):
        with self.assertRaises(ValueError):
            build_strategy('magic', StrategyConfig())


if __name__ == '__main__':
    unittest.main()
