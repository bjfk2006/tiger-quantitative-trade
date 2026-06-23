# -*- coding: utf-8 -*-
"""PositionStateMachine 单测：开仓/平仓/幂等/降级。对应设计 §5。

适配层全部 mock，不触达 SDK / 网络。
"""
import unittest
from unittest.mock import MagicMock

from option_bot.adapters.errors import OpenRejected
from option_bot.domain.models import (BotState, CloseReason, Direction,
                                      OptionPick, PositionView, StrategyConfig)
from option_bot.strategy.state_machine import PositionStateMachine


def make_pick():
    return OptionPick(symbol='AAPL', expiry='20250815', strike=200.0,
                      put_call='CALL', identifier='AAPL  250815C00200000')


def make_position(qty=1, salable=1, pnl_pct=10.0):
    return PositionView(quantity=qty, salable_qty=salable, average_cost=8.0,
                        market_price=8.8, unrealized_pnl=80.0,
                        unrealized_pnl_percent=pnl_pct)


class TestStateMachine(unittest.TestCase):
    def setUp(self):
        self.td = MagicMock()
        self.md = MagicMock()
        self.store = MagicMock()
        self.cfg = StrategyConfig(max_qty=2, fill_timeout=5, fill_poll_interval=0)
        self.td.account = 'paper-1'
        self.td.new_dedup_tag.return_value = 'obot-test'
        self.sm = PositionStateMachine(self.td, self.md, self.store, self.cfg,
                                       sleep=lambda *_: None, now_ms=lambda: 1000)

    def test_open_happy_path(self):
        self.md.get_option_quote.return_value = {'bid_price': 8.0, 'ask_price': 8.1}
        self.md.is_market_trading.return_value = True
        self.td.open_market.return_value = 9001
        self.td.get_order_status.return_value = {
            'status': 'FILLED', 'filled': 1, 'remaining': 0, 'avg_fill_price': 8.05}

        oid = self.sm.open(make_pick(), Direction.LONG, 1)

        self.assertEqual(oid, 9001)
        self.assertEqual(self.sm.state, BotState.MONITORING)
        self.assertEqual(self.sm.entry_price, 8.05)
        self.td.open_market.assert_called_once()

    def test_open_rejected_when_not_trading(self):
        self.md.get_option_quote.return_value = {'bid_price': 8.0, 'ask_price': 8.1}
        self.md.is_market_trading.return_value = False
        with self.assertRaises(OpenRejected):
            self.sm.open(make_pick(), Direction.LONG, 1)
        self.td.open_market.assert_not_called()

    def test_open_rejected_qty_exceeds_max(self):
        with self.assertRaises(OpenRejected):
            self.sm.open(make_pick(), Direction.LONG, 99)

    def test_open_direction_mismatch(self):
        # SHORT 必须对 PUT；用 CALL 的 pick 应被拒
        with self.assertRaises(OpenRejected):
            self.sm.open(make_pick(), Direction.SHORT, 1)

    def test_close_happy_path(self):
        self.sm.pick = make_pick()
        self.sm.direction = Direction.LONG
        self.sm.state = BotState.MONITORING
        self.td.get_option_position.return_value = make_position(qty=1, salable=1)
        self.td.close_market.return_value = 9002
        self.td.get_order_status.return_value = {
            'status': 'FILLED', 'filled': 1, 'remaining': 0, 'avg_fill_price': 9.0}

        oid = self.sm.close(CloseReason.TAKE_PROFIT)

        self.assertEqual(oid, 9002)
        self.assertEqual(self.sm.state, BotState.CLOSED)
        self.store.clear.assert_called_once()
        self.td.close_market.assert_called_once()

    def test_close_idempotent_when_no_position(self):
        # 平仓前发现已无可卖量 -> 直接 CLOSED，不再下单（幂等关键）
        self.sm.pick = make_pick()
        self.sm.state = BotState.MONITORING
        self.td.get_option_position.return_value = None

        self.sm.close(CloseReason.STOP_LOSS)

        self.assertEqual(self.sm.state, BotState.CLOSED)
        self.td.close_market.assert_not_called()

    def test_close_noop_when_already_closed(self):
        self.sm.state = BotState.CLOSED
        self.assertIsNone(self.sm.close(CloseReason.TIME_FORCE_CLOSE))
        self.td.close_market.assert_not_called()

    def test_close_unconfirmed_fill_does_not_mark_closed(self):
        # 回归 #1: 成交状态未知(remaining=None) 时绝不能标记 CLOSED 丢快照
        self.sm.pick = make_pick()
        self.sm.state = BotState.MONITORING
        self.td.get_option_position.return_value = make_position(qty=1, salable=1)
        self.td.close_market.return_value = 9003
        self.td.get_order_status.return_value = {
            'status': 'UNKNOWN', 'filled': 0, 'remaining': None, 'avg_fill_price': 0}
        from option_bot.adapters.errors import CloseRejected
        with self.assertRaises(CloseRejected):
            self.sm.close(CloseReason.STOP_LOSS)
        self.assertEqual(self.sm.state, BotState.MONITORING)  # 退回监控以便重试
        self.store.clear.assert_not_called()                  # 快照未被丢弃

    def test_time_force_close_bypasses_auto_close_switch(self):
        # 回归 #2: enable_auto_close=False 不应禁用时间强平
        cfg = StrategyConfig(max_qty=2, fill_timeout=5, fill_poll_interval=0,
                             enable_auto_close=False)
        sm = PositionStateMachine(self.td, self.md, self.store, cfg,
                                  sleep=lambda *_: None, now_ms=lambda: 1000)
        sm.pick = make_pick()
        sm.state = BotState.MONITORING
        self.td.get_option_position.return_value = make_position(qty=1, salable=1)
        self.td.close_market.return_value = 9004
        self.td.get_order_status.return_value = {
            'status': 'FILLED', 'filled': 1, 'remaining': 0, 'avg_fill_price': 9.0}
        sm.close(CloseReason.TIME_FORCE_CLOSE)
        self.assertEqual(sm.state, BotState.CLOSED)
        self.td.close_market.assert_called_once()

    def test_auto_close_switch_blocks_take_profit(self):
        cfg = StrategyConfig(enable_auto_close=False)
        sm = PositionStateMachine(self.td, self.md, self.store, cfg,
                                  sleep=lambda *_: None, now_ms=lambda: 1000)
        sm.pick = make_pick()
        sm.state = BotState.MONITORING
        self.assertIsNone(sm.close(CloseReason.TAKE_PROFIT))
        self.td.close_market.assert_not_called()

    def test_open_partial_fill_enters_monitoring(self):
        # 回归 #3: 部分成交必须纳入盯盘（filled 数量），不得遗弃
        cfg = StrategyConfig(max_qty=3, fill_timeout=5, fill_poll_interval=0)
        sm = PositionStateMachine(self.td, self.md, self.store, cfg,
                                  sleep=lambda *_: None, now_ms=lambda: 1000)
        self.md.get_option_quote.return_value = {'bid_price': 8.0, 'ask_price': 8.1}
        self.md.is_market_trading.return_value = True
        self.td.open_market.return_value = 9005
        self.td.get_order_status.return_value = {
            'status': 'HELD', 'filled': 1, 'remaining': 2, 'avg_fill_price': 8.05}
        sm.open(make_pick(), Direction.LONG, 3)   # qty 3 ≤ max_qty 3
        self.assertEqual(sm.state, BotState.MONITORING)
        self.assertEqual(sm.qty, 1)  # 按已成交数量

    def test_open_invokes_sink(self):
        # 持久化解耦：开仓成交后应调用 sink.on_open（NullSink 默认不破坏其他用例）
        sink = MagicMock()
        sm = PositionStateMachine(self.td, self.md, self.store, self.cfg,
                                  sleep=lambda *_: None, now_ms=lambda: 1000, sink=sink)
        self.md.get_option_quote.return_value = {'bid_price': 8.0, 'ask_price': 8.1}
        self.md.is_market_trading.return_value = True
        self.td.open_market.return_value = 9001
        self.td.get_order_status.return_value = {
            'status': 'FILLED', 'filled': 1, 'remaining': 0, 'avg_fill_price': 8.05}
        sm.open(make_pick(), Direction.LONG, 1)
        sink.on_open.assert_called_once()

    def test_resume_rejects_account_mismatch(self):
        # 回归 M1: 快照账户与当前账户不一致 -> 丢弃快照、不认领、不查持仓
        from option_bot.domain.models import TradeSnapshot
        snap = TradeSnapshot(
            account='other-acc-999', direction='LONG', pick=make_pick().__dict__,
            qty=1, entry_price=8.0, tp_percent=30, sl_percent=50,
            close_buffer_minutes=5, open_order_id=1, external_id='x',
            state='MONITORING', opened_at=1000)
        self.store.load.return_value = snap
        self.td.account = 'paper-1'   # 当前账户 != 快照账户
        self.assertFalse(self.sm.resume())
        self.store.clear.assert_called_once()
        self.td.get_option_position.assert_not_called()

    def test_resume_adopts_when_account_matches(self):
        from option_bot.domain.models import TradeSnapshot
        snap = TradeSnapshot(
            account='paper-1', direction='LONG', pick=make_pick().__dict__,
            qty=1, entry_price=8.0, tp_percent=30, sl_percent=50,
            close_buffer_minutes=5, open_order_id=1, external_id='x',
            state='MONITORING', opened_at=1000)
        self.store.load.return_value = snap
        self.td.account = 'paper-1'
        self.td.get_option_position.return_value = make_position(qty=1)
        self.assertTrue(self.sm.resume())
        self.assertEqual(self.sm.state, BotState.MONITORING)

    def test_current_pnl_uses_position_percent(self):
        self.sm.pick = make_pick()
        self.td.get_option_position.return_value = make_position(pnl_pct=25.0)
        pnl, pos = self.sm.current_pnl_percent()
        self.assertEqual(pnl, 25.0)

    def test_current_pnl_degrades_to_quote(self):
        # 持仓 pnl% 缺失 -> 用行情 mid 与入场价估算
        self.sm.pick = make_pick()
        self.sm.entry_price = 8.0
        self.td.get_option_position.return_value = make_position(pnl_pct=None)
        self.md.get_option_quote.return_value = {'bid_price': 9.9, 'ask_price': 10.1}
        pnl, _ = self.sm.current_pnl_percent()
        self.assertAlmostEqual(pnl, 25.0)  # (10-8)/8*100


if __name__ == '__main__':
    unittest.main()
