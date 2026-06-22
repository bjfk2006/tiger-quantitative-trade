# -*- coding: utf-8 -*-
"""MarketClock 单测：收盘时刻推算（注入时钟，不依赖真实时间）。对应设计 §5 Flow 5.4。"""
import datetime as dt
import unittest

import pytz

from option_bot.domain.models import StrategyConfig
from option_bot.strategy.market_clock import MarketClock

ET = pytz.timezone('America/New_York')


class FakeMarketData:
    def __init__(self, trading=True):
        self._trading = trading

    def trading_days(self, market, begin_date, end_date):
        t = 'TRADING' if self._trading else 'NON_TRADING'
        return [{'date': begin_date, 'type': t}]


def _clock_at(y, mo, d, h, mi):
    return lambda: ET.localize(dt.datetime(y, mo, d, h, mi))


class TestMarketClock(unittest.TestCase):
    def test_minutes_before_regular_close(self):
        cfg = StrategyConfig()
        mc = MarketClock(FakeMarketData(True), cfg, clock=_clock_at(2025, 8, 15, 15, 57))
        # 16:00 收盘 - 15:57 = 3 分钟
        self.assertAlmostEqual(mc.minutes_to_close(), 3.0, places=3)

    def test_after_close_returns_none(self):
        cfg = StrategyConfig()
        mc = MarketClock(FakeMarketData(True), cfg, clock=_clock_at(2025, 8, 15, 16, 1))
        self.assertIsNone(mc.minutes_to_close())

    def test_non_trading_day_returns_none(self):
        cfg = StrategyConfig()
        mc = MarketClock(FakeMarketData(False), cfg, clock=_clock_at(2025, 8, 16, 15, 50))
        self.assertIsNone(mc.minutes_to_close())

    def test_early_close_half_day(self):
        cfg = StrategyConfig(early_close_dates={'2025-11-28': '13:00'})
        mc = MarketClock(FakeMarketData(True), cfg, clock=_clock_at(2025, 11, 28, 12, 58))
        # 半日市 13:00 收盘 - 12:58 = 2 分钟
        self.assertAlmostEqual(mc.minutes_to_close(), 2.0, places=3)

    def test_far_from_close(self):
        cfg = StrategyConfig()
        mc = MarketClock(FakeMarketData(True), cfg, clock=_clock_at(2025, 8, 15, 10, 0))
        self.assertAlmostEqual(mc.minutes_to_close(), 360.0, places=3)

    def test_trading_day_cached(self):
        md = FakeMarketData(True)
        calls = {'n': 0}
        orig = md.trading_days
        def counting(*a, **k):
            calls['n'] += 1
            return orig(*a, **k)
        md.trading_days = counting
        mc = MarketClock(md, StrategyConfig(), clock=_clock_at(2025, 8, 15, 15, 0))
        mc.minutes_to_close()
        mc.minutes_to_close()
        self.assertEqual(calls['n'], 1)  # 同一天只查一次日历


if __name__ == '__main__':
    unittest.main()
