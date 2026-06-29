# -*- coding: utf-8 -*-
"""铁鹰盈亏回测器单测：合成 BS 链 + 价格路径，验证各出场路径与单仓顺序。

设计 docs/design/2026-06-29-condor-pnl-backtester.md。不依赖 dolt。
"""
import datetime as _dt
import unittest

from option_bot.domain.models import StrategyConfig
from option_bot.strategy.condor import bs_price
from option_bot.backtest.condor_engine import run_condor_backtest

EXPIRY = '2026-02-16'
STRIKES = list(range(80, 121, 5))


def _bs_rows(date, spot, sigma, r=0.04, spread=0.05):
    t = (_dt.date(*map(int, EXPIRY.split('-'))) - _dt.date(*map(int, date.split('-')))).days / 365.0
    rows = []
    for k in STRIKES:
        for pc in ('Call', 'Put'):
            px = bs_price(spot, k, t, sigma, r, pc)
            rows.append({'date': date, 'expiration': EXPIRY, 'strike': float(k), 'put_call': pc,
                         'bid': max(0.0, px - spread), 'ask': px + spread})
    return rows


def _chain(path):
    """path: [(date, spot, sigma)] → 拼接的链行。"""
    rows = []
    for date, spot, sig in path:
        rows += _bs_rows(date, spot, sig)
    return rows


def _cfg(**kw):
    base = dict(mode='condor', condor_underlying='SPY', condor_target_dte=40, condor_dte_exit=21,
                condor_short_delta=0.16, condor_wing_width=5.0, condor_min_iv=0.20,
                condor_profit_target=0.5, condor_stop_mult=2.0, max_qty=1,
                condor_account_equity=0.0, condor_close_strategy='threshold')
    base.update(kw)
    return StrategyConfig(**base)


class TestCondorBacktest(unittest.TestCase):
    def test_take_profit(self):
        # 入场后 IV 崩 → 平仓成本骤降 → 止盈
        rows = _chain([('2026-01-05', 100, 0.30), ('2026-01-06', 100, 0.05)])
        out = run_condor_backtest(rows, _cfg())
        self.assertEqual(out['summary']['count'], 1)
        t = out['trades'][0]
        self.assertEqual(t['reason'], 'TAKE_PROFIT')
        self.assertGreater(t['pnl_usd'], 0)
        self.assertEqual(t['exit_date'], '2026-01-06')

    def test_stop_loss(self):
        # 标的崩穿看跌价差 + IV 飙 → 平仓成本逼近翼宽 → 止损
        rows = _chain([('2026-01-05', 100, 0.30), ('2026-01-06', 75, 0.60)])
        out = run_condor_backtest(rows, _cfg())
        self.assertEqual(out['summary']['count'], 1)
        t = out['trades'][0]
        self.assertEqual(t['reason'], 'STOP_LOSS')
        self.assertLess(t['pnl_usd'], 0)

    def test_time_force_close(self):
        # 横盘不动 + 把止盈/止损调到够不着 → 隔离出"持有到 DTE≤21 强平"路径
        # （注：正常参数下 15 天 flat theta 已够止盈，那才是对的；此处专测时间分支）
        rows = _chain([('2026-01-05', 100, 0.30), ('2026-01-20', 100, 0.30),
                       ('2026-01-28', 100, 0.30)])   # 2026-01-28 距 02-16 = 19 天
        out = run_condor_backtest(rows, _cfg(condor_profit_target=5.0, condor_stop_mult=10.0))
        self.assertEqual(out['summary']['count'], 1)
        self.assertEqual(out['trades'][0]['reason'], 'TIME_FORCE_CLOSE')
        self.assertEqual(out['trades'][0]['exit_date'], '2026-01-28')

    def test_no_entry_when_iv_low(self):
        # 入场日 IV 低于绝对闸 → 不开仓
        rows = _chain([('2026-01-05', 100, 0.10), ('2026-01-06', 100, 0.10)])
        out = run_condor_backtest(rows, _cfg())
        self.assertEqual(out['summary']['count'], 0)

    def test_trailing_stop(self):
        # trailing：先冲高武装记峰值，再回吐超过 giveback → 移动止盈
        rows = _chain([('2026-01-05', 100, 0.30), ('2026-01-06', 100, 0.05),
                       ('2026-01-07', 100, 0.35)])
        out = run_condor_backtest(rows, _cfg(condor_close_strategy='trailing',
                                             condor_trail_activation=30.0,
                                             condor_trail_giveback=15.0))
        self.assertEqual(out['summary']['count'], 1)
        self.assertEqual(out['trades'][0]['reason'], 'TRAILING_STOP')
        self.assertEqual(out['trades'][0]['exit_date'], '2026-01-07')

    def test_single_position_sequential(self):
        # 两段机会：第一笔平掉后才开第二笔（入场2 > 出场1，不重叠）
        rows = _chain([('2026-01-05', 100, 0.30), ('2026-01-06', 100, 0.05),   # 笔1 入/止盈
                       ('2026-01-07', 100, 0.30), ('2026-01-08', 100, 0.05)])   # 笔2 入/止盈
        out = run_condor_backtest(rows, _cfg())
        self.assertEqual(out['summary']['count'], 2)
        t1, t2 = out['trades']
        self.assertEqual(t1['exit_date'], '2026-01-06')
        self.assertEqual(t2['entry_date'], '2026-01-07')   # 平了再开，不重叠
        self.assertGreaterEqual(t2['entry_date'], t1['exit_date'])

    def test_summary_fields(self):
        rows = _chain([('2026-01-05', 100, 0.30), ('2026-01-06', 100, 0.05)])
        s = run_condor_backtest(rows, _cfg())['summary']
        for k in ('count', 'win_rate', 'total_pnl_usd', 'avg_pnl_pct_credit',
                  'max_drawdown_usd', 'reasons'):
            self.assertIn(k, s)


if __name__ == '__main__':
    unittest.main()
