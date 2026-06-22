# -*- coding: utf-8 -*-
"""行情适配层：封装 tigeropen.QuoteClient（设计文档 §3 MarketDataAdapter）。

把 SDK 的 DataFrame/list 归一化成本程序的简单结构，并把 SDK 异常翻译为领域错误。
对应 SDK: quote_client.py:924(expirations) / 963(chain) / 1050(briefs) /
          139(market_status) / 2736(trading_calendar)
"""
import logging

from tigeropen.common.consts import Market
from tigeropen.common.exceptions import RequestException, ResponseException
from tigeropen.common.exceptions import ApiException

from option_bot.adapters.errors import DataUnavailable
from option_bot.domain.models import OptionPick

logger = logging.getLogger('option_bot.market')


class MarketDataAdapter:
    def __init__(self, quote_client):
        self._qc = quote_client

    def list_expirations(self, symbol, market=Market.US):
        """返回 [{'date': 'YYYY-MM-DD', 'timestamp': ms, 'period_tag': ...}]。"""
        try:
            df = self._qc.get_option_expirations(symbol, market=market)
        except (RequestException, ResponseException, ApiException) as e:
            raise DataUnavailable(f'获取到期日失败: {e}')
        if df is None or df.empty:
            return []
        cols = [c for c in ('date', 'timestamp', 'period_tag') if c in df.columns]
        return df[cols].to_dict('records')

    def get_chain(self, symbol, expiry, put_call=None, market=Market.US):
        """返回期权链 records；put_call 给定时按方向过滤（CALL/PUT）。"""
        try:
            df = self._qc.get_option_chain(symbol, expiry, market=market)
        except (RequestException, ResponseException, ApiException) as e:
            raise DataUnavailable(f'获取期权链失败: {e}')
        if df is None or df.empty:
            return []
        if put_call and 'put_call' in df.columns:
            df = df[df['put_call'] == put_call]
        return df.to_dict('records')

    def get_option_quote(self, identifier, market=Market.US):
        """获取单个期权实时行情，返回 dict（含 latest_price/mid_price/bid/ask）。"""
        try:
            df = self._qc.get_option_briefs(identifier, market=market)
        except (RequestException, ResponseException, ApiException) as e:
            raise DataUnavailable(f'获取期权行情失败: {e}')
        if df is None or df.empty:
            return None
        return df.iloc[0].to_dict()

    def resolve_pick(self, symbol, expiry, strike, direction, market=Market.US):
        """从期权链定位用户/服务选定的单腿期权，返回 OptionPick。

        :param direction: domain.models.Direction（LONG→CALL / SHORT→PUT）
        :param expiry: 'YYYY-MM-DD'（链查询用）；OptionPick.expiry 归一化为 'YYYYMMDD'
        """
        put_call = direction.put_call
        rows = self.get_chain(symbol, expiry, put_call=put_call, market=market)
        for r in rows:
            try:
                same_strike = abs(float(r.get('strike')) - float(strike)) < 1e-6
            except (TypeError, ValueError):
                continue
            if same_strike and str(r.get('put_call')).upper() == put_call:
                return OptionPick(
                    symbol=symbol,
                    expiry=str(expiry).replace('-', '').strip(),
                    strike=float(r.get('strike')),
                    put_call=put_call,
                    identifier=str(r.get('identifier')).strip(),
                    multiplier=int(r.get('multiplier') or 100),
                )
        raise DataUnavailable(
            f'未在 {symbol} {expiry} {put_call} 链中找到行权价 {strike} 的期权')

    def is_market_trading(self, market='US'):
        """券商侧市场状态判断：是否处于 RTH 交易中。"""
        try:
            statuses = self._qc.get_market_status(market)
        except (RequestException, ResponseException, ApiException) as e:
            raise DataUnavailable(f'获取市场状态失败: {e}')
        for st in statuses or []:
            if str(getattr(st, 'market', '')).upper() == str(market).upper():
                return str(getattr(st, 'trading_status', '')).upper() == 'TRADING'
        return False

    def trading_days(self, market, begin_date, end_date):
        """返回区间内 [{'date': 'YYYY-MM-DD', 'type': 'TRADING'|'NON_TRADING'}]。"""
        try:
            cal = self._qc.get_trading_calendar(market, begin_date=begin_date, end_date=end_date)
        except (RequestException, ResponseException, ApiException) as e:
            raise DataUnavailable(f'获取交易日历失败: {e}')
        return cal or []
