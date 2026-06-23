# -*- coding: utf-8 -*-
"""可插拔平仓策略（设计：docs/design/2026-06-23-pluggable-close-strategy.md）。

接口 + 安全基类 + 具体策略 + 注册表。纯逻辑、无 SDK，易单测。
安全约定：时间强平 + 硬止损放在 BaseCloseStrategy，任何策略都自带、不可绕过；
子类只实现 profit_decide（怎么止盈）。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from option_bot.domain.models import CloseReason


@dataclass
class StrategyContext:
    """每 tick 喂给策略的上下文。"""
    pnl_percent: Optional[float]      # 未实现盈亏%（百分数），None 表示暂不可得
    minutes_to_close: Optional[float]  # 距收盘分钟数，None 表示非交易日/已收盘
    market_price: Optional[float] = None
    entry_price: Optional[float] = None
    now_ts: Optional[int] = None


class CloseStrategy(ABC):
    name = 'base'

    @abstractmethod
    def decide(self, ctx: StrategyContext) -> Optional[CloseReason]:
        ...

    def state(self) -> dict:
        """导出运行态用于持久化（无状态策略返回空）。"""
        return {}

    def load_state(self, d: dict) -> None:
        """崩溃恢复时还原运行态。"""
        return None


class BaseCloseStrategy(CloseStrategy):
    """安全底座：时间强平 + 硬止损永远优先生效。"""

    def __init__(self, close_buffer_minutes, sl_percent):
        self.close_buffer = close_buffer_minutes
        self.sl_percent = sl_percent

    def decide(self, ctx):
        # ① 时间强平：无条件，最高优先级（即使 pnl 不可得也要平）
        if ctx.minutes_to_close is not None and ctx.minutes_to_close <= self.close_buffer:
            return CloseReason.TIME_FORCE_CLOSE
        if ctx.pnl_percent is None:
            return None
        # ② 硬止损兜底
        if ctx.pnl_percent <= -self.sl_percent:
            return CloseReason.STOP_LOSS
        # ③ 子类的盈利了结
        return self.profit_decide(ctx)

    def profit_decide(self, ctx) -> Optional[CloseReason]:
        return None


class ThresholdStrategy(BaseCloseStrategy):
    """固定止盈（等价现状）：pnl ≥ tp% 即止盈。"""
    name = 'threshold'

    def __init__(self, close_buffer_minutes, sl_percent, tp_percent):
        super().__init__(close_buffer_minutes, sl_percent)
        self.tp_percent = tp_percent

    def profit_decide(self, ctx):
        if ctx.pnl_percent >= self.tp_percent:
            return CloseReason.TAKE_PROFIT
        return None


class TrailingStrategy(BaseCloseStrategy):
    """移动止盈/回撤保护：涨破 activation 后武装并记峰值；从峰值回撤 giveback 即平仓锁盈。

    例：activation=20, giveback=10 → 涨破 +20% 武装；峰值 +20% 回落到 +10% 平；
    若涨到 +35%，止盈线跟随峰值上移到 +25%。
    """
    name = 'trailing'

    def __init__(self, close_buffer_minutes, sl_percent, trail_activation, trail_giveback):
        super().__init__(close_buffer_minutes, sl_percent)
        self.trail_activation = trail_activation
        self.trail_giveback = trail_giveback
        self.armed = False
        self.peak = None

    def profit_decide(self, ctx):
        pnl = ctx.pnl_percent
        if not self.armed:
            if pnl >= self.trail_activation:
                self.armed = True
                self.peak = pnl
            return None
        # 已武装：峰值上移
        if self.peak is None or pnl > self.peak:
            self.peak = pnl
        # 从峰值回撤达阈值 → 锁盈平仓
        if pnl <= self.peak - self.trail_giveback:
            return CloseReason.TRAILING_STOP
        return None

    def state(self):
        return {'armed': self.armed, 'peak': self.peak}

    def load_state(self, d):
        if d:
            self.armed = bool(d.get('armed', False))
            self.peak = d.get('peak')


STRATEGY_REGISTRY = {
    'threshold': ThresholdStrategy,
    'trailing': TrailingStrategy,
}


def build_strategy(name, cfg) -> CloseStrategy:
    """按名称 + StrategyConfig 构建策略。未知名抛错。"""
    name = (name or 'threshold').lower()
    if name == 'threshold':
        return ThresholdStrategy(cfg.close_buffer_minutes, cfg.sl_percent, cfg.tp_percent)
    if name == 'trailing':
        return TrailingStrategy(cfg.close_buffer_minutes, cfg.sl_percent,
                                cfg.trail_activation, cfg.trail_giveback)
    raise ValueError(f'未知平仓策略: {name}（可选: {list(STRATEGY_REGISTRY)}）')
