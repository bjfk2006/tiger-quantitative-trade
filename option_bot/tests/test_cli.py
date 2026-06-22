# -*- coding: utf-8 -*-
"""CLI 集成测试（CliRunner）。

通过 patch _build_clients 隔离 SDK / 网络 / 凭证，只验证 CLI 接线与分支。
对应设计 §4 Presentation 层 / kill switch（--no-enable-open）。
"""
import unittest
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


def _fake_clients():
    """返回 (config, quote_client, trade_client) 三元组的 mock。"""
    config = MagicMock()
    config.account = 'paper-123'
    return config, MagicMock(), MagicMock()


class TestRunEnableOpenSwitch(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    @patch('option_bot.cli.main._build_clients')
    def test_no_enable_open_without_position_exits_cleanly(self, mock_build):
        """--no-enable-open 且无遗留持仓时：友好退出，绝不下单。"""
        from option_bot.cli.main import cli
        _config, qc, tc = _fake_clients()
        mock_build.return_value = (_config, qc, tc)

        with self.runner.isolated_filesystem():
            result = self.runner.invoke(cli, [
                '--account', 'paper-123',
                'run', 'AAPL',
                '--direction', 'LONG',
                '--expiry', '2025-08-15',
                '--strike', '200',
                '--no-enable-open',
                '--state-file', 'no_such_state.json',
            ])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn('开仓开关已关闭', result.output)
        # 关键断言：未尝试任何下单
        tc.place_order.assert_not_called()
        # 也未去查期权链（不进入开仓流程）
        qc.get_option_chain.assert_not_called()

    @patch('option_bot.cli.main._build_clients')
    def test_no_enable_open_resumes_existing_position(self, mock_build):
        """--no-enable-open 但有遗留持仓时：应进入监控（resume 生效），而非退出。"""
        from option_bot.cli.main import cli
        _config, qc, tc = _fake_clients()
        mock_build.return_value = (_config, qc, tc)

        # 让 PositionStateMachine.resume() 返回 True、MonitorLoop 立刻停机，
        # 避免真实盯盘循环；只验证「没有走到开仓退出分支」。
        with patch('option_bot.cli.main.PositionStateMachine') as MockSM, \
                patch('option_bot.cli.main.MonitorLoop') as MockLoop:
            sm = MockSM.return_value
            sm.resume.return_value = True
            sm.state.value = 'MONITORING'
            MockLoop.return_value.run.return_value = None

            with self.runner.isolated_filesystem():
                result = self.runner.invoke(cli, [
                    '--account', 'paper-123',
                    'run', 'AAPL',
                    '--direction', 'LONG',
                    '--expiry', '2025-08-15',
                    '--strike', '200',
                    '--no-enable-open',
                ])

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertNotIn('开仓开关已关闭', result.output)  # 未走开仓退出分支
        sm.resume.assert_called_once()
        sm.open.assert_not_called()                       # 不开新仓
        MockLoop.return_value.run.assert_called_once()    # 进入盯盘


if __name__ == '__main__':
    unittest.main()
