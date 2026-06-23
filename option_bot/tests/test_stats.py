# -*- coding: utf-8 -*-
"""已平仓盈亏统计单测（纯逻辑）。对应设计 2026-06-23-trade-history-stats.md。"""
import unittest

from option_bot.persistence.stats import (equity_curve, filter_by_close_ts,
                                          pair_round_trips, summarize)


def tr(action, price, ts, ident='X', acc='a', qty=1, pnl=None, sym='QQQ', direction='LONG'):
    return {'account': acc, 'identifier': ident, 'symbol': sym, 'direction': direction,
            'action': action, 'qty': qty, 'price': price, 'ts': ts, 'pnl_percent': pnl,
            'reason': ('OPEN' if action == 'OPEN' else 'TAKE_PROFIT')}


class TestPairing(unittest.TestCase):
    def test_basic_pair(self):
        rts = pair_round_trips([tr('OPEN', 7.5, 1000), tr('CLOSE', 11.3, 2000, pnl=50.7)])
        self.assertEqual(len(rts), 1)
        r = rts[0]
        self.assertEqual(r['open_price'], 7.5)
        self.assertEqual(r['close_price'], 11.3)
        # (11.3-7.5)*1*100 = 380
        self.assertAlmostEqual(r['pnl_amount'], 380.0)
        self.assertEqual(r['pnl_percent'], 50.7)

    def test_fifo_multiple(self):
        rows = [tr('OPEN', 5, 1), tr('CLOSE', 6, 2), tr('OPEN', 8, 3), tr('CLOSE', 7, 4)]
        rts = pair_round_trips(rows)
        self.assertEqual(len(rts), 2)
        self.assertAlmostEqual(rts[0]['pnl_amount'], 100.0)   # (6-5)*100
        self.assertAlmostEqual(rts[1]['pnl_amount'], -100.0)  # (7-8)*100

    def test_unpaired_open_excluded(self):
        # 仍持仓(只有 OPEN) -> 不产出 round-trip
        self.assertEqual(pair_round_trips([tr('OPEN', 5, 1)]), [])

    def test_close_without_open_skipped(self):
        self.assertEqual(pair_round_trips([tr('CLOSE', 5, 1)]), [])

    def test_separate_accounts_not_crossed(self):
        rows = [tr('OPEN', 5, 1, acc='a'), tr('CLOSE', 6, 2, acc='b')]
        # b 的 CLOSE 无 b 的 OPEN -> 跳过；a 的 OPEN 未平 -> 不产出
        self.assertEqual(pair_round_trips(rows), [])


class TestFilterAndSummary(unittest.TestCase):
    def setUp(self):
        self.rts = pair_round_trips([
            tr('OPEN', 5, 1000), tr('CLOSE', 6, 2000, pnl=20),   # +100
            tr('OPEN', 8, 3000), tr('CLOSE', 7, 4000, pnl=-12.5),  # -100
            tr('OPEN', 10, 5000), tr('CLOSE', 13, 6000, pnl=30),  # +300
        ])

    def test_filter_by_close_ts(self):
        # 只要 close_ts ∈ [3500, 6000)
        out = filter_by_close_ts(self.rts, 3500, 6000)
        self.assertEqual(len(out), 1)  # 仅 close_ts=4000 那笔（6000 为开区间上界排除）
        self.assertAlmostEqual(out[0]['pnl_amount'], -100.0)

    def test_summary(self):
        s = summarize(self.rts)
        self.assertEqual(s['count'], 3)
        self.assertEqual(s['wins'], 2)
        self.assertEqual(s['losses'], 1)
        self.assertAlmostEqual(s['win_rate'], 0.6667, places=3)
        self.assertAlmostEqual(s['total_pnl_amount'], 300.0)
        self.assertAlmostEqual(s['max_win'], 300.0)
        self.assertAlmostEqual(s['max_loss'], -100.0)

    def test_summary_empty(self):
        s = summarize([])
        self.assertEqual(s['count'], 0)
        self.assertEqual(s['total_pnl_amount'], 0.0)
        self.assertEqual(s['win_rate'], 0.0)

    def test_equity_curve(self):
        c = equity_curve(self.rts)
        self.assertEqual([p['cum_pnl'] for p in c], [100.0, 0.0, 300.0])


if __name__ == '__main__':
    unittest.main()
