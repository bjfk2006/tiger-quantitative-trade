# -*- coding: utf-8 -*-
"""交易适配层：封装 tigeropen.TradeClient（设计文档 §3 TradingAdapter / §8 最小权限）。

只暴露 BUY-to-open 与 SELL-to-close 两类市价单——从源头杜绝裸卖期权/多腿越权。
对应 SDK: order_utils.market_order:12 / trade_client.place_order:919 /
          get_order:709 / get_positions:259 / contract_utils.option_contract_by_symbol:19
"""
import logging
import uuid

from tigeropen.common.consts import SecurityType
from tigeropen.common.exceptions import ApiException, RequestException, ResponseException
from tigeropen.common.util.contract_utils import option_contract_by_symbol
from tigeropen.common.util.order_utils import combo_order, market_order
from tigeropen.trade.domain.contract import OrderContractLeg

from option_bot.adapters.errors import CloseRejected, DataUnavailable, OpenRejected
from option_bot.domain.models import OptionPick, PositionView

logger = logging.getLogger('option_bot.trade')


class TradingAdapter:
    def __init__(self, trade_client, account):
        if not account:
            raise ValueError('account 不能为空——所有下单必须显式带 account')
        self._tc = trade_client
        self._account = account

    @property
    def account(self):
        return self._account

    def _build_contract(self, pick: OptionPick):
        return option_contract_by_symbol(
            symbol=pick.symbol, expiry=pick.expiry, strike=pick.strike,
            put_call=pick.put_call, currency=pick.currency, multiplier=pick.multiplier,
        )

    @staticmethod
    def new_dedup_tag() -> str:
        """生成下单去重/追踪标记，写入 order.user_mark（设计文档 §5 幂等）。"""
        return 'obot-' + uuid.uuid4().hex[:16]

    def open_market(self, pick: OptionPick, qty: int, user_mark: str) -> int:
        """BUY-to-open 市价开仓（做多买Call/做空买Put 都走这里）。返回全局订单 id。"""
        contract = self._build_contract(pick)
        order = market_order(self._account, contract, 'BUY', qty)
        order.user_mark = user_mark
        try:
            order_id = self._tc.place_order(order)
        except (ApiException, RequestException, ResponseException) as e:
            raise OpenRejected(f'开仓下单被拒: {e}')
        if not order_id:
            raise OpenRejected('开仓下单未返回订单 id')
        logger.info('开仓已提交 order_id=%s mark=%s', order_id, user_mark)
        return order_id

    def close_market(self, pick: OptionPick, qty: int, user_mark: str) -> int:
        """SELL-to-close 市价平仓。返回全局订单 id。"""
        contract = self._build_contract(pick)
        order = market_order(self._account, contract, 'SELL', qty)
        order.user_mark = user_mark
        try:
            order_id = self._tc.place_order(order)
        except (ApiException, RequestException, ResponseException) as e:
            raise CloseRejected(f'平仓下单被拒: {e}')
        if not order_id:
            raise CloseRejected('平仓下单未返回订单 id')
        logger.info('平仓已提交 order_id=%s mark=%s', order_id, user_mark)
        return order_id

    def place_combo(self, symbol, expiry, legs, combo_type, action, qty,
                    limit_price, user_mark, currency='USD', multiplier=100, market='US'):
        """多腿组合**净限价**下单（铁鹰用，定义风险整笔成交）。返回订单 id。

        legs: [{'put_call','side'(BUY/SELL),'strike','ratio'}]。
        limit_price 为**组合净价**（卖方信用价差取负=收款，见 SDK 示例 limit_price=-2.52）。
        ⚠️ 净价正负/combo action 的具体约定须先在 **paper 账户**验证后再实盘。
        """
        contract_legs = [
            OrderContractLeg(symbol=symbol, sec_type='OPT', expiry=expiry,
                             strike=str(lg['strike']), put_call=lg['put_call'].upper(),
                             action=lg['side'].upper(), ratio=int(lg.get('ratio', 1)),
                             market=market, currency=currency, multiplier=multiplier)
            for lg in legs]
        order = combo_order(self._account, contract_legs, combo_type, action, qty,
                            limit_price=limit_price)
        order.user_mark = user_mark
        try:
            order_id = self._tc.place_order(order)
        except (ApiException, RequestException, ResponseException) as e:
            raise OpenRejected(f'组合下单被拒: {e}')
        if not order_id:
            raise OpenRejected('组合下单未返回订单 id')
        logger.info('组合单已提交 order_id=%s type=%s action=%s qty=%s limit=%s mark=%s',
                    order_id, combo_type, action, qty, limit_price, user_mark)
        return order_id

    def get_order_status(self, order_id: int) -> dict:
        """返回 {'status': str, 'filled': float, 'remaining': float, 'avg_fill_price': float}。"""
        try:
            order = self._tc.get_order(account=self._account, id=order_id)
        except (ApiException, RequestException, ResponseException) as e:
            raise DataUnavailable(f'查询订单失败: {e}')
        if order is None:
            raise DataUnavailable(f'订单不存在: {order_id}')
        status = order.status
        status_name = status.name if hasattr(status, 'name') else str(status)
        return {
            'status': status_name,
            'filled': order.filled,
            'remaining': order.remaining,
            'avg_fill_price': order.avg_fill_price,
        }

    def get_option_position(self, pick: OptionPick):
        """查指定期权持仓，返回 PositionView 或 None（无持仓）。

        仅按 symbol + sec_type 向服务端查询，再在本地按行权价(浮点容差)与
        put_call 精确匹配——避免 strike/expiry 的字符串格式差异导致漏匹配。
        unrealized_pnl_percent 由 SDK 的小数（0.30）转为百分数（30.0）。
        """
        try:
            positions = self._tc.get_positions(
                account=self._account,
                sec_type=SecurityType.OPT,
                symbol=pick.symbol,
            )
        except (ApiException, RequestException, ResponseException) as e:
            raise DataUnavailable(f'查询持仓失败: {e}')
        for p in positions or []:
            if not getattr(p, 'quantity', 0):
                continue
            if not self._matches_pick(p, pick):
                continue
            pct = getattr(p, 'unrealized_pnl_percent', None)
            return PositionView(
                quantity=p.quantity,
                salable_qty=getattr(p, 'salable_qty', None) or p.quantity,
                average_cost=getattr(p, 'average_cost', None),
                market_price=getattr(p, 'market_price', None),
                unrealized_pnl=getattr(p, 'unrealized_pnl', None),
                unrealized_pnl_percent=(pct * 100.0) if pct is not None else None,
            )
        return None

    @staticmethod
    def _matches_pick(position, pick: OptionPick) -> bool:
        """按行权价(浮点容差)+put_call 匹配持仓与选定期权。"""
        contract = getattr(position, 'contract', None)
        if contract is None:
            return False
        pc = str(getattr(contract, 'put_call', '') or '').upper()
        if pc != pick.put_call.upper():
            return False
        try:
            return abs(float(getattr(contract, 'strike', None)) - float(pick.strike)) < 1e-6
        except (TypeError, ValueError):
            return False
