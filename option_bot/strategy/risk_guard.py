# -*- coding: utf-8 -*-
"""风控判定（设计文档 §10）。纯逻辑，无 SDK 依赖。

评估优先级（硬性）：时间强平 > 止损 > 止盈 > 持有。
阈值单位为百分数：tp_percent=30 表示 +30%，sl_percent=50 表示 -50%。
"""
import logging

from option_bot.domain.models import CloseReason

logger = logging.getLogger('option_bot.risk')


def compute_pnl_percent(entry_price, current_price):
    """买入开仓的持仓盈亏%（做多Call/做空Put 都是买入，公式一致）。

    返回百分数（如 25.0 表示 +25%）。入参非法返回 None。
    """
    if not entry_price or entry_price <= 0 or current_price is None:
        return None
    return (current_price - entry_price) / entry_price * 100.0


class RiskGuard:
    def __init__(self, config):
        self._cfg = config

    def evaluate(self, pnl_percent, minutes_to_close):
        """返回 CloseReason 或 None（持有）。

        :param pnl_percent: 持仓未实现盈亏%（百分数），None 表示暂不可得。
        :param minutes_to_close: 距收盘分钟数，None 表示非交易日/已收盘。
        """
        # ① 时间强平：无条件，最高优先级
        if minutes_to_close is not None and minutes_to_close <= self._cfg.close_buffer_minutes:
            return CloseReason.TIME_FORCE_CLOSE
        if pnl_percent is None:
            return None
        # ② 止损
        if pnl_percent <= -self._cfg.sl_percent:
            return CloseReason.STOP_LOSS
        # ③ 止盈
        if pnl_percent >= self._cfg.tp_percent:
            return CloseReason.TAKE_PROFIT
        # ④ 持有
        return None

    def pre_open_check(self, quote, market_trading):
        """开仓前预检：RTH + 点差/流动性。返回 (ok: bool, reason: str)。"""
        if not self._cfg.enable_open:
            return False, '开仓开关已关闭(enable_open=False)'
        if not market_trading:
            return False, '当前非常规交易时段(RTH)，拒绝市价开仓'
        if not quote:
            return False, '无法获取期权盘口行情，拒绝市价开仓'
        bid = quote.get('bid_price')
        ask = quote.get('ask_price')
        if not bid or not ask or bid <= 0 or ask <= 0:
            return False, '盘口买卖价缺失或为0(流动性不足)，拒绝市价开仓'
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0
        if spread_pct > self._cfg.max_spread_pct:
            return False, (f'相对点差 {spread_pct:.1f}% 超过上限 '
                           f'{self._cfg.max_spread_pct}%，市价单滑点风险过大')
        return True, 'ok'
