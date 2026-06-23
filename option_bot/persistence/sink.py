# -*- coding: utf-8 -*-
"""事件 sink（设计增量 §3/§4）：把交易/持仓事件写持久化。

strategy 层只认 EventSink 抽象（默认 NullSink），不直接依赖 sqlite——
保证既有 CLI 行为与单测零变化。SqliteSink 才真正落库。
"""
import logging
from option_bot.strategy.risk_guard import compute_pnl_percent

logger = logging.getLogger('option_bot.sink')


class EventSink:
    """事件下沉抽象。所有方法对入参取最小必要信息，便于不同实现。"""

    def on_open(self, account, pick, direction, qty, entry_price, order_id):
        ...

    def on_position(self, account, pick, direction, entry_price, view):
        ...

    def on_close(self, account, pick, direction, qty, price, reason, order_id, entry_price):
        ...

    def on_position_closed(self, identifier):
        ...


class NullSink(EventSink):
    """默认空实现：不持久化（CLI 一次性运行 / 单测用）。"""

    def on_open(self, *a, **k):
        return None

    def on_position(self, *a, **k):
        return None

    def on_close(self, *a, **k):
        return None

    def on_position_closed(self, *a, **k):
        return None


class SqliteSink(EventSink):
    """落 SQLite：trades(开/平历史) + positions(当前快照) + position_ticks(逐tick时序)。"""

    def __init__(self, repo, tick_retention_days=7):
        self._repo = repo
        self._tick_retention_days = tick_retention_days
        self._tick_count = 0

    def on_open(self, account, pick, direction, qty, entry_price, order_id):
        d = direction.value if hasattr(direction, 'value') else str(direction)
        self._repo.insert_trade(
            account=account, identifier=pick.identifier, symbol=pick.symbol,
            direction=d, action='OPEN', qty=qty, price=entry_price,
            reason='OPEN', order_id=order_id, pnl_percent=None)
        self._repo.upsert_position(
            identifier=pick.identifier, account=account, symbol=pick.symbol,
            direction=d, qty=qty, entry_price=entry_price, market_price=None,
            unrealized_pnl=None, unrealized_pnl_percent=None, state='MONITORING')

    def on_position(self, account, pick, direction, entry_price, view):
        d = direction.value if hasattr(direction, 'value') else str(direction)
        self._repo.upsert_position(
            identifier=pick.identifier, account=account, symbol=pick.symbol,
            direction=d, qty=view.quantity, entry_price=entry_price,
            market_price=view.market_price, unrealized_pnl=view.unrealized_pnl,
            unrealized_pnl_percent=view.unrealized_pnl_percent, state='MONITORING')
        # 逐tick时序：每 tick 追加一条，供「持仓走势」密集曲线
        self._repo.insert_position_tick(
            account=account, identifier=pick.identifier, symbol=pick.symbol,
            market_price=view.market_price, unrealized_pnl=view.unrealized_pnl,
            unrealized_pnl_percent=view.unrealized_pnl_percent)
        # 定期清理（每约 600 tick≈20 分钟），按保留天数删旧
        self._tick_count += 1
        if self._tick_count % 600 == 0:
            import time as _t
            cutoff = int(_t.time() * 1000) - self._tick_retention_days * 86400 * 1000
            try:
                self._repo.prune_position_ticks(cutoff)
            except Exception:  # noqa: BLE001
                pass

    def on_close(self, account, pick, direction, qty, price, reason, order_id, entry_price):
        d = direction.value if hasattr(direction, 'value') else str(direction)
        r = reason.value if hasattr(reason, 'value') else str(reason)
        # 已实现盈亏%：平仓成交价相对入场价（买入开仓，做多/做空公式一致）
        pnl = compute_pnl_percent(entry_price, price)
        self._repo.insert_trade(
            account=account, identifier=pick.identifier, symbol=pick.symbol,
            direction=d, action='CLOSE', qty=qty, price=price,
            reason=r, order_id=order_id, pnl_percent=pnl)

    def on_position_closed(self, identifier):
        self._repo.delete_position(identifier)
