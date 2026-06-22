# -*- coding: utf-8 -*-
"""option_bot — 美股期权自动交易程序（基于 tigeropen SDK）.

设计文档: docs/design/2026-06-21-us-option-trading-bot-solution.md
能力: 查看期权链 / 做多买Call·做空买Put / 市价开仓 / 轮询盈亏% /
      止盈·止损·收盘前N分钟强平 自动平仓。
"""

__VERSION__ = '0.1.0'
