# -*- coding: utf-8 -*-
"""期权日线回测（设计：docs/design/2026-06-24-options-daily-backtest.md）。

复用实盘同一套可插拔平仓策略（strategy.close_strategies），仅喂逐日 pnl%。
日线粒度，无法复现盘中 trailing/收盘前强平，结论偏保守。纯离线工具，零侵入实盘。
"""
