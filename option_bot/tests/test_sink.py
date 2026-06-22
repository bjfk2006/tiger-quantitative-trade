# -*- coding: utf-8 -*-
"""SqliteSink / NullSink 单测。对应设计增量 §3/§4。"""
import os
import tempfile
import unittest

from option_bot.domain.models import CloseReason, Direction, OptionPick, PositionView
from option_bot.persistence.db import SqliteRepo
from option_bot.persistence.sink import NullSink, SqliteSink


def make_pick():
    return OptionPick(symbol='AAPL', expiry='20250815', strike=200.0,
                      put_call='CALL', identifier='AAPL  250815C00200000')


class TestSqliteSink(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.repo = SqliteRepo(os.path.join(self.dir, 's.db'))
        self.sink = SqliteSink(self.repo)
        self.pick = make_pick()

    def test_on_open_writes_trade_and_position(self):
        self.sink.on_open('acc', self.pick, Direction.LONG, 1, 8.0, 111)
        trades = self.repo.list_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['action'], 'OPEN')
        self.assertEqual(trades[0]['direction'], 'LONG')
        pos = self.repo.list_positions()
        self.assertEqual(len(pos), 1)
        self.assertEqual(pos[0]['identifier'], self.pick.identifier)

    def test_on_position_updates_snapshot(self):
        self.sink.on_open('acc', self.pick, Direction.LONG, 1, 8.0, 111)
        view = PositionView(quantity=1, salable_qty=1, average_cost=8.0,
                            market_price=9.0, unrealized_pnl=100.0,
                            unrealized_pnl_percent=12.5)
        self.sink.on_position('acc', self.pick, Direction.LONG, 8.0, view)
        pos = self.repo.list_positions()
        self.assertEqual(len(pos), 1)
        self.assertAlmostEqual(pos[0]['unrealized_pnl_percent'], 12.5)

    def test_on_close_records_realized_pnl(self):
        # 入场 8.0、平仓 10.0 -> +25%
        self.sink.on_close('acc', self.pick, Direction.LONG, 1, 10.0,
                           CloseReason.TAKE_PROFIT, 222, entry_price=8.0)
        trades = self.repo.list_trades()
        self.assertEqual(trades[0]['action'], 'CLOSE')
        self.assertEqual(trades[0]['reason'], 'TAKE_PROFIT')
        self.assertAlmostEqual(trades[0]['pnl_percent'], 25.0)

    def test_on_position_closed_deletes(self):
        self.sink.on_open('acc', self.pick, Direction.LONG, 1, 8.0, 111)
        self.sink.on_position_closed(self.pick.identifier)
        self.assertEqual(self.repo.list_positions(), [])


class TestNullSink(unittest.TestCase):
    def test_noop(self):
        s = NullSink()
        # 不抛异常即可
        s.on_open('a', make_pick(), Direction.LONG, 1, 8.0, 1)
        s.on_position('a', make_pick(), Direction.LONG, 8.0, None)
        s.on_close('a', make_pick(), Direction.LONG, 1, 9.0, CloseReason.MANUAL, 2, 8.0)
        s.on_position_closed('x')


if __name__ == '__main__':
    unittest.main()
