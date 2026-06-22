# -*- coding: utf-8 -*-
"""领域错误：Adapter 层把 SDK 异常翻译为这些，供状态机决策（设计文档 §6）。"""


class OptionBotError(Exception):
    pass


class DataUnavailable(OptionBotError):
    """行情/持仓/日历等数据拉取失败（可重试/降级）。"""


class OpenRejected(OptionBotError):
    """开仓被拒（权限/资金/预检不通过）。"""


class CloseRejected(OptionBotError):
    """平仓被拒（需重试，临近收盘尤其重要）。"""
