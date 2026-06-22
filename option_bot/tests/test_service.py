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


if __name__ == '__main__':
    unittest.main()
