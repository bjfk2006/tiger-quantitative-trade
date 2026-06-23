# -*- coding: utf-8 -*-
"""双向跨式(straddle)多腿持仓 + 腿管理（设计：docs/design/2026-06-23-straddle-dual-leg.md）。

同标的/到期/行权价买 call+put 两腿；腿亏到 -leg_stop 平该腿；组合盈亏按 fixed/trailing
止盈了结剩余；收盘前强平兜底；无硬止损。纯决策逻辑(combined_pnl_percent/decide_combined_close/
legs_to_stop)与 IO(StraddleManager) 分离，便于单测。
"""
import json
import logging
import os
import tempfile
import time

from option_bot.adapters.errors import CloseRejected, DataUnavailable, OpenRejected
from option_bot.domain.models import (BotState, CloseReason, Direction,
                                      OptionPick, StraddleLeg, StraddleSnapshot)
from option_bot.persistence.sink import NullSink

logger = logging.getLogger('option_bot.straddle')

_FILLED = 'FILLED'


# ---------------- 纯决策逻辑（可单测） ----------------
def combined_pnl_percent(legs, market_by_id):
    """组合盈亏% = (已实现 + 未实现) / 总成本 × 100。

    任一**未平腿**缺现价 → 返回 None（数据不全，本 tick 不决策）。总成本 0 → None。
    """
    total_cost = sum((l.entry_price or 0) * l.qty * 100 for l in legs if l.entry_price)
    if total_cost <= 0:
        return None
    realized = sum(l.realized_pnl for l in legs if l.closed)
    unrealized = 0.0
    for l in legs:
        if l.closed:
            continue
        mk = market_by_id.get(l.identifier)
        if mk is None or l.entry_price is None:
            return None
        unrealized += (mk - l.entry_price) * l.qty * 100
    return (realized + unrealized) / total_cost * 100.0


def decide_combined_close(combined_pnl, mtc, close_buffer, tp_mode, tp,
                          trail_activation, trail_giveback, combo_state):
    """组合层"平掉所有腿"决策（mutates combo_state for trailing）。返回 CloseReason 或 None。"""
    if mtc is not None and mtc <= close_buffer:
        return CloseReason.TIME_FORCE_CLOSE
    if combined_pnl is None:
        return None
    if tp_mode == 'fixed':
        if combined_pnl >= tp:
            return CloseReason.TAKE_PROFIT
        return None
    # trailing：两阶段
    if not combo_state.get('armed'):
        if combined_pnl >= trail_activation:
            combo_state['armed'] = True
            combo_state['peak'] = combined_pnl
        return None
    peak = combo_state.get('peak')
    if peak is None or combined_pnl > peak:
        combo_state['peak'] = peak = combined_pnl
    if combined_pnl <= peak - trail_giveback:
        return CloseReason.TRAILING_STOP
    return None


def legs_to_stop(legs, market_by_id, leg_stop):
    """返回需按腿止损平掉的未平腿 identifier 列表（per-leg pnl% ≤ -leg_stop）。"""
    out = []
    for l in legs:
        if l.closed or l.entry_price is None:
            continue
        mk = market_by_id.get(l.identifier)
        if mk is None:
            continue
        leg_pnl = (mk - l.entry_price) / l.entry_price * 100.0
        if leg_pnl <= -leg_stop:
            out.append(l.identifier)
    return out


# ---------------- IO 编排 ----------------
class StraddleManager:
    def __init__(self, trading_adapter, market_data_adapter, config, clock,
                 state_path, sink=None, sleep=time.sleep, now_ms=None):
        self._td = trading_adapter
        self._md = market_data_adapter
        self._cfg = config
        self._clock = clock
        self._state_path = state_path
        self._sink = sink or NullSink()
        self._sleep = sleep
        self._now_ms = now_ms or (lambda: int(time.time() * 1000))

        self.state = BotState.IDLE
        self.symbol = self.expiry = None
        self.strike = None
        self.qty = 0
        self.legs = []
        self._picks = {}
        self.combo_state = {'armed': False, 'peak': None}
        self._tag = None
        self._opened_at = None

    @staticmethod
    def _leg_dir(leg):
        return Direction.LONG if leg.put_call == 'CALL' else Direction.SHORT

    def _mid(self, quote):
        if not quote:
            return None
        b, a = quote.get('bid_price'), quote.get('ask_price')
        return (b + a) / 2.0 if (b and a) else quote.get('latest_price')

    # ---------- 开仓（双腿） ----------
    def open(self, symbol, expiry, strike, qty):
        if self.state not in (BotState.IDLE, BotState.CLOSED):
            raise OpenRejected(f'当前状态 {self.state.value} 不可开新跨式')
        if qty > self._cfg.max_qty:
            raise OpenRejected(f'数量 {qty} 超过单笔上限 max_qty={self._cfg.max_qty}')
        call = self._md.resolve_option(symbol, expiry, strike, 'CALL')
        put = self._md.resolve_option(symbol, expiry, strike, 'PUT')
        trading = self._md.is_market_trading(call.market)
        from option_bot.strategy.risk_guard import RiskGuard
        rg = RiskGuard(self._cfg)
        for pick in (call, put):
            q = self._md.get_option_quote(pick.identifier, market=pick.market)
            ok, reason = rg.pre_open_check(q, trading)
            if not ok:
                raise OpenRejected(f'{pick.put_call} 腿预检失败: {reason}')

        self.symbol = symbol
        self.expiry = call.expiry
        self.strike = float(strike)
        self.qty = qty
        self.legs = [StraddleLeg(call.identifier, 'CALL', qty),
                     StraddleLeg(put.identifier, 'PUT', qty)]
        self._picks = {call.identifier: call, put.identifier: put}
        self.combo_state = {'armed': False, 'peak': None}
        self._tag = self._td.new_dedup_tag()
        self._opened_at = self._now_ms()
        self.state = BotState.MONITORING
        self._persist()

        for leg in self.legs:
            pick = self._picks[leg.identifier]
            try:
                oid = self._td.open_market(pick, qty, self._td.new_dedup_tag())
                st = self._await_fill(oid)
            except OpenRejected as e:
                leg.closed = True
                logger.error('%s 腿开仓被拒，跳过: %s', leg.put_call, e)
                continue
            if st['status'] != _FILLED and (st.get('filled') or 0) <= 0:
                leg.closed = True
                logger.error('%s 腿未成交，跳过 order_id=%s', leg.put_call, oid)
                continue
            leg.open_order_id = oid
            q = self._md.get_option_quote(pick.identifier, market=pick.market)
            leg.entry_price = st['avg_fill_price'] or self._mid(q)
            self._sink.on_open(self._td.account, pick, self._leg_dir(leg), qty,
                               leg.entry_price, oid)
            self._persist()
            logger.info('跨式开腿成交 %s entry=%s', leg.identifier, leg.entry_price)

        if all(l.closed for l in self.legs):
            logger.error('两腿均未成交，跨式未建立')
            self._mark_closed()
        return [l.identifier for l in self.legs if not l.closed]

    # ---------- 每 tick ----------
    def run_once(self):
        mtc = self._clock.minutes_to_close()
        market_by_id = {}
        try:
            for leg in self.legs:
                if leg.closed:
                    continue
                pick = self._picks[leg.identifier]
                view = self._td.get_option_position(pick)
                if view is None or not view.quantity:
                    leg.closed = True   # 远端已无此腿（外部平掉）
                    continue
                market_by_id[leg.identifier] = view.market_price
                self._sink.on_position(self._td.account, pick, self._leg_dir(leg),
                                       leg.entry_price, view)
        except DataUnavailable as e:
            logger.warning('跨式取持仓失败: %s', e)
            return self._cfg.poll_interval

        combined = combined_pnl_percent(self.legs, market_by_id)
        # ① 时间强平 / ② 组合止盈（fixed/trailing）→ 平所有
        reason = decide_combined_close(
            combined, mtc, self._cfg.close_buffer_minutes, self._cfg.straddle_tp_mode,
            self._cfg.straddle_tp, self._cfg.straddle_trail_activation,
            self._cfg.straddle_trail_giveback, self.combo_state)
        if reason is not None:
            self._close_all(reason)
            self._persist()
            return self._cfg.poll_interval
        # ③ 腿止损
        for ident in legs_to_stop(self.legs, market_by_id, self._cfg.leg_stop):
            self._close_leg(ident, CloseReason.STOP_LOSS)
        if all(l.closed for l in self.legs):
            self._mark_closed()
        self._persist()
        return self._cfg.poll_interval

    def _close_leg(self, identifier, reason):
        leg = next((l for l in self.legs if l.identifier == identifier and not l.closed), None)
        if leg is None:
            return
        pick = self._picks[leg.identifier]
        pos = self._td.get_option_position(pick)
        if pos is None or not pos.salable_qty or int(pos.salable_qty) < 1:
            leg.closed = True
            return
        sell_qty = int(pos.salable_qty)
        oid = self._td.close_market(pick, sell_qty, self._td.new_dedup_tag())
        st = self._await_fill(oid)
        if st['status'] != _FILLED:
            logger.error('腿 %s 平仓未确认完全成交，下个 tick 重试', identifier)
            return
        leg.closed = True
        cp = st['avg_fill_price']
        if cp is not None and leg.entry_price is not None:
            leg.realized_pnl = (cp - leg.entry_price) * leg.qty * 100
        self._sink.on_close(self._td.account, pick, self._leg_dir(leg), sell_qty,
                            cp, reason, oid, leg.entry_price)
        self._sink.on_position_closed(identifier)
        logger.info('跨式平腿 %s reason=%s realized=%.2f', identifier, reason.value,
                    leg.realized_pnl)

    def _close_all(self, reason):
        for leg in self.legs:
            if not leg.closed:
                try:
                    self._close_leg(leg.identifier, reason)
                except CloseRejected as e:
                    logger.error('平腿失败: %s', e)
        if all(l.closed for l in self.legs):
            self._mark_closed()

    def _mark_closed(self):
        self.state = BotState.CLOSED
        self._clear()

    def _await_fill(self, order_id):
        deadline = time.time() + self._cfg.fill_timeout
        last = {'status': 'UNKNOWN', 'filled': 0, 'remaining': None, 'avg_fill_price': 0}
        while time.time() < deadline:
            try:
                last = self._td.get_order_status(order_id)
            except DataUnavailable:
                self._sleep(self._cfg.fill_poll_interval)
                continue
            if last['status'] == _FILLED or last.get('remaining') == 0:
                last['status'] = _FILLED
                return last
            self._sleep(self._cfg.fill_poll_interval)
        return last

    # ---------- 恢复 ----------
    def resume(self):
        snap = self._load()
        if not snap:
            return False
        if snap.account != self._td.account:
            logger.warning('跨式快照账户≠当前账户，丢弃')
            self._clear()
            return False
        self.symbol, self.expiry, self.strike, self.qty = (
            snap.symbol, snap.expiry, snap.strike, snap.qty)
        self.legs = [StraddleLeg(**lg) for lg in snap.legs]
        self.combo_state = {'armed': snap.combo_armed, 'peak': snap.combo_peak}
        self._opened_at = snap.opened_at
        self._tag = snap.external_id
        self._picks = {}
        for leg in self.legs:
            self._picks[leg.identifier] = OptionPick(
                symbol=self.symbol, expiry=self.expiry, strike=self.strike,
                put_call=leg.put_call, identifier=leg.identifier)
        # 以券商持仓为准对齐
        any_open = False
        for leg in self.legs:
            if leg.closed:
                continue
            try:
                pos = self._td.get_option_position(self._picks[leg.identifier])
            except DataUnavailable:
                any_open = True  # 查不到先按持有，稍后重试
                continue
            if pos is None or not pos.quantity:
                leg.closed = True
            else:
                any_open = True
        if not any_open:
            logger.info('跨式远端无持仓，清快照')
            self._clear()
            return False
        self.state = BotState.MONITORING
        logger.info('已恢复跨式 %s 行权价=%s tp_mode=%s -> MONITORING',
                    self.symbol, self.strike, self._cfg.straddle_tp_mode)
        return True

    # ---------- 快照持久化 ----------
    def _snapshot(self):
        return StraddleSnapshot(
            account=self._td.account, symbol=self.symbol, expiry=self.expiry,
            strike=self.strike, qty=self.qty, legs=[l.__dict__ for l in self.legs],
            state=self.state.value, opened_at=self._opened_at, external_id=self._tag,
            tp_mode=self._cfg.straddle_tp_mode, combo_armed=self.combo_state.get('armed', False),
            combo_peak=self.combo_state.get('peak'))

    def _persist(self):
        data = json.dumps(self._snapshot().to_dict(), ensure_ascii=False)
        d = os.path.dirname(os.path.abspath(self._state_path))
        fd, tmp = tempfile.mkstemp(prefix='.obstr_', dir=d)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(data)
            os.replace(tmp, self._state_path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def _load(self):
        if not os.path.exists(self._state_path):
            return None
        try:
            with open(self._state_path, 'r', encoding='utf-8') as f:
                return StraddleSnapshot.from_dict(json.load(f))
        except Exception as e:
            logger.error('读跨式快照失败: %s', e)
            return None

    def _clear(self):
        if os.path.exists(self._state_path):
            try:
                os.remove(self._state_path)
            except OSError:
                pass

    def status(self):
        return {
            'mode': 'straddle', 'state': self.state.value, 'symbol': self.symbol,
            'strike': self.strike, 'tp_mode': self._cfg.straddle_tp_mode,
            'combo_armed': self.combo_state.get('armed'),
            'combo_peak': self.combo_state.get('peak'),
            'legs': [{'id': l.identifier, 'closed': l.closed, 'entry': l.entry_price,
                      'realized': l.realized_pnl} for l in self.legs],
        }


class StraddleSupervisor:
    """跨式 bot 线程：与单腿 Supervisor 同构（run/status/命令排空）。"""

    def __init__(self, manager, config, command_queue, open_spec=None,
                 allow_live_open=False, is_paper=True, sleep=time.sleep):
        self._m = manager
        self._cfg = config
        self._queue = command_queue
        self._open_spec = open_spec
        self._allow_live_open = allow_live_open
        self._is_paper = is_paper
        self._sleep = sleep
        self._stopped = False
        self.bot_alive = False

    def stop(self):
        self._stopped = True

    def run(self):
        self.bot_alive = True
        try:
            recovered = False
            try:
                recovered = self._m.resume()
            except Exception as e:  # noqa: BLE001
                logger.warning('跨式恢复失败: %s', e)
            if not recovered and self._open_spec:
                if not self._is_paper and not self._allow_live_open:
                    logger.critical('实盘账户默认禁止自动开跨式；需 OBOT_ALLOW_LIVE_AUTO_OPEN=true')
                else:
                    self._do_open()
            while not self._stopped:
                self._drain()
                if self._stopped:
                    break
                interval = self._m.run_once() if self._m.state == BotState.MONITORING \
                    else self._cfg.poll_interval
                self._sleep(interval)
        except Exception as e:  # noqa: BLE001
            logger.critical('跨式监督器异常退出: %s', e, exc_info=True)
        finally:
            self.bot_alive = False

    def _do_open(self):
        s = self._open_spec
        try:
            self._m.open(s['symbol'], s['expiry'], s['strike'], int(s['qty']))
        except Exception as e:  # noqa: BLE001
            logger.error('跨式 OPEN_ON_START 失败(看板仍可用): %s', e)

    def _drain(self):
        from option_bot.service import (CMD_CLOSE, CMD_STOP)
        while True:
            items = self._queue.drain()
            if not items:
                break
            for cmd in items:
                if cmd == CMD_CLOSE and self._m.state == BotState.MONITORING:
                    self._m._close_all(CloseReason.MANUAL)
                elif cmd == CMD_STOP:
                    self._stopped = True

    def status(self):
        st = self._m.status()
        st['bot_alive'] = self.bot_alive
        st['queue_size'] = self._queue.size()
        return st
