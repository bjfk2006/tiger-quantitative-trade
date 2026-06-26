# -*- coding: utf-8 -*-
"""Supervisor 命令排空 + CommandQueue 单测。对应设计增量 §3/§10。"""
import unittest
from unittest.mock import MagicMock

from option_bot.domain.models import BotState, CloseReason, StrategyConfig
from option_bot.service import (CMD_CLOSE, CMD_DISABLE_OPEN, CMD_ENABLE_OPEN,
                                CMD_STOP, CommandQueue, Supervisor)


class TestCommandQueue(unittest.TestCase):
    def test_put_drain(self):
        q = CommandQueue()
        q.put(CMD_CLOSE)
        q.put(CMD_STOP)
        self.assertEqual(q.drain(), [CMD_CLOSE, CMD_STOP])
        self.assertEqual(q.drain(), [])

    def test_reject_unknown(self):
        with self.assertRaises(ValueError):
            CommandQueue().put('hack')


class TestSupervisorCommands(unittest.TestCase):
    def setUp(self):
        self.sm = MagicMock()
        self.sm.state = BotState.MONITORING
        self.loop = MagicMock()
        self.cfg = StrategyConfig()
        self.q = CommandQueue()
        self.sup = Supervisor(self.sm, self.loop, self.cfg, self.q,
                              sleep=lambda *_: None)

    def test_close_calls_manual_close(self):
        self.q.put(CMD_CLOSE)
        self.sup._drain_commands()
        self.sm.close.assert_called_once_with(CloseReason.MANUAL)

    def test_close_ignored_when_no_position(self):
        self.sm.state = BotState.CLOSED
        self.q.put(CMD_CLOSE)
        self.sup._drain_commands()
        self.sm.close.assert_not_called()

    def test_disable_then_enable_open(self):
        self.q.put(CMD_DISABLE_OPEN)
        self.sup._drain_commands()
        self.assertFalse(self.cfg.enable_open)
        self.q.put(CMD_ENABLE_OPEN)
        self.sup._drain_commands()
        self.assertTrue(self.cfg.enable_open)

    def test_stop_sets_stopped(self):
        self.q.put(CMD_STOP)
        self.sup._drain_commands()
        self.assertTrue(self.sup._stopped)

    def test_status_shape(self):
        self.sm.pick = None
        self.sm.entry_price = None
        st = self.sup.status()
        self.assertIn('state', st)
        self.assertIn('enable_open', st)
        self.assertIn('bot_alive', st)


class TestDailyLossLimit(unittest.TestCase):
    """当日亏损上限：只挡开仓，不平已有仓；统计故障放行。"""

    def _open_trade(self, price, ts, ident='X'):
        return {'account': 'acc', 'identifier': ident, 'symbol': 'SPCX',
                'direction': 'LONG', 'action': 'OPEN', 'qty': 1, 'price': price, 'ts': ts}

    def _close_trade(self, price, ts, ident='X'):
        return {'account': 'acc', 'identifier': ident, 'symbol': 'SPCX',
                'direction': 'LONG', 'action': 'CLOSE', 'qty': 1, 'price': price, 'ts': ts}

    def _sup(self, repo, limit=300.0):
        cfg = StrategyConfig(daily_loss_limit=limit)
        sup = Supervisor(MagicMock(), MagicMock(), cfg, CommandQueue(),
                         sleep=lambda *_: None, repo=repo, account='acc')
        # 固定「今天」窗口，避免依赖真实当前日期
        sup._today_window_ms = lambda: (1000, 9_000_000)
        return sup

    def test_disabled_when_limit_zero(self):
        sup = self._sup(MagicMock(), limit=0.0)
        self.assertFalse(sup._daily_loss_blocked())

    def test_blocks_when_loss_exceeds_limit(self):
        repo = MagicMock()
        # 当日两笔已实现：-$400 + -$100 = -$500 ≤ -300 → 拦截
        repo.list_trades_in_range.return_value = [
            self._open_trade(10, 2000), self._close_trade(6, 3000),          # -400
            self._open_trade(5, 4000, 'Y'), self._close_trade(4, 5000, 'Y'),  # -100
        ]
        self.assertTrue(self._sup(repo).  _daily_loss_blocked())

    def test_allows_when_within_limit(self):
        repo = MagicMock()
        repo.list_trades_in_range.return_value = [
            self._open_trade(10, 2000), self._close_trade(9, 3000),  # -100 > -300
        ]
        self.assertFalse(self._sup(repo)._daily_loss_blocked())

    def test_stats_failure_allows_open(self):
        repo = MagicMock()
        repo.list_trades_in_range.side_effect = RuntimeError('db down')
        self.assertFalse(self._sup(repo)._daily_loss_blocked())

    def test_open_on_start_skipped_when_blocked(self):
        repo = MagicMock()
        repo.list_trades_in_range.return_value = [
            self._open_trade(10, 2000), self._close_trade(2, 3000),  # -800 ≤ -300
        ]
        sup = self._sup(repo)
        sup._md = MagicMock()
        sup._open_spec = {'symbol': 'SPCX', 'direction': 'LONG',
                          'expiry': '20260702', 'strike': 155.0, 'qty': 1}
        sup._do_open_on_start()
        sup._sm.open.assert_not_called()


if __name__ == '__main__':
    unittest.main()
