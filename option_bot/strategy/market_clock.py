# -*- coding: utf-8 -*-
"""收盘时刻推算（设计文档 §5 Flow 5.4 / §10 R5）。

SDK 不提供收盘时间：get_market_status 只给下次开盘、get_trading_calendar 只给
日期类型。故本地推算：NASDAQ/NYSE 常规收盘 16:00 美东，半日市 13:00（可配），
用交易日历过滤非交易日。日历取不到时保守按常规收盘计算（宁可早平不可漏平）。
"""
import datetime as dt
import logging

import pytz

from option_bot.adapters.errors import DataUnavailable

logger = logging.getLogger('option_bot.clock')


class MarketClock:
    def __init__(self, market_data_adapter, config, clock=None):
        """:param clock: 可注入的「当前时间」函数（返回 tz-aware datetime），便于单测。"""
        self._md = market_data_adapter
        self._cfg = config
        self._tz = pytz.timezone(config.timezone)
        self._clock = clock or (lambda: dt.datetime.now(self._tz))
        self._trading_day_cache = {}   # 'YYYY-MM-DD' -> bool

    def _parse_hhmm(self, s):
        hh, mm = s.split(':')
        return int(hh), int(mm)

    def _close_time_for(self, date_str):
        """返回该日期的收盘 'HH:MM'：命中半日市表用其值，否则常规收盘。"""
        return self._cfg.early_close_dates.get(date_str, self._cfg.regular_close)

    def is_trading_day(self, date_str):
        """查交易日历判断是否交易日；失败时保守视为交易日并告警。"""
        if date_str in self._trading_day_cache:
            return self._trading_day_cache[date_str]
        try:
            # end_date 不包含，故取次日
            d = dt.datetime.strptime(date_str, '%Y-%m-%d').date()
            nxt = (d + dt.timedelta(days=1)).strftime('%Y-%m-%d')
            days = self._md.trading_days(self._cfg.market, date_str, nxt)
            trading = True  # 默认保守为交易日
            for item in days:
                if item.get('date') == date_str:
                    trading = (item.get('type') == 'TRADING')
                    break
            self._trading_day_cache[date_str] = trading
            return trading
        except DataUnavailable as e:
            logger.warning('交易日历不可用，保守视为交易日: %s', e)
            return True

    def minutes_to_close(self, now=None):
        """距今日收盘的分钟数（float）。非交易日或已过收盘返回 None。"""
        now = now or self._clock()
        if now.tzinfo is None:
            now = self._tz.localize(now)
        else:
            now = now.astimezone(self._tz)
        date_str = now.strftime('%Y-%m-%d')
        if not self.is_trading_day(date_str):
            return None
        hh, mm = self._parse_hhmm(self._close_time_for(date_str))
        naive_close = dt.datetime(now.year, now.month, now.day, hh, mm)
        close_dt = self._tz.localize(naive_close)
        if now >= close_dt:
            return None
        return (close_dt - now).total_seconds() / 60.0
