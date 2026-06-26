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


def trailing_giveback(peak, abs_giveback, relative_ratio=0.0, relative_threshold=50.0):
    """移动止盈的回撤阈值(点)。

    默认 = 绝对 abs_giveback；当 relative_ratio>0 且 peak≥relative_threshold 时，
    取 max(abs_giveback, peak × relative_ratio%)——盈利越大容忍回撤越大，但不小于绝对值。
    relative_ratio=0 → 纯绝对（向后兼容）。平仓条件：pnl ≤ peak − 本函数返回值。
    """
    gb = abs_giveback
    if relative_ratio and peak is not None and peak >= relative_threshold:
        gb = max(abs_giveback, peak * relative_ratio / 100.0)
    return gb


@dataclass
class StrategyContext:
    """每 tick 喂给策略的上下文。"""
    pnl_percent: Optional[float]      # 未实现盈亏%（百分数），None 表示暂不可得
    minutes_to_close: Optional[float]  # 距收盘分钟数，None 表示非交易日/已收盘
    market_price: Optional[float] = None
    entry_price: Optional[float] = None
    now_ts: Optional[int] = None
    opened_at: Optional[int] = None    # 开仓时间(ms)，由状态机注入；time_in_trade 用
    dte: Optional[int] = None          # 距到期天数，由状态机注入；收盘前强平的 DTE 判据


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

    def __init__(self, close_buffer_minutes, sl_percent, eod_close_max_dte=1):
        self.close_buffer = close_buffer_minutes
        self.sl_percent = sl_percent
        # 收盘前强平只作用于 DTE≤该值的期权；更长期权持有过夜（由 build_strategy 按配置覆盖）
        self.eod_close_max_dte = eod_close_max_dte

    def decide(self, ctx):
        # ① 收盘前强平：仅当 DTE 已临近(≤ eod_close_max_dte)才平；
        #    DTE 未知(None)同样强平（安全默认，不把未知期限留过夜）；更长期权落到下面继续判止损/止盈。
        if ctx.minutes_to_close is not None and ctx.minutes_to_close <= self.close_buffer:
            if ctx.dte is None or ctx.dte <= self.eod_close_max_dte:
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

    def __init__(self, close_buffer_minutes, sl_percent, trail_activation, trail_giveback,
                 relative_ratio=0.0, relative_threshold=50.0):
        super().__init__(close_buffer_minutes, sl_percent)
        self.trail_activation = trail_activation
        self.trail_giveback = trail_giveback
        self.relative_ratio = relative_ratio
        self.relative_threshold = relative_threshold
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
        # 从峰值回撤达阈值(绝对/相对混合) → 锁盈平仓
        gb = trailing_giveback(self.peak, self.trail_giveback,
                               self.relative_ratio, self.relative_threshold)
        if pnl <= self.peak - gb:
            return CloseReason.TRAILING_STOP
        return None

    def state(self):
        return {'armed': self.armed, 'peak': self.peak}

    def load_state(self, d):
        if d:
            self.armed = bool(d.get('armed', False))
            self.peak = d.get('peak')


class BreakevenStrategy(BaseCloseStrategy):
    """保本止损：盈利冲过 activation% 后武装；之后回吐到 lock%（0=成本价）即平，锁住已有利润。"""
    name = 'breakeven'

    def __init__(self, close_buffer_minutes, sl_percent, activation, lock):
        super().__init__(close_buffer_minutes, sl_percent)
        self.activation = activation
        self.lock = lock
        self.armed = False

    def profit_decide(self, ctx):
        pnl = ctx.pnl_percent
        if not self.armed and pnl >= self.activation:
            self.armed = True
        if self.armed and pnl <= self.lock:
            return CloseReason.BREAKEVEN
        return None

    def state(self):
        return {'armed': self.armed}

    def load_state(self, d):
        if d:
            self.armed = bool(d.get('armed', False))


class TimeInTradeStrategy(BaseCloseStrategy):
    """持仓时长上限：持仓超过 max_hold_minutes 即平（theta 兜底）。无状态(用 ctx.opened_at)。"""
    name = 'time_in_trade'

    def __init__(self, close_buffer_minutes, sl_percent, max_hold_minutes):
        super().__init__(close_buffer_minutes, sl_percent)
        self.max_hold_minutes = max_hold_minutes

    def profit_decide(self, ctx):
        if (self.max_hold_minutes and ctx.opened_at is not None
                and ctx.now_ts is not None):
            held_min = (ctx.now_ts - ctx.opened_at) / 60000.0
            if held_min >= self.max_hold_minutes:
                return CloseReason.TIME_IN_TRADE
        return None


class BracketStrategy(BaseCloseStrategy):
    """可组合括弧：硬止损+时间强平(基类) + 保本/移动止盈/固定止盈/时长 任意开关。

    每个组件「值>0 即启用」。两阶段：先更新所有有状态子规则，再按优先级判触发：
    ③保本 > ④移动止盈 > ⑤固定止盈 > ⑥时长。动作均为整仓平，第一个命中胜出。
    """
    name = 'bracket'

    def __init__(self, close_buffer_minutes, sl_percent, tp_percent,
                 breakeven_activation, breakeven_lock,
                 trail_activation, trail_giveback, max_hold_minutes,
                 trail_relative_ratio=0.0, trail_relative_threshold=50.0):
        super().__init__(close_buffer_minutes, sl_percent)
        self.tp_percent = tp_percent
        self.be_activation = breakeven_activation
        self.be_lock = breakeven_lock
        self.trail_activation = trail_activation
        self.trail_giveback = trail_giveback
        self.trail_relative_ratio = trail_relative_ratio
        self.trail_relative_threshold = trail_relative_threshold
        self.max_hold_minutes = max_hold_minutes
        self.be_armed = False
        self.trail_armed = False
        self.peak = None

    def profit_decide(self, ctx):
        pnl = ctx.pnl_percent
        # 阶段1：更新所有有状态子规则（不管是否触发）
        if self.be_activation > 0 and not self.be_armed and pnl >= self.be_activation:
            self.be_armed = True
        if self.trail_activation > 0:
            if not self.trail_armed and pnl >= self.trail_activation:
                self.trail_armed = True
                self.peak = pnl
            elif self.trail_armed and (self.peak is None or pnl > self.peak):
                self.peak = pnl
        # 阶段2：按优先级判触发
        if self.be_activation > 0 and self.be_armed and pnl <= self.be_lock:
            return CloseReason.BREAKEVEN
        if self.trail_armed:
            gb = trailing_giveback(self.peak, self.trail_giveback,
                                   self.trail_relative_ratio, self.trail_relative_threshold)
            if pnl <= self.peak - gb:
                return CloseReason.TRAILING_STOP
        if self.tp_percent > 0 and pnl >= self.tp_percent:
            return CloseReason.TAKE_PROFIT
        if (self.max_hold_minutes > 0 and ctx.opened_at is not None
                and ctx.now_ts is not None):
            if (ctx.now_ts - ctx.opened_at) / 60000.0 >= self.max_hold_minutes:
                return CloseReason.TIME_IN_TRADE
        return None

    def state(self):
        return {'be_armed': self.be_armed, 'trail_armed': self.trail_armed, 'peak': self.peak}

    def load_state(self, d):
        if d:
            self.be_armed = bool(d.get('be_armed', False))
            self.trail_armed = bool(d.get('trail_armed', False))
            self.peak = d.get('peak')


STRATEGY_REGISTRY = {
    'threshold': ThresholdStrategy,
    'trailing': TrailingStrategy,
    'breakeven': BreakevenStrategy,
    'time_in_trade': TimeInTradeStrategy,
    'bracket': BracketStrategy,
}


def build_strategy(name, cfg) -> CloseStrategy:
    """按名称 + StrategyConfig 构建策略。未知名抛错。"""
    name = (name or 'threshold').lower()
    cb, sl = cfg.close_buffer_minutes, cfg.sl_percent
    if name == 'threshold':
        strat = ThresholdStrategy(cb, sl, cfg.tp_percent)
    elif name == 'trailing':
        strat = TrailingStrategy(cb, sl, cfg.trail_activation, cfg.trail_giveback,
                                 cfg.trail_relative_ratio, cfg.trail_relative_threshold)
    elif name == 'breakeven':
        # standalone 时若未配 activation 则回退默认 20
        strat = BreakevenStrategy(cb, sl, cfg.breakeven_activation or 20.0, cfg.breakeven_lock)
    elif name == 'time_in_trade':
        strat = TimeInTradeStrategy(cb, sl, cfg.max_hold_minutes or 60.0)
    elif name == 'bracket':
        strat = BracketStrategy(cb, sl, cfg.tp_percent, cfg.breakeven_activation,
                                cfg.breakeven_lock, cfg.trail_activation,
                                cfg.trail_giveback, cfg.max_hold_minutes,
                                cfg.trail_relative_ratio, cfg.trail_relative_threshold)
    else:
        raise ValueError(f'未知平仓策略: {name}（可选: {list(STRATEGY_REGISTRY)}）')
    # 收盘前强平的 DTE 阈值由配置统一注入（默认 1）
    strat.eod_close_max_dte = getattr(cfg, 'eod_close_max_dte', 1)
    return strat
