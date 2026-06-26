# -*- coding: utf-8 -*-
"""已平仓盈亏统计单测（纯逻辑）。对应设计 2026-06-23-trade-history-stats.md。"""
import unittest

from option_bot.persistence.stats import (downsample, equity_curve,
                                          filter_by_close_ts, pair_round_trips,
                                          realized_pnl_amount, summarize)


class TestDownsample(unittest.TestCase):
    def test_no_downsample_when_small(self):
        rows = [{'ts': i} for i in range(10)]
        self.assertEqual(downsample(rows, 1000), rows)

    def test_downsamples_and_keeps_last(self):
        rows = [{'ts': i} for i in range(5000)]
        out = downsample(rows, 1000)
        self.assertLessEqual(len(out), 1001)
        self.assertEqual(out[-1]['ts'], 4999)  # 始终保留最后一点


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


class TestRealizedPnlAmount(unittest.TestCase):
    """当日亏损上限核算用：按平仓时间窗 + 账户配对汇总已实现盈亏$。"""

    def test_window_filters_to_day(self):
        rows = [tr('OPEN', 5, 1000), tr('CLOSE', 6, 2000),     # +100, close@2000
                tr('OPEN', 8, 3000), tr('CLOSE', 7, 4000)]     # -100, close@4000
        # 窗口 [3500, 5000) 只含第二笔 → -100
        self.assertAlmostEqual(realized_pnl_amount(rows, 3500, 5000), -100.0)
        # 全窗 → 0
        self.assertAlmostEqual(realized_pnl_amount(rows, 0, 5000), 0.0)

    def test_open_before_window_still_paired(self):
        # OPEN 早于窗口、CLOSE 落在窗口内：配对需完整 OPEN，故不按 ts 截断输入
        rows = [tr('OPEN', 10, 100), tr('CLOSE', 7, 9000)]     # -300, close@9000
        self.assertAlmostEqual(realized_pnl_amount(rows, 8000, 10000), -300.0)

    def test_account_filter(self):
        rows = [tr('OPEN', 5, 1000, acc='a'), tr('CLOSE', 4, 2000, acc='a'),   # a: -100
                tr('OPEN', 5, 1000, acc='b', ident='Y'),
                tr('CLOSE', 9, 2000, acc='b', ident='Y')]                       # b: +400
        self.assertAlmostEqual(realized_pnl_amount(rows, 0, 5000, account='a'), -100.0)
        self.assertAlmostEqual(realized_pnl_amount(rows, 0, 5000, account='b'), 400.0)

    def test_no_trades_zero(self):
        self.assertEqual(realized_pnl_amount([], 0, 5000), 0.0)


if __name__ == '__main__':
    unittest.main()
