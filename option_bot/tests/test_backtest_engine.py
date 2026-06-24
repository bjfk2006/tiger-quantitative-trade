# -*- coding: utf-8 -*-
"""回测引擎纯逻辑单测（合成价格路径）。设计：2026-06-24-options-daily-backtest.md。"""
import unittest

from option_bot.domain.models import CloseReason, StrategyConfig
from option_bot.backtest.engine import run_backtest, run_batch, summarize


def series(*bids, entry_ask=10.0):
    """构造日线：第0天为入场日(ask=entry_ask)，其后每天给 bid（ask 跟随，便于多入场）。"""
    rows = [{'date': '2024-01-01', 'bid': entry_ask, 'ask': entry_ask}]
    for i, b in enumerate(bids):
        d = f'2024-01-{i+2:02d}'
        rows.append({'date': d, 'bid': float(b), 'ask': float(b) + 0.2})
    return rows


class TestRunBacktest(unittest.TestCase):
    def test_trailing_stop(self):
        cfg = StrategyConfig(strategy_name='trailing', trail_activation=20, trail_giveback=10)
        # entry ask=10; bids: 12(+20%武装) 13(+30%峰) 11.9(+19%<=30-10触发)
        r = run_backtest(series(12, 13, 11.9), cfg, 'trailing')
        self.assertEqual(r.reason, CloseReason.TRAILING_STOP.value)
        self.assertEqual(r.exit_date, '2024-01-04')
        self.assertEqual(r.pnl_percent, 19.0)
        self.assertEqual(r.peak_pnl_percent, 30.0)
        self.assertEqual(r.entry_price, 10.0)

    def test_stop_loss(self):
        cfg = StrategyConfig(strategy_name='trailing', sl_percent=50)
        r = run_backtest(series(4), cfg, 'trailing')   # -60% <= -50
        self.assertEqual(r.reason, CloseReason.STOP_LOSS.value)
        self.assertEqual(r.pnl_percent, -60.0)

    def test_threshold_take_profit(self):
        cfg = StrategyConfig(strategy_name='threshold', tp_percent=30)
        r = run_backtest(series(11, 13), cfg, 'threshold')  # +10% 然后 +30% 触发
        self.assertEqual(r.reason, CloseReason.TAKE_PROFIT.value)
        self.assertEqual(r.exit_date, '2024-01-03')
        self.assertEqual(r.pnl_percent, 30.0)

    def test_force_close_at_end(self):
        cfg = StrategyConfig(strategy_name='trailing', trail_activation=20, trail_giveback=10)
        r = run_backtest(series(10.5, 10.8), cfg, 'trailing')  # 从不武装 → 末日强平
        self.assertEqual(r.reason, CloseReason.TIME_FORCE_CLOSE.value)
        self.assertEqual(r.exit_date, '2024-01-03')
        self.assertAlmostEqual(r.pnl_percent, 8.0, places=1)

    def test_relative_trailing(self):
        cfg = StrategyConfig(strategy_name='trailing', trail_activation=20, trail_giveback=10,
                             trail_relative_ratio=20, trail_relative_threshold=50)
        # 峰值+200% → giveback=max(10,40)=40 → 跌回+160%平；+165%不平
        r = run_backtest(series(30, 26.5, 26), cfg, 'trailing')
        self.assertEqual(r.reason, CloseReason.TRAILING_STOP.value)
        self.assertEqual(r.exit_date, '2024-01-04')
        self.assertEqual(r.pnl_percent, 160.0)

    def test_no_entry_when_empty(self):
        cfg = StrategyConfig(strategy_name='trailing')
        self.assertIsNone(run_backtest([], cfg, 'trailing'))

    def test_entry_date_selection(self):
        cfg = StrategyConfig(strategy_name='threshold', tp_percent=30)
        rows = series(11, 13, 20)
        r = run_backtest(rows, cfg, 'threshold', entry_date='2024-01-03')
        # 入场改为 01-03（bid=13,ask=13.2）；其后 01-04 bid=20 → (20-13.2)/13.2≈51.5%>=30
        self.assertEqual(r.entry_date, '2024-01-03')
        self.assertEqual(r.reason, CloseReason.TAKE_PROFIT.value)


class TestBatchAndSummary(unittest.TestCase):
    def test_batch_runs_each_day(self):
        cfg = StrategyConfig(strategy_name='trailing', trail_activation=20, trail_giveback=10)
        out = run_batch(series(12, 13, 11.9), cfg, 'trailing')
        # 入场日有 4 天，但最后一天无后续→无结果；前几天各产出一笔
        self.assertGreaterEqual(out['summary']['count'], 1)
        self.assertIn('win_rate', out['summary'])

    def test_summarize_empty(self):
        self.assertEqual(summarize([]), {'count': 0})


if __name__ == '__main__':
    unittest.main()
