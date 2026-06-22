# -*- coding: utf-8 -*-
"""单持仓生命周期状态机（设计文档 §5 Flow 5.1/5.2/5.3）。

状态：IDLE -> OPENING -> MONITORING -> CLOSING -> CLOSED。
幂等：平仓前必查剩余可卖量；单线程轮询，同一持仓至多一张在途平仓单。
真相源永远是券商侧 get_order/get_positions，本地快照仅辅助恢复。
"""
import logging
import time

from option_bot.adapters.errors import CloseRejected, DataUnavailable, OpenRejected
from option_bot.domain.models import (BotState, CloseReason, Direction,
                                      OptionPick, TradeSnapshot)
from option_bot.strategy.risk_guard import compute_pnl_percent

logger = logging.getLogger('option_bot.sm')

_FILLED = 'FILLED'


class PositionStateMachine:
    def __init__(self, trading_adapter, market_data_adapter, state_store, config,
                 sleep=time.sleep, now_ms=None, sink=None):
        self._td = trading_adapter
        self._md = market_data_adapter
        self._store = state_store
        self._cfg = config
        self._sleep = sleep
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))
        # 事件下沉：默认 NullSink（不持久化），服务模式注入 SqliteSink
        from option_bot.persistence.sink import NullSink
        self._sink = sink or NullSink()

        self.state = BotState.IDLE
        self.pick = None
        self.direction = None
        self.qty = 0
        self.entry_price = None
        self.open_order_id = None

    # ---------- 开仓 ----------
    def open(self, pick: OptionPick, direction: Direction, qty: int):
        if self.state not in (BotState.IDLE, BotState.CLOSED):
            raise OpenRejected(f'当前状态 {self.state.value} 不可开新仓')
        if qty > self._cfg.max_qty:
            raise OpenRejected(f'数量 {qty} 超过单笔上限 max_qty={self._cfg.max_qty}')
        if direction.put_call != pick.put_call:
            raise OpenRejected(f'方向 {direction.value} 与期权 {pick.put_call} 不匹配')

        quote = self._md.get_option_quote(pick.identifier, market=pick.market)
        trading = self._md.is_market_trading(pick.market)
        ok, reason = self._pre_open_ok(quote, trading)
        if not ok:
            raise OpenRejected(reason)

        self.pick, self.direction, self.qty = pick, direction, qty
        tag = self._td.new_dedup_tag()
        self.state = BotState.OPENING
        self._save(tag, None)

        order_id = self._td.open_market(pick, qty, tag)  # 单次提交，不盲重发
        self.open_order_id = order_id
        self._save(tag, order_id)

        st = self._await_fill(order_id)
        filled = st.get('filled') or 0
        if st['status'] != _FILLED and filled <= 0:
            # 完全未成交：不盲目重发，保留 OPENING 与快照供人工核对（设计 Flow 5.1 失败模式）
            raise OpenRejected(
                f'开仓未在 {self._cfg.fill_timeout}s 内成交(status={st["status"]})，'
                f'请手动核对挂单 order_id={order_id}')
        if st['status'] != _FILLED:
            # 部分成交：已有真实持仓，必须纳入盯盘保护，绝不留无人盯盘仓位（设计 §10 R2）
            logger.warning('开仓部分成交 filled=%s/%s，按已成交数量进入监控', filled, qty)
            self.qty = int(filled)

        self.entry_price = st['avg_fill_price'] or self._mid_from_quote(quote)
        self.state = BotState.MONITORING
        self._save(tag, order_id)
        self._sink.on_open(self._td.account, self.pick, self.direction,
                           self.qty, self.entry_price, order_id)
        logger.info('开仓成交 entry=%s qty=%s -> MONITORING', self.entry_price, self.qty)
        return order_id

    def _pre_open_ok(self, quote, trading):
        # 预检逻辑放在 RiskGuard，但状态机需要它的结论；通过 config 直接复用
        from option_bot.strategy.risk_guard import RiskGuard
        return RiskGuard(self._cfg).pre_open_check(quote, trading)

    @staticmethod
    def _mid_from_quote(quote):
        if not quote:
            return None
        bid, ask = quote.get('bid_price'), quote.get('ask_price')
        if bid and ask:
            return (bid + ask) / 2.0
        return quote.get('latest_price')

    # ---------- 平仓 ----------
    def close(self, reason: CloseReason):
        """平仓。可被止盈/止损/时间强平/手动触发；幂等。"""
        if self.state == BotState.CLOSED:
            return None
        if self.state == BotState.IDLE:
            raise CloseRejected('无持仓可平')
        # 时间强平是硬性安全，不受 enable_auto_close 开关影响（设计 §10 R5 无条件）
        if (reason not in (CloseReason.MANUAL, CloseReason.TIME_FORCE_CLOSE)
                and not self._cfg.enable_auto_close):
            logger.warning('自动平仓开关关闭，跳过 reason=%s', reason.value)
            return None

        # 平仓前必查剩余可卖量——幂等关键：若上一次其实已成交，这里 qty=0 直接收尾
        pos = self._td.get_option_position(self.pick)
        if pos is None or not pos.salable_qty or pos.salable_qty <= 0:
            logger.info('已无可卖持仓，标记 CLOSED')
            self._mark_closed()
            return None

        sell_qty = int(pos.salable_qty)
        if sell_qty < 1:
            # 可卖量不足 1 张（期权按整张计），视为无可平仓位，避免发出 0 数量委托
            logger.info('可卖量不足 1 张(%.4f)，标记 CLOSED', pos.salable_qty)
            self._mark_closed()
            return None
        self.state = BotState.CLOSING
        tag = self._td.new_dedup_tag()
        order_id = self._td.close_market(self.pick, sell_qty, tag)
        st = self._await_fill(order_id)
        if st['status'] != _FILLED:
            # 未「确认」完全成交（含成交状态未知 remaining=None）一律退回 MONITORING，
            # 下个 tick 会重新查 salable_qty 后再决定是否重试——绝不在未确认时标记 CLOSED
            # 而丢弃快照，避免遗弃仍持有的仓位（设计 Flow 5.2 CLOSING_RETRY / §10 R2）。
            self.state = BotState.MONITORING
            raise CloseRejected(
                f'平仓未确认完全成交(status={st["status"]} remaining={st.get("remaining")})，将重试')
        self._sink.on_close(self._td.account, self.pick, self.direction, sell_qty,
                            st['avg_fill_price'], reason, order_id, self.entry_price)
        logger.info('平仓成交 reason=%s order_id=%s -> CLOSED', reason.value, order_id)
        self._mark_closed()
        return order_id

    def _mark_closed(self):
        self.state = BotState.CLOSED
        self._store.clear()
        if self.pick:
            self._sink.on_position_closed(self.pick.identifier)

    # ---------- 成交确认轮询 ----------
    def _await_fill(self, order_id):
        deadline = time.time() + self._cfg.fill_timeout
        last = {'status': 'UNKNOWN', 'filled': 0, 'remaining': None, 'avg_fill_price': 0}
        while time.time() < deadline:
            try:
                last = self._td.get_order_status(order_id)
            except DataUnavailable as e:
                logger.warning('成交确认查询失败，重试: %s', e)
                self._sleep(self._cfg.fill_poll_interval)
                continue
            if last['status'] == _FILLED or (last.get('remaining') == 0):
                last['status'] = _FILLED
                return last
            self._sleep(self._cfg.fill_poll_interval)
        return last

    # ---------- 当前持仓盈亏%（含降级估算，设计 R3）----------
    def current_pnl_percent(self):
        """返回 (pnl_percent, position_view)。无持仓返回 (None, None)。"""
        pos = self._td.get_option_position(self.pick)
        if pos is None or not pos.quantity:
            return None, None
        # 每 tick 刷新持仓快照到持久化（看板读取）
        self._sink.on_position(self._td.account, self.pick, self.direction,
                               self.entry_price, pos)
        if pos.unrealized_pnl_percent is not None:
            return pos.unrealized_pnl_percent, pos
        # 降级：用行情 mid 与入场价估算
        quote = self._md.get_option_quote(self.pick.identifier, market=self.pick.market)
        cur = self._mid_from_quote(quote)
        return compute_pnl_percent(self.entry_price, cur), pos

    # ---------- 崩溃恢复（设计 Flow 5.3）----------
    def resume(self):
        """启动时以远端持仓为准核对快照，恢复 MONITORING 或清快照。"""
        snap = self._store.load()
        if not snap:
            return False
        # M1: 校验快照账户 == 当前账户，防止切换账户后认领错账户的同名合约
        if snap.account != self._td.account:
            logger.warning('快照账户(%s) != 当前账户(%s)，丢弃快照、不认领该持仓（防跨账户误管理）',
                           snap.account, self._td.account)
            self._store.clear()
            return False
        try:
            pick = OptionPick(**snap.pick)
        except TypeError:
            logger.error('快照 pick 字段不兼容，丢弃')
            self._store.clear()
            return False
        pos = None
        try:
            pos = self._td.get_option_position(pick)
        except DataUnavailable as e:
            logger.warning('恢复时查询持仓失败，稍后由监控循环重试: %s', e)
        if pos is None or not pos.quantity:
            logger.info('远端无对应持仓，清除陈旧快照')
            self._store.clear()
            return False
        self.pick = pick
        self.direction = Direction(snap.direction)
        self.qty = snap.qty
        self.entry_price = snap.entry_price
        self.open_order_id = snap.open_order_id
        self.state = BotState.MONITORING
        logger.info('已从快照恢复持仓 %s qty=%s -> MONITORING', pick.identifier, snap.qty)
        return True

    # ---------- 快照写入 ----------
    def _save(self, tag, order_id):
        snap = TradeSnapshot(
            account=self._td.account,
            direction=self.direction.value if self.direction else None,
            pick=self.pick.__dict__ if self.pick else None,
            qty=self.qty,
            entry_price=self.entry_price,
            tp_percent=self._cfg.tp_percent,
            sl_percent=self._cfg.sl_percent,
            close_buffer_minutes=self._cfg.close_buffer_minutes,
            open_order_id=order_id,
            external_id=tag,
            state=self.state.value,
            opened_at=self._now_ms(),
        )
        self._store.save(snap)
