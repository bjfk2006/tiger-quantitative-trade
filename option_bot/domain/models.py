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
    PROPOSED = 'PROPOSED'      # 已产出开仓提案，等待人工 approve（铁鹰卖方用）
    OPENING = 'OPENING'
    MONITORING = 'MONITORING'
    CLOSING = 'CLOSING'
    CLOSED = 'CLOSED'
    ERROR = 'ERROR'


class CloseReason(Enum):
    """平仓触发原因。优先级（基类强制）：TIME > STOP_LOSS > 策略盈利了结(TP/TRAILING)。"""
    TAKE_PROFIT = 'TAKE_PROFIT'
    STOP_LOSS = 'STOP_LOSS'
    TIME_FORCE_CLOSE = 'TIME_FORCE_CLOSE'
    TRAILING_STOP = 'TRAILING_STOP'   # 移动止盈/回撤保护触发
    BREAKEVEN = 'BREAKEVEN'           # 保本止损（盈利回吐到保本线）触发
    TIME_IN_TRADE = 'TIME_IN_TRADE'   # 持仓时长上限触发
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
    tp_percent: float = 30.0          # 止盈阈值（+%，threshold 策略用）
    sl_percent: float = 50.0          # 硬止损阈值（-%，所有策略强制生效）
    close_buffer_minutes: int = 5     # 收盘前 N 分钟强平（所有策略强制生效）
    # 收盘前强平仅作用于 DTE≤该值的期权（0/1=临近到期当日平）；更长期权持有过夜
    eod_close_max_dte: int = 1
    # 平仓策略：threshold（默认，等价 tp/sl）/ trailing（移动止盈，涨破 activation 后回撤 giveback 平仓）
    strategy_name: str = 'threshold'
    trail_activation: float = 20.0    # trailing 武装阈值（+%）
    trail_giveback: float = 10.0      # trailing 从峰值回撤多少个点(绝对)即平仓
    # 相对比例回撤（混合）：peak≥threshold 时，回撤阈值取 max(trail_giveback, peak×ratio%)
    trail_relative_ratio: float = 0.0      # 0=关闭(纯绝对)；如 20 表示峰值的 20%
    trail_relative_threshold: float = 50.0 # 仅当峰值盈利≥此值才启用相对回撤
    # breakeven（保本止损）：盈利冲过 activation% 后，回吐到 lock% 即平（0=保本点）
    breakeven_activation: float = 0.0  # 0=该组件关闭（bracket 用；standalone 会回退默认）
    breakeven_lock: float = 0.0
    # 持仓时长上限（分钟）：0=关闭
    max_hold_minutes: float = 0.0
    # ---- 铁鹰(condor)卖方模式（设计：2026-06-26-condor-premium-selling-engine.md）----
    # 定义风险的双垂直信用价差：卖近月 ~short_delta 短腿、买 wing 外翼；IV 高才提案、人工确认开仓。
    condor_underlying: str = 'SPY'        # 标的（SPY/QQQ）
    condor_target_dte: int = 40           # 目标到期天数（30~45）
    condor_short_delta: float = 0.16      # 短腿目标 |delta|（~1σ 价外）
    condor_wing_width: float = 5.0        # 翼宽（行权价美元间距）
    condor_side: str = 'both'             # 结构：both(铁鹰)/call(bear call)/put(bull put)
    condor_commission_per_leg: float = 0.0  # 每腿每次执行佣金$（往返=腿数×2×张数），扣佣净盈亏用
    condor_min_iv: float = 0.20           # IV 入场闸：ATM 隐含波动率绝对下限
    condor_profit_target: float = 0.5     # 止盈：吃到 50% 权利金即平
    condor_stop_mult: float = 2.0         # 止损：亏到 2× 权利金即平
    condor_dte_exit: int = 21             # 到期前 N 天平仓（避 gamma）
    condor_max_loss_pct: float = 0.05     # 单仓最大亏损占账户比例（仓位上限）
    condor_account_equity: float = 0.0    # 账户净值（用于按 max_loss_pct 定张数；0=回退 max_qty）
    condor_proposal_ttl_min: float = 10.0 # 开仓提案有效期（分钟），过期或现价漂移则作废重评
    condor_synthetic_greeks: bool = True  # 券商无逐档 delta 时按 BS 自算（平价反推现价+briefs平值IV）
    condor_risk_free: float = 0.0         # 合成 delta 用无风险利率；0=用 briefs rates_bonds，>0 覆盖
    condor_iv_source: str = 'computed'    # 入场 IV 来源：computed(BS反推ATM活IV,默认) / briefs(旧volatility字段,对照)
    # 铁鹰平仓策略（可插拔，复用 close_strategies）。threshold=固定止盈(=今天行为)；trailing=移动止盈。
    # 信用口径：止盈/止损由 condor_profit_target×100 / condor_stop_mult×100 映射；trailing 单位=占权利金%。
    condor_close_strategy: str = 'threshold'  # threshold(默认,等价现状) / trailing
    condor_trail_activation: float = 0.0      # trailing 武装阈值(占权利金%)；0=未配
    condor_trail_giveback: float = 0.0        # trailing 从峰值回撤多少(占权利金%)即锁盈平仓
    # IV-Rank 入场闸（设计 2026-06-29-condor-iv-rank-entry-gate）。默认 absolute=今天行为零变化。
    condor_iv_gate_mode: str = 'absolute'     # absolute(IV≥min_iv) / rank(IVP≥阈值) / both(地板+IVP)
    condor_min_iv_rank: float = 50.0          # IV 分位入场阈值(0–100)，rank/both 用
    condor_iv_rank_floor: float = 0.0         # both 模式的绝对地板(IV小数)；0=无地板(both 退化为 rank)
    condor_iv_rank_lookback_days: int = 252   # IV 历史滚动窗口(交易日)
    condor_iv_rank_min_history: int = 60      # 暖机：历史不足此数则回退 absolute(用 min_iv)
    condor_iv_rank_seed_from_vix: bool = False  # 用 VIX(close−gap)回填历史加速暖机(口径近似,默认关)
    condor_iv_rank_vix_gap: float = 4.0       # 种子用 VIX 高于 ATM IV 的点数(偏斜溢价)
    condor_iv_history_file: str = ''          # IV 历史文件路径；空=从 state 目录派生(引擎/影子须指同一文件)
    condor_open_combo_type: str = 'CUSTOM'  # 开仓单类型：CUSTOM(单笔4腿原子) / VERTICAL(两垂直,回退)
    # ---- 双向跨式(straddle)多腿模式 ----
    mode: str = 'single'              # single（单腿）/ straddle（call+put 双腿）/ condor（铁鹰卖方）
    leg_stop: float = 10.0            # 单腿止损%（亏到即平该腿）
    straddle_tp_mode: str = 'trailing'  # 组合止盈：fixed / trailing
    straddle_tp: float = 10.0         # fixed：组合止盈%（总成本占比）
    straddle_trail_activation: float = 10.0  # trailing：组合武装阈值%
    straddle_trail_giveback: float = 10.0    # trailing：组合从峰值回撤%
    poll_interval: float = 2.0        # 监控轮询间隔（秒）
    near_close_poll_interval: float = 5.0  # 临近收盘窗口收紧后的最大间隔（秒）
    max_qty: int = 1                  # 单笔最大数量上限
    max_spread_pct: float = 5.0       # 市价单允许的最大相对点差（%）
    fill_poll_interval: float = 1.0   # 成交确认轮询间隔（秒）
    fill_timeout: float = 30.0        # 成交确认上限（秒）
    max_data_failures: int = 5        # 连续数据拉取失败触发 kill switch
    daily_loss_limit: float = 0.0     # 当日已实现亏损达此美元数即停止当日开仓(0=关闭)
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
    # 策略与其运行态（带默认值，兼容旧快照；用于 trailing 等有状态策略崩溃恢复）
    strategy_name: str = 'threshold'
    strategy_state: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'TradeSnapshot':
        # 忽略未知字段，向后兼容快照结构演进
        valid = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in valid})


@dataclass
class CondorLeg:
    """铁鹰的一条腿。side: 'BUY'(买翼)/'SELL'(卖体)。"""
    identifier: str
    put_call: str          # CALL / PUT
    side: str              # BUY / SELL
    strike: float
    qty: int
    entry_price: Optional[float] = None   # 该腿成交价（参考）


@dataclass
class CondorSnapshot:
    """铁鹰持仓崩溃恢复快照。legs 为 CondorLeg.__dict__ 列表。"""
    account: str
    symbol: str
    expiry: str                # YYYYMMDD
    qty: int
    legs: list
    entry_credit: float        # 每张净收权利金（每股口径）
    max_loss: float            # 每张最大亏损（每股口径）= 翼宽 − entry_credit
    state: str
    opened_at: Optional[int]
    external_id: Optional[str]
    combo_order_ids: list = field(default_factory=list)
    mid_credit: float = 0.0    # 开仓中间价信用（点差缺口/看板；默认 0 兼容旧快照）
    # 平仓策略与其运行态（带默认值，兼容旧快照；trailing 等有状态策略崩溃恢复用）
    strategy_name: str = 'threshold'
    strategy_state: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'CondorSnapshot':
        valid = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in valid})


@dataclass
class StraddleLeg:
    """跨式的一条腿（call 或 put）。"""
    identifier: str
    put_call: str
    qty: int
    entry_price: Optional[float] = None
    open_order_id: Optional[int] = None
    closed: bool = False
    realized_pnl: float = 0.0          # 平腿时记 (close-entry)*qty*100


@dataclass
class StraddleSnapshot:
    """跨式多腿崩溃恢复快照。"""
    account: str
    symbol: str
    expiry: str
    strike: float
    qty: int
    legs: list                         # [StraddleLeg.__dict__, ...]
    state: str
    opened_at: Optional[int]
    external_id: Optional[str]
    tp_mode: str = 'trailing'
    combo_armed: bool = False
    combo_peak: Optional[float] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> 'StraddleSnapshot':
        valid = cls.__dataclass_fields__.keys()
        return cls(**{k: v for k, v in d.items() if k in valid})
