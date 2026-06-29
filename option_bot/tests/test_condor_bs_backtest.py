# -*- coding: utf-8 -*-
"""铁鹰 BS 重定价回测器单测：合成 (spot, iv) 路径，验证出场/盈亏/封顶/单仓顺序。

设计 docs/design/2026-06-29-condor-bs-repriced-backtester.md。不依赖 dolt。
"""
import unittest

from option_bot.domain.models import StrategyConfig
from option_bot.backtest.condor_engine import run_condor_bs_backtest


def _series(path):
    """path: [(date, spot, iv)] → (spot_series, iv_series)。"""
    return ({d: s for d, s, v in path}, {d: v for d, s, v in path})


def _cfg(**kw):
    base = dict(mode='condor', condor_underlying='SPY', condor_target_dte=40, condor_dte_exit=21,
                condor_short_delta=0.16, condor_wing_width=5.0, condor_min_iv=0.20,
                condor_profit_target=0.5, condor_stop_mult=2.0, max_qty=1,
                condor_account_equity=0.0, condor_close_strategy='threshold')
    base.update(kw)
    return StrategyConfig(**base)


def _run(path, **kw):
    sp, iv = _series(path)
    return run_condor_bs_backtest(sp, iv, _cfg(**kw), strike_spacing=1.0)


class TestCondorBSBacktest(unittest.TestCase):
    def test_take_profit(self):
        out = _run([('2026-01-05', 100, 0.30), ('2026-01-06', 100, 0.05)])
        self.assertEqual(out['summary']['count'], 1)
        t = out['trades'][0]
        self.assertEqual(t['reason'], 'TAKE_PROFIT')
        self.assertGreater(t['pnl_usd'], 0)
        self.assertGreater(t['entry_credit'], 0)

    def test_stop_loss_capped_at_maxloss(self):
        # 标的崩到 60 + IV 飙 → 看跌价差全 ITM → 止损，且亏损封顶在最大亏损(买了翼)
        out = _run([('2026-01-05', 100, 0.30), ('2026-01-06', 60, 0.80)])
        self.assertEqual(out['summary']['count'], 1)
        t = out['trades'][0]
        self.assertEqual(t['reason'], 'STOP_LOSS')
        self.assertLess(t['pnl_usd'], 0)
        # 每股亏损不超过最大亏损（定义风险封顶），容差浮点
        self.assertGreaterEqual(t['pnl_per_share'], -t['max_loss'] - 1e-6)

    def test_time_force_close(self):
        # 把止盈/止损调到够不着 → 隔离"持有到 DTE≤21 强平"（2026-01-26 距 02-14 = 19 天）
        out = _run([('2026-01-05', 100, 0.30), ('2026-01-26', 100, 0.30)],
                   condor_profit_target=5.0, condor_stop_mult=10.0)
        self.assertEqual(out['summary']['count'], 1)
        self.assertEqual(out['trades'][0]['reason'], 'TIME_FORCE_CLOSE')
        self.assertEqual(out['trades'][0]['exit_date'], '2026-01-26')

    def test_flat_theta_profit(self):
        # 横盘几天 + 正常止盈：theta 衰减 → 卖方盈利（验证盈利来源）
        out = _run([('2026-01-05', 100, 0.30), ('2026-01-15', 100, 0.30)])
        self.assertEqual(out['summary']['count'], 1)
        self.assertGreater(out['trades'][0]['pnl_usd'], 0)

    def test_no_entry_when_iv_low(self):
        out = _run([('2026-01-05', 100, 0.10), ('2026-01-06', 100, 0.10)])
        self.assertEqual(out['summary']['count'], 0)

    def test_trailing_stop(self):
        out = _run([('2026-01-05', 100, 0.30), ('2026-01-06', 100, 0.05), ('2026-01-07', 100, 0.20)],
                   condor_close_strategy='trailing', condor_trail_activation=30.0,
                   condor_trail_giveback=15.0)
        self.assertEqual(out['summary']['count'], 1)
        self.assertEqual(out['trades'][0]['reason'], 'TRAILING_STOP')

    def test_single_position_sequential(self):
        out = _run([('2026-01-05', 100, 0.30), ('2026-01-06', 100, 0.05),
                    ('2026-01-07', 100, 0.30), ('2026-01-08', 100, 0.05)])
        self.assertEqual(out['summary']['count'], 2)
        t1, t2 = out['trades']
        self.assertEqual(t1['exit_date'], '2026-01-06')
        self.assertEqual(t2['entry_date'], '2026-01-07')   # 平了再开，不重叠

    def test_summary_fields(self):
        s = _run([('2026-01-05', 100, 0.30), ('2026-01-06', 100, 0.05)])['summary']
        for k in ('count', 'win_rate', 'total_pnl_usd', 'max_drawdown_usd',
                  'profit_factor', 'reasons'):
            self.assertIn(k, s)


if __name__ == '__main__':
    unittest.main()
