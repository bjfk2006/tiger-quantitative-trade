# -*- coding: utf-8 -*-
"""SqliteRepo 单测：临时 db，建表/CRUD。对应设计增量 §5。"""
import os
import tempfile
import unittest

from option_bot.persistence.db import SqliteRepo


class TestSqliteRepo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.repo = SqliteRepo(os.path.join(self.dir, 't.db'))

    def test_insert_and_list_trades(self):
        self.repo.insert_trade('acc', 'AAPL  250815C00200000', 'AAPL', 'LONG',
                               'OPEN', 1, price=8.0, reason='OPEN', order_id=1, ts=1000)
        self.repo.insert_trade('acc', 'AAPL  250815C00200000', 'AAPL', 'LONG',
                               'CLOSE', 1, price=9.0, reason='TAKE_PROFIT',
                               order_id=2, pnl_percent=12.5, ts=2000)
        rows = self.repo.list_trades()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['action'], 'CLOSE')  # ts desc
        self.assertEqual(rows[0]['pnl_percent'], 12.5)

    def test_upsert_and_delete_position(self):
        self.repo.upsert_position('id1', 'acc', 'AAPL', 'LONG', qty=1,
                                  entry_price=8.0, market_price=8.5,
                                  unrealized_pnl_percent=6.25, state='MONITORING')
        rows = self.repo.list_positions()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['unrealized_pnl_percent'], 6.25)
        # upsert 同 identifier 应更新而非新增
        self.repo.upsert_position('id1', 'acc', 'AAPL', 'LONG', qty=1,
                                  entry_price=8.0, market_price=9.0,
                                  unrealized_pnl_percent=12.5, state='MONITORING')
        rows = self.repo.list_positions()
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]['market_price'], 9.0)
        # 删除
        self.repo.delete_position('id1')
        self.assertEqual(self.repo.list_positions(), [])

    def test_ops_audit(self):
        self.repo.insert_ops_audit('close', source_ip='1.2.3.4', key_id='abcd***',
                                   result='queued', ts=5000)
        rows = self.repo.list_ops_audit()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['action'], 'close')
        self.assertEqual(rows[0]['source_ip'], '1.2.3.4')


if __name__ == '__main__':
    unittest.main()
