# -*- coding: utf-8 -*-
"""IV 历史存储 + IV 分位/Rank 纯函数单测。设计 2026-06-29-condor-iv-rank-entry-gate。"""
import json
import os
import tempfile
import unittest

from option_bot.strategy.iv_history import IVHistoryStore, iv_percentile, iv_rank


class TestIVMetrics(unittest.TestCase):
    def test_percentile(self):
        h = [0.10, 0.12, 0.14, 0.16, 0.18]
        self.assertAlmostEqual(iv_percentile(h, 0.15), 60.0)   # 10/12/14 < 15 → 3/5
        self.assertAlmostEqual(iv_percentile(h, 0.10), 0.0)    # 无严格更小
        self.assertAlmostEqual(iv_percentile(h, 0.20), 100.0)  # 全部更小
        self.assertAlmostEqual(iv_percentile([0.1, 0.1, 0.1], 0.1), 0.0)

    def test_percentile_edge(self):
        self.assertIsNone(iv_percentile([], 0.15))
        self.assertIsNone(iv_percentile([0.1], None))

    def test_rank(self):
        self.assertAlmostEqual(iv_rank([0.10, 0.20], 0.15), 50.0)
        self.assertAlmostEqual(iv_rank([0.10, 0.20], 0.20), 100.0)
        self.assertAlmostEqual(iv_rank([0.10, 0.20], 0.10), 0.0)

    def test_rank_edge(self):
        self.assertIsNone(iv_rank([], 0.15))
        self.assertIsNone(iv_rank([0.1, 0.1, 0.1], 0.1))       # max==min
        self.assertIsNone(iv_rank([0.1], None))


class TestIVHistoryStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mktemp(suffix='_ivh.json')

    def tearDown(self):
        if os.path.exists(self._tmp):
            os.remove(self._tmp)

    def test_append_and_values(self):
        s = IVHistoryStore(self._tmp, lookback_days=252)
        self.assertTrue(s.append_daily('2026-01-01', 0.14))
        self.assertTrue(s.append_daily('2026-01-02', 0.16))
        self.assertEqual(s.values(), [0.14, 0.16])
        self.assertEqual(len(s), 2)

    def test_same_day_live_is_noop(self):
        s = IVHistoryStore(self._tmp)
        self.assertTrue(s.append_daily('2026-01-01', 0.14))
        self.assertFalse(s.append_daily('2026-01-01', 0.99))   # 同日真样本不重复
        self.assertEqual(s.values(), [0.14])

    def test_live_overwrites_seed(self):
        s = IVHistoryStore(self._tmp)
        self.assertTrue(s.append_daily('2026-01-01', 0.13, src='seed'))
        self.assertTrue(s.append_daily('2026-01-01', 0.15, src='live'))  # 真样本覆盖种子
        self.assertEqual(s.values(), [0.15])

    def test_prune_to_lookback(self):
        s = IVHistoryStore(self._tmp, lookback_days=3)
        for i, v in enumerate([0.11, 0.12, 0.13, 0.14, 0.15], start=1):
            s.append_daily(f'2026-01-0{i}', v)
        # 只保留最近 3 天（按日期排序）
        self.assertEqual(s.values(), [0.13, 0.14, 0.15])

    def test_rejects_bad_iv(self):
        s = IVHistoryStore(self._tmp)
        self.assertFalse(s.append_daily('2026-01-01', None))
        self.assertFalse(s.append_daily('2026-01-01', 0.0))
        self.assertFalse(s.append_daily('2026-01-01', -0.1))
        self.assertEqual(s.values(), [])

    def test_corrupt_file_tolerated(self):
        with open(self._tmp, 'w') as f:
            f.write('{not json')
        s = IVHistoryStore(self._tmp)
        self.assertEqual(s.values(), [])              # 坏文件→空，不抛
        self.assertTrue(s.append_daily('2026-01-01', 0.14))   # 仍可写
        self.assertEqual(s.values(), [0.14])

    def test_persisted_json_valid(self):
        s = IVHistoryStore(self._tmp)
        s.append_daily('2026-01-01', 0.14)
        with open(self._tmp) as f:
            data = json.load(f)
        self.assertEqual(data, [{'date': '2026-01-01', 'iv': 0.14, 'src': 'live'}])

    def test_seed_from_vix(self):
        vix = tempfile.mktemp(suffix='_vix.csv')
        with open(vix, 'w') as f:
            f.write('DATE,OPEN,HIGH,LOW,CLOSE\n')
            f.write('06/01/2026,18,18,18,18.0\n')   # iv=(18-4)/100=0.14
            f.write('06/02/2026,24,24,24,24.0\n')   # iv=0.20
            f.write('07/01/2026,30,30,30,30.0\n')   # > today → 不种
        try:
            s = IVHistoryStore(self._tmp, lookback_days=252)
            n = s.seed_from_vix(vix, gap=4.0, today_str='2026-06-15')
            self.assertEqual(n, 2)
            self.assertEqual(sorted(s.values()), [0.14, 0.20])
            # 已有真样本时不再种
            s.append_daily('2026-06-10', 0.15, src='live')
            self.assertEqual(s.seed_from_vix(vix, 4.0, '2026-06-15'), 0)
        finally:
            if os.path.exists(vix):
                os.remove(vix)

    def test_seed_missing_csv_returns_zero(self):
        s = IVHistoryStore(self._tmp)
        self.assertEqual(s.seed_from_vix('/tmp/nope_vix.csv', 4.0, '2026-06-15'), 0)


if __name__ == '__main__':
    unittest.main()
