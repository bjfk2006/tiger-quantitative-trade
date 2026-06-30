# -*- coding: utf-8 -*-
"""看板策略状态聚合单测（设计 docs/design/2026-06-30-dashboard-strategy-nav-condor-panel.md）。

纯函数，无 SDK/IO；覆盖：点差/theta 派生、spot 距离、边界、数据源选择、单 mode 语义。
"""
import unittest

from option_bot.web.strategy_status import (build_strategy_status,
                                            compute_condor_view, short_strikes)

LEGS = [{'side': 'BUY', 'put_call': 'PUT', 'strike': 704.0},
        {'side': 'SELL', 'put_call': 'PUT', 'strike': 709.0},
        {'side': 'SELL', 'put_call': 'CALL', 'strike': 784.0},
        {'side': 'BUY', 'put_call': 'CALL', 'strike': 790.0}]

ENTRY = {'symbol': 'SPY', 'expiry_date': '2026-08-07', 'legs': LEGS,
         'entry_credit': 1.09, 'mid_credit': 1.18, 'spot': 741.63, 'dte0': 39,
         'strategy_state': {'armed': False}}
TICK = {'close_cost': 1.17, 'pnl_pct_of_credit': -7.3, 'dte': 39}


class TestShortStrikes(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(short_strikes(LEGS), (709.0, 784.0))

    def test_missing(self):
        self.assertEqual(short_strikes([]), (None, None))


class TestComputeCondorView(unittest.TestCase):
    def test_spread_and_theta(self):
        v = compute_condor_view(ENTRY, TICK, spot=744.57)
        self.assertAlmostEqual(v['gap0_pct'], (1.09 - 1.18) / 1.09 * 100, places=4)  # ≈-8.26
        self.assertEqual(v['pnl_pct'], -7.3)
        self.assertAlmostEqual(v['theta_filled_pt'], -7.3 - v['gap0_pct'], places=4)  # ≈+0.96
        self.assertEqual(v['put_strike'], 709.0)
        self.assertEqual(v['call_strike'], 784.0)
        self.assertEqual(v['mid_strike'], 746.5)

    def test_spot_distances(self):
        v = compute_condor_view(ENTRY, TICK, spot=744.57)
        self.assertAlmostEqual(v['d_put'], 744.57 - 709.0, places=4)
        self.assertAlmostEqual(v['d_call'], 784.0 - 744.57, places=4)
        self.assertEqual(v['near'], 'put')          # 离 put 更近
        self.assertEqual(v['spot_side'], 'put')     # 744.57 < 746.5 中点
        self.assertEqual(v['warns'], [])            # 缓冲都 >2%

    def test_no_spot_omits_distances(self):
        v = compute_condor_view(ENTRY, TICK, spot=None)
        self.assertIsNone(v['spot'])
        self.assertIsNone(v['d_put'])
        self.assertIsNone(v['buf_put_pct'])
        self.assertIsNotNone(v['gap0_pct'])          # 点差/theta 不依赖 spot
        self.assertIsNotNone(v['theta_filled_pt'])

    def test_breach_and_buffer_warn(self):
        v = compute_condor_view(ENTRY, TICK, spot=708.0)   # 跌破 short put 709
        self.assertLess(v['d_put'], 0)
        self.assertTrue(any('击穿' in w for w in v['warns']))

    def test_armed_warn(self):
        e = dict(ENTRY, strategy_state={'armed': True})
        v = compute_condor_view(e, TICK, spot=744.57)
        self.assertTrue(any('armed' in w for w in v['warns']))

    def test_zero_credit_no_gap(self):
        e = dict(ENTRY, entry_credit=0.0)
        v = compute_condor_view(e, TICK, spot=744.57)
        self.assertIsNone(v['gap0_pct'])
        self.assertIsNone(v['theta_filled_pt'])

    def test_missing_tick(self):
        v = compute_condor_view(ENTRY, None, spot=None)
        self.assertIsNone(v['pnl_pct'])
        self.assertEqual(v['dte'], 39)               # 回退 entry.dte0


class TestBuildStrategyStatus(unittest.TestCase):
    def _shadow(self, **kw):
        base = {'status': 'TRACKING', 'outcome': None, 'entry': ENTRY,
                'trajectory': [TICK]}
        base.update(kw)
        return base

    def test_condor_active_shadow_tracking(self):
        eng = {'mode': 'condor', 'iv': 0.1558, 'ivp': 78.5, 'gate_mode': 'both',
               'symbol': 'SPY', 'qty': 0, 'bot_alive': True}
        out = build_strategy_status(eng, self._shadow())
        self.assertEqual(out['active_mode'], 'condor')
        c = out['strategies']['condor']
        self.assertTrue(c['live'])
        self.assertEqual(c['source'], 'shadow')
        self.assertTrue(c['active'])
        self.assertEqual(c['iv'], 0.1558)            # 引擎闸/IV 带出
        self.assertIsNotNone(c['gap0_pct'])          # 影子派生
        self.assertFalse(out['strategies']['straddle']['active'])

    def test_condor_waiting_no_shadow(self):
        eng = {'mode': 'condor', 'iv': 0.14, 'ivp': 30.0, 'gate_mode': 'both', 'qty': 0}
        out = build_strategy_status(eng, None)
        c = out['strategies']['condor']
        self.assertFalse(c['live'])
        self.assertEqual(c['source'], 'none')
        self.assertEqual(c['iv'], 0.14)

    def test_condor_closed_outcome(self):
        out = build_strategy_status({'mode': 'condor'},
                                    self._shadow(outcome={'reason': 'TAKE_PROFIT'}, status='TRACKING'))
        c = out['strategies']['condor']
        self.assertFalse(c['live'])
        self.assertEqual(c['outcome']['reason'], 'TAKE_PROFIT')

    def test_other_mode_active(self):
        out = build_strategy_status({'mode': 'straddle', 'bot_alive': True}, None)
        self.assertEqual(out['active_mode'], 'straddle')
        self.assertTrue(out['strategies']['straddle']['active'])
        self.assertFalse(out['strategies']['condor']['live'])
        self.assertFalse(out['strategies']['condor']['active'])

    def test_empty_engine(self):
        out = build_strategy_status({}, None)
        self.assertEqual(out['active_mode'], 'single')   # 默认
        self.assertTrue(out['strategies']['single']['active'])


if __name__ == '__main__':
    unittest.main()
