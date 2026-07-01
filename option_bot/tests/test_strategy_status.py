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

    def test_commission_net_pnl(self):
        # 毛吃 ~27% 权利金，扣往返佣金后净利接近 0（4腿×2×$3.22=$25.76）
        e = dict(ENTRY, entry_credit=1.05, qty=1, commission_per_leg=3.22)
        t = {'close_cost': 0.77, 'pnl_pct_of_credit': 26.7, 'dte': 30}
        v = compute_condor_view(e, t, spot=None)
        self.assertAlmostEqual(v['commission_rt'], 25.76, places=2)
        self.assertAlmostEqual(v['gross_pnl_usd'], 28.0, places=1)
        self.assertAlmostEqual(v['net_pnl_usd'], 28.0 - 25.76, places=2)
        self.assertAlmostEqual(v['commission_drag_pct'], 24.5, places=1)
        self.assertAlmostEqual(v['net_pnl_pct'], 26.7 - 24.5, places=1)

    def test_no_commission_leaves_net_none(self):
        v = compute_condor_view(ENTRY, TICK, spot=None)   # commission_per_leg 未给
        self.assertIsNone(v['commission_rt'])
        self.assertIsNone(v['net_pnl_usd'])

    def test_single_side_half_commission(self):
        call_legs = [{'side': 'SELL', 'put_call': 'CALL', 'strike': 784.0},
                     {'side': 'BUY', 'put_call': 'CALL', 'strike': 790.0}]
        e = dict(ENTRY, legs=call_legs, entry_credit=0.6, qty=1, commission_per_leg=3.22)
        v = compute_condor_view(e, {'close_cost': 0.4, 'pnl_pct_of_credit': 33.3, 'dte': 30}, None)
        self.assertAlmostEqual(v['commission_rt'], 3.22 * 2 * 2, places=2)   # 2 腿

    def test_missing_tick(self):
        v = compute_condor_view(ENTRY, None, spot=None)
        self.assertIsNone(v['pnl_pct'])
        self.assertEqual(v['dte'], 39)               # 回退 entry.dte0

    def test_side_both(self):
        v = compute_condor_view(ENTRY, TICK, spot=744.57)
        self.assertEqual(v['side'], 'both')

    def test_side_call_only(self):
        # bear call：只有 SELL CALL（+BUY CALL 翼），无 short put
        call_legs = [{'side': 'SELL', 'put_call': 'CALL', 'strike': 784.0},
                     {'side': 'BUY', 'put_call': 'CALL', 'strike': 790.0}]
        e = dict(ENTRY, legs=call_legs)
        v = compute_condor_view(e, TICK, spot=744.57)
        self.assertEqual(v['side'], 'call')
        self.assertIsNone(v['put_strike'])
        self.assertEqual(v['call_strike'], 784.0)
        self.assertIsNotNone(v['d_call'])            # 上侧距离照算
        self.assertIsNone(v['d_put'])                # 无下侧
        self.assertIsNone(v['mid_strike'])           # 单边无中点
        self.assertIsNone(v['near'])
        self.assertIsNotNone(v['gap0_pct'])          # 点差/theta 与侧无关

    def test_side_put_only_breach_warn(self):
        put_legs = [{'side': 'BUY', 'put_call': 'PUT', 'strike': 704.0},
                    {'side': 'SELL', 'put_call': 'PUT', 'strike': 709.0}]
        e = dict(ENTRY, legs=put_legs)
        v = compute_condor_view(e, TICK, spot=708.0)  # 跌破 short put 709
        self.assertEqual(v['side'], 'put')
        self.assertTrue(any('put 已被击穿' in w for w in v['warns']))
        self.assertIsNone(v['d_call'])


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

    def test_iv_fallback_to_shadow_when_engine_none(self):
        # 引擎 iv=None（重启后未采样），影子 entry 带 iv → 兜底显示
        eng = {'mode': 'condor', 'gate_mode': 'both', 'qty': 0}
        sh = self._shadow(entry=dict(ENTRY, iv=0.1534))
        c = build_strategy_status(eng, sh)['strategies']['condor']
        self.assertEqual(c['iv'], 0.1534)

    def test_engine_iv_preferred_over_shadow(self):
        eng = {'mode': 'condor', 'iv': 0.1558, 'gate_mode': 'both', 'qty': 0}
        sh = self._shadow(entry=dict(ENTRY, iv=0.1534))
        c = build_strategy_status(eng, sh)['strategies']['condor']
        self.assertEqual(c['iv'], 0.1558)            # 引擎优先

    def test_skips_dirty_tick_close_cost_nonpositive(self):
        # 末尾混入 close_cost<=0 的开盘脏点 → 应取前一个可信 tick，而非假 +100%
        dirty = {'close_cost': -0.01, 'pnl_pct_of_credit': 100.9, 'dte': 38}
        sh = self._shadow(trajectory=[TICK, dirty])
        c = build_strategy_status({'mode': 'condor'}, sh)['strategies']['condor']
        self.assertEqual(c['pnl_pct'], -7.3)         # 用了可信的 TICK，不是脏点

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
