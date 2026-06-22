# -*- coding: utf-8 -*-
"""领域模型：枚举与值对象（无 SDK 依赖，便于单测）.

对应设计文档 §4 Domain/Config 层。
"""
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, Optional


class Direction(Enum):
    """开仓方向。两者都是 BUY-to-open（风险限定在权利金）。

    设计文档约束：做多 = 买入 Call；做空 = 买入 Put。
    """
    LONG = 'LONG'    # 看涨 -> 买入 CALL
    SHORT = 'SHORT'  # 看跌 -> 买入 PUT

    @property
    def put_call(self) -> str:
        return 'CALL' if self is Direction.LONG else 'PUT'


class BotState(Enum):
    """单持仓生命周期状态（设计文档 §5）。"""
    IDLE = 'IDLE'
    OPENING = 'OPENING'
    MONITORING = 'MONITORING'
    CLOSING = 'CLOSING'
    CLOSED = 'CLOSED'
    ERROR = 'ERROR'


class CloseReason(Enum):
    """平仓触发原因。评估优先级见 RiskGuard：TIME > STOP_LOSS > TAKE_PROFIT。"""
    TAKE_PROFIT = 'TAKE_PROFIT'
    STOP_LOSS = 'STOP_LOSS'
    TIME_FORCE_CLOSE = 'TIME_FORCE_CLOSE'
    MANUAL = 'MANUAL'


@dataclass
class OptionPick:
    """用户选定的单腿期权。

    expiry 统一存为 'YYYYMMDD'（下单合约与持仓筛选都用该格式）。
    identifier 为行情/期权链返回的标准标识，如 'AAPL  250815C00090000'。
    """
    symbol: str
    expiry: str            # YYYYMMDD
    strike: float
    put_call: str          # CALL / PUT
    identifier: str
    multiplier: int = 100
    currency: str = 'USD'
    market: str = 'US'


@dataclass
class PositionView:
    """对一笔期权持仓的只读快照（由 TradingAdapter 从 SDK Position 归一化）。

    unrealized_pnl_percent 单位为「百分数」：+30.0 表示 +30%（adapter 已将
    SDK 的小数 0.30 乘以 100）。
    """
    quantity: float
    salable_qty: float
    average_cost: Optional[float]
    market_price: Optional[float]
    unrealized_pnl: Optional[float]
    unrealized_pnl_percent: Optional[float]


@dataclass
class StrategyConfig:
    """策略与风控参数（设计文档 §1 非功能 / §10 风控）。"""
    tp_percent: float = 30.0          # 止盈阈值（+%）
    sl_percent: float = 50.0          # 止损阈值（-%，正数表示亏损幅度）
    close_buffer_minutes: int = 5     # 收盘前 N 分钟强平
    poll_interval: float = 2.0        # 监控轮询间隔（秒）
    near_close_poll_interval: float = 5.0  # 临近收盘窗口收紧后的最大间隔（秒）
    max_qty: int = 1                  # 单笔最大数量上限
    max_spread_pct: float = 5.0       # 市价单允许的最大相对点差（%）
    fill_poll_interval: float = 1.0   # 成交确认轮询间隔（秒）
    fill_timeout: float = 30.0        # 成交确认上限（秒）
    max_data_failures: int = 5        # 连续数据拉取失败触发 kill switch
    enable_open: bool = True
    enable_auto_close: bool = True
    # 半日市（提前收盘）日期表：'YYYY-MM-DD' -> 'HH:MM'(美东)。SDK 不提供，需本地配置。
    regular_close: str = '16:00'
    early_close_dates: Dict[str, str] = field(default_factory=dict)
    market: str = 'US'
    timezone: str = 'America/New_York'

    def validate(self) -> None:
        if self.tp_percent <= 0:
            raise ValueError('tp_percent 必须 > 0')
        if self.sl_percent <= 0:
            raise ValueError('sl_percent 必须 > 0（表示亏损幅度）')
        if self.close_buffer_minutes < 0:
            raise ValueError('close_buffer_minutes 不能为负')
        if self.poll_interval <= 0:
            raise ValueError('poll_interval 必须 > 0')
        if self.max_qty <= 0:
            raise ValueError('max_qty 必须 > 0')


@dataclass
class TradeSnapshot:
    """崩溃恢复用的本地状态快照（设计文档 §7）。"""
    account: str
    direction: str
    pick: dict
    qty: int
    entry_price: Optional[float]
    tp_percent: float
    sl_percent: float
    close_buffer_minutes: int
    open_order_id: Optional[int]
    external_id: Optional[str]
    state: str
    opened_at: Optional[int]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'TradeSnapshot':
        return cls(**d)
