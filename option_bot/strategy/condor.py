# -*- coding: utf-8 -*-
"""铁鹰(iron condor)卖方策略引擎（设计：docs/design/2026-06-26-condor-premium-selling-engine.md）。

定义风险的双垂直信用价差：卖近月 ~short_delta 短腿、买 wing 外翼；只在 IV 高时产出
开仓提案、**人工 approve 后才提交**；止盈(+50%)/止损(−2×)/到期前(≤dte_exit)平仓**自动执行**。

本文件分两层：
  ① 纯决策核心（无 SDK，易单测）：入场闸 / 选腿 / 组合权利金 / 最大亏损 / 张数 / 出场判定。
  ② IO 编排（CondorManager / CondorSupervisor，见文件后半）。
下单走 combo 净限价整笔成交，绝不逐腿——保证"定义风险"完整性。
"""
import datetime as _dt
import json
import logging
import os
import tempfile
import time

from option_bot.adapters.errors import CloseRejected, DataUnavailable, OpenRejected
from option_bot.domain.models import (BotState, CloseReason, CondorLeg,
                                      CondorSnapshot)
from option_bot.persistence.sink import NullSink

logger = logging.getLogger('option_bot.condor')

_FILLED = 'FILLED'
_COMBO_VERTICAL = 'VERTICAL'
# ⚠️ 组合净价/动作约定：按 SDK 示例(action='BUY', 信用价差 limit 取负=收款)。
#    务必先在 paper 账户验证成交方向/价格符号正确，再用于实盘。
_OPEN_ACTION = 'BUY'
_CLOSE_ACTION = 'SELL'


# ==================== ① 纯决策核心（可单测，无 SDK） ====================

def _num(x):
    """转 float；None/NaN/非数 → None。注意：0.0 是合法价（深虚值翼买价常为 0）。"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if x != x else x   # NaN != NaN


def _bid(q):
    return _num(q.get('bid_price')) if q else None


def _ask(q):
    return _num(q.get('ask_price')) if q else None


def _mid(q):
    if not q:
        return None
    b, a = _num(q.get('bid_price')), _num(q.get('ask_price'))
    if b is not None and a is not None:
        return (b + a) / 2.0
    return _num(q.get('latest_price'))


def passes_entry_gate(iv_now, iv_min, rth, has_position):
    """开仓提案前的入场闸。返回 (ok: bool, reason: str)。

    顺序：已有持仓 → 非 RTH → IV 不可得 → IV 低于闸 → 通过。
    """
    if has_position:
        return False, '已有持仓，单仓模式不再开新仓'
    if not rth:
        return False, '非常规交易时段(RTH)，不开仓'
    if iv_now is None:
        return False, 'ATM 隐含波动率不可得'
    if iv_now < iv_min:
        return False, f'IV {iv_now:.1%} < 入场闸 {iv_min:.1%}（溢价不够，不卖）'
    return True, 'ok'


def atm_iv(chain_rows):
    """链中 |delta| 最接近 0.5 的期权的隐含波动率（ATM IV 近似）。无有效行返回 None。"""
    best, best_iv = None, None
    for r in chain_rows:
        d, iv = _num(r.get('delta')), _num(r.get('implied_vol'))
        if d is None or iv is None:
            continue
        dist = abs(abs(d) - 0.5)
        if best is None or dist < best:
            best, best_iv = dist, iv
    return best_iv


def select_by_delta(chain_rows, target_abs_delta, put_call):
    """在指定方向(CALL/PUT)里选 |delta| 最接近 target 的一行。无候选返回 None。

    NaN 的 delta/strike 视为缺失（闭市或冷门行权价的希腊字母可能为 NaN）。
    """
    pc = put_call.upper()
    cands = [r for r in chain_rows
             if str(r.get('put_call', '')).upper() == pc
             and _num(r.get('delta')) is not None and _num(r.get('strike')) is not None]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(abs(_num(r['delta'])) - target_abs_delta))


def nearest_strike_row(chain_rows, target_strike, put_call):
    """在指定方向里选行权价最接近 target_strike 的一行（选翼用）。NaN 行权价跳过。"""
    pc = put_call.upper()
    cands = [r for r in chain_rows
             if str(r.get('put_call', '')).upper() == pc and _num(r.get('strike')) is not None]
    if not cands:
        return None
    return min(cands, key=lambda r: abs(_num(r['strike']) - target_strike))


def build_condor(call_rows, put_rows, short_delta, wing_width):
    """构建铁鹰四腿。返回 dict{legs, put_width, call_width} 或 None（结构非法）。

    legs：[{identifier, put_call, side(SELL/BUY), strike}]，顺序 put_long/put_short/call_short/call_long。
    校验 put_long < put_short < call_short < call_long，否则返回 None。
    """
    put_short = select_by_delta(put_rows, short_delta, 'PUT')
    call_short = select_by_delta(call_rows, short_delta, 'CALL')
    if not put_short or not call_short:
        return None
    put_long = nearest_strike_row(put_rows, float(put_short['strike']) - wing_width, 'PUT')
    call_long = nearest_strike_row(call_rows, float(call_short['strike']) + wing_width, 'CALL')
    if not put_long or not call_long:
        return None
    kps, kpl = float(put_short['strike']), float(put_long['strike'])
    kcs, kcl = float(call_short['strike']), float(call_long['strike'])
    if not (kpl < kps < kcs < kcl):
        return None

    def leg(row, side):
        return {'identifier': str(row.get('identifier')).strip(),
                'put_call': str(row.get('put_call')).upper(), 'side': side,
                'strike': float(row.get('strike'))}
    return {
        'legs': [leg(put_long, 'BUY'), leg(put_short, 'SELL'),
                 leg(call_short, 'SELL'), leg(call_long, 'BUY')],
        'put_width': kps - kpl,
        'call_width': kcl - kcs,
    }


def net_credit(legs, quote_by_id, fill='mid', closing=False):
    """组合净权利金（每股口径）= Σ_SELL价 − Σ_BUY价，在 quote_by_id 给的行情下。

    fill='mid' 用中间价；fill='conservative' 用最不利方向：
      开仓(closing=False) 卖腿吃 bid、买腿付 ask；平仓(closing=True) 买回卖腿付 ask、卖出买腿收 bid。
    任一腿行情缺失 → 返回 None（数据不全不决策）。
    """
    total = 0.0
    for lg in legs:
        q = quote_by_id.get(lg['identifier'])
        if q is None:
            return None
        if fill == 'mid':
            px = _mid(q)
        elif lg['side'] == 'SELL':
            px = _ask(q) if closing else _bid(q)
        else:  # BUY
            px = _bid(q) if closing else _ask(q)
        if px is None:
            return None
        total += px if lg['side'] == 'SELL' else -px
    return total


def condor_max_loss(put_width, call_width, entry_credit):
    """每股最大亏损 = max(翼宽) − 净权利金（到期只有一侧会被击穿）。下限 0。"""
    return max(0.0, max(put_width, call_width) - entry_credit)


def size_by_max_loss(max_loss_per_share, multiplier, equity, max_loss_pct, fallback_qty):
    """按"单仓最大亏损 ≤ 账户×max_loss_pct"定张数；账户净值未配(≤0)则回退 fallback_qty。"""
    per_contract = max_loss_per_share * multiplier
    if equity and equity > 0 and per_contract > 0:
        return max(0, int((equity * max_loss_pct) // per_contract))
    return fallback_qty


def exit_decision(entry_credit, close_cost, dte, profit_target=0.5,
                  stop_mult=2.0, dte_exit=21):
    """铁鹰出场判定（自动执行）。close_cost=当前平仓需付的净债（可负）。

    优先级：止盈(吃满 profit_target×权利金) > 止损(亏达 stop_mult×权利金) > 到期前(≤dte_exit)。
    """
    if entry_credit is None or close_cost is None:
        if dte is not None and dte <= dte_exit:
            return CloseReason.TIME_FORCE_CLOSE
        return None
    pnl = entry_credit - close_cost
    if entry_credit > 0 and pnl >= profit_target * entry_credit:
        return CloseReason.TAKE_PROFIT
    if entry_credit > 0 and pnl <= -stop_mult * entry_credit:
        return CloseReason.STOP_LOSS
    if dte is not None and dte <= dte_exit:
        return CloseReason.TIME_FORCE_CLOSE
    return None


# ==================== ② IO 编排：CondorManager / CondorSupervisor ====================

class CondorManager:
    """单铁鹰生命周期：提案 → (人工 approve) → 双垂直 combo 净限价开仓 → 自动监控/出场。

    状态：IDLE → PROPOSED → MONITORING → CLOSED。开仓需人工 approve()；
    止盈/止损/到期前平仓自动执行。combo 净限价整笔成交，保证定义风险。
    """

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
        self.symbol = self._cfg.condor_underlying
        self.expiry = None              # YYYYMMDD
        self._expiry_date = None        # YYYY-MM-DD（链查询用）
        self.qty = 0
        self.legs = []                  # [CondorLeg]
        self.entry_credit = None        # 每股
        self.max_loss = None            # 每股
        self.proposal = None            # 待批提案 dict
        self.combo_order_ids = []
        self._opened_at = None
        self._tag = None

    # ---------- 工具 ----------
    def _today_date(self):
        import pytz
        tz = pytz.timezone(self._cfg.timezone)
        return _dt.datetime.fromtimestamp(self._now_ms() / 1000.0, tz).date()

    def _dte(self, expiry_date_str):
        try:
            exp = _dt.datetime.strptime(expiry_date_str, '%Y-%m-%d').date()
            return (exp - self._today_date()).days
        except Exception:  # noqa: BLE001
            return None

    def _pick_expiry(self):
        """选 DTE 最接近 target_dte（且 ≥ dte_exit+1）的到期。返回 'YYYY-MM-DD' 或 None。"""
        rows = self._md.list_expirations(self.symbol)
        best, best_d = None, None
        for r in rows:
            d = r.get('date')
            dte = self._dte(d) if d else None
            if dte is None or dte <= self._cfg.condor_dte_exit:
                continue
            dist = abs(dte - self._cfg.condor_target_dte)
            if best is None or dist < best_d:
                best, best_d = d, dist
        return best

    def _quotes_for(self, legs):
        q = {}
        for lg in legs:
            ident = lg['identifier']
            q[ident] = self._md.get_option_quote(ident, market='US')
        return q

    # ---------- 提案（自动） ----------
    def _try_propose(self):
        if not self._md.is_market_trading('US'):
            return
        try:
            self._expiry_date = self._pick_expiry()
            if not self._expiry_date:
                logger.debug('无合适到期(target_dte=%s)', self._cfg.condor_target_dte)
                return
            chain = self._md.get_chain(self.symbol, self._expiry_date)
        except DataUnavailable as e:
            logger.warning('提案取链失败: %s', e)
            return
        iv = atm_iv(chain)
        ok, reason = passes_entry_gate(iv, self._cfg.condor_min_iv, True, False)
        if not ok:
            logger.info('铁鹰入场闸未过: %s', reason)
            return
        calls = [r for r in chain if str(r.get('put_call', '')).upper() == 'CALL']
        puts = [r for r in chain if str(r.get('put_call', '')).upper() == 'PUT']
        structure = build_condor(calls, puts, self._cfg.condor_short_delta,
                                 self._cfg.condor_wing_width)
        if not structure:
            logger.info('铁鹰结构构建失败（链不足/行权价不成序）')
            return
        legs = structure['legs']
        try:
            qbi = self._quotes_for(legs)
        except DataUnavailable as e:
            logger.warning('提案取腿行情失败: %s', e)
            return
        credit = net_credit(legs, qbi, 'conservative', closing=False)
        if credit is None or credit <= 0:
            logger.info('净权利金不为正(%.s)，放弃本次提案', credit)
            return
        maxloss = condor_max_loss(structure['put_width'], structure['call_width'], credit)
        qty = size_by_max_loss(maxloss, 100, self._cfg.condor_account_equity,
                               self._cfg.condor_max_loss_pct, self._cfg.max_qty)
        if qty < 1:
            logger.info('按风险预算定出张数<1，放弃')
            return
        spot = None
        try:
            spot = self._md.get_underlying_price(self.symbol)
        except DataUnavailable:
            pass
        self.expiry = self._expiry_date.replace('-', '')
        self.proposal = {
            'legs': legs, 'credit': round(credit, 4), 'mid_credit': net_credit(legs, qbi, 'mid'),
            'max_loss': round(maxloss, 4), 'qty': qty, 'iv': round(iv, 4),
            'expiry': self.expiry, 'expiry_date': self._expiry_date,
            'dte': self._dte(self._expiry_date), 'spot': spot,
            'created_ms': self._now_ms(),
            'put_width': structure['put_width'], 'call_width': structure['call_width'],
        }
        self.state = BotState.PROPOSED
        self._persist()
        logger.warning('★ 铁鹰开仓提案（待人工 approve）: %s %s DTE%s 现价%s | '
                       '净权利金/股≈%.2f 最大亏损/股≈%.2f 张数%s | 腿: %s',
                       self.symbol, self.expiry, self.proposal['dte'], spot,
                       credit, maxloss, qty,
                       ' '.join(f"{l['side']}{l['put_call'][0]}{l['strike']:g}" for l in legs))

    def _proposal_stale(self):
        p = self.proposal
        if p is None:
            return True
        age_min = (self._now_ms() - p['created_ms']) / 60000.0
        if age_min > self._cfg.condor_proposal_ttl_min:
            logger.info('提案超时(%.1f>%smin)，作废重评', age_min, self._cfg.condor_proposal_ttl_min)
            return True
        return False

    # ---------- 人工确认 ----------
    def approve(self):
        """人工批准开仓：提交两个垂直 combo 净限价单。返回 (ok, msg)。"""
        if self.state != BotState.PROPOSED or self.proposal is None:
            return False, '当前无待批提案'
        if self._proposal_stale():
            self.proposal = None
            self.state = BotState.IDLE
            self._persist()
            return False, '提案已过期，已作废（下轮重评）'
        p = self.proposal
        legs = p['legs']
        put_legs = [l for l in legs if l['put_call'] == 'PUT']    # BUY low + SELL high = 牛市认沽信用
        call_legs = [l for l in legs if l['put_call'] == 'CALL']  # SELL low + BUY high = 熊市认购信用
        qty = int(p['qty'])
        self._tag = self._td.new_dedup_tag()
        self.combo_order_ids = []
        try:
            qbi = self._quotes_for(legs)
        except DataUnavailable as e:
            return False, f'批准时取行情失败: {e}'
        for vlegs in (put_legs, call_legs):
            vcredit = net_credit(vlegs, qbi, 'mid', closing=False)
            if vcredit is None:
                return False, '批准时垂直腿行情缺失'
            # 信用价差：净价取负=收款（见 _OPEN_ACTION 注释，需 paper 验证）
            limit = -round(abs(vcredit), 2)
            try:
                oid = self._td.place_combo(self.symbol, self.expiry, vlegs, _COMBO_VERTICAL,
                                           _OPEN_ACTION, qty, limit, self._td.new_dedup_tag())
            except OpenRejected as e:
                return False, f'垂直 combo 开仓被拒: {e}'
            st = self._await_fill(oid)
            self.combo_order_ids.append(oid)
            if st['status'] != _FILLED and (st.get('filled') or 0) <= 0:
                logger.error('垂直 combo 未成交 order_id=%s —— 需人工核对挂单', oid)
                return False, f'垂直 combo 未在 {self._cfg.fill_timeout}s 内成交，请人工核对'
        # 成交：登记腿、进入监控
        self.qty = qty
        self.entry_credit = p['credit']
        self.max_loss = p['max_loss']
        self.legs = [CondorLeg(identifier=l['identifier'], put_call=l['put_call'],
                               side=l['side'], strike=l['strike'], qty=qty,
                               entry_price=_mid(qbi.get(l['identifier'])))
                     for l in legs]
        self._opened_at = self._now_ms()
        self.state = BotState.MONITORING
        self.proposal = None
        self._persist()
        logger.warning('铁鹰开仓成交 %s %s 张%s 净权利金/股≈%.2f -> MONITORING',
                       self.symbol, self.expiry, qty, self.entry_credit)
        return True, '已开仓，进入监控'

    def reject(self):
        if self.state != BotState.PROPOSED:
            return False, '当前无待批提案'
        self.proposal = None
        self.state = BotState.IDLE
        self._persist()
        logger.info('提案已被拒绝，回到 IDLE')
        return True, '提案已拒绝'

    # ---------- 每 tick ----------
    def run_once(self):
        if self.state == BotState.MONITORING:
            return self._monitor_once()
        if self.state == BotState.PROPOSED:
            if self._proposal_stale():
                self.proposal = None
                self.state = BotState.IDLE
                self._persist()
            return self._cfg.poll_interval
        if self.state in (BotState.IDLE, BotState.CLOSED):
            try:
                self._try_propose()
            except Exception as e:  # noqa: BLE001 —— 提案失败不应杀线程
                logger.warning('提案评估异常: %s', e)
        return self._cfg.poll_interval

    def _monitor_once(self):
        legdicts = [{'identifier': l.identifier, 'side': l.side,
                     'put_call': l.put_call, 'strike': l.strike} for l in self.legs]
        try:
            qbi = self._quotes_for(legdicts)
        except DataUnavailable as e:
            logger.warning('监控取腿行情失败: %s', e)
            return self._cfg.poll_interval
        close_cost = net_credit(legdicts, qbi, 'mid', closing=True)
        dte = self._dte(self._expiry_date) if self._expiry_date else None
        reason = exit_decision(self.entry_credit, close_cost, dte,
                               self._cfg.condor_profit_target, self._cfg.condor_stop_mult,
                               self._cfg.condor_dte_exit)
        if reason is not None:
            pnl = (self.entry_credit - close_cost) if close_cost is not None else None
            logger.warning('铁鹰触发出场 reason=%s 平仓成本/股≈%s pnl/股≈%s dte=%s',
                           reason.value, None if close_cost is None else round(close_cost, 2),
                           None if pnl is None else round(pnl, 2), dte)
            self._close_all(reason, qbi)
        self._persist()
        return self._cfg.poll_interval

    def _close_all(self, reason, qbi=None):
        """平掉两个垂直（反向 combo 净限价）。"""
        if not self.legs:
            self._mark_closed()
            return
        if qbi is None:
            try:
                qbi = self._quotes_for([{'identifier': l.identifier} for l in self.legs])
            except DataUnavailable as e:
                logger.error('平仓取行情失败，下个 tick 重试: %s', e)
                return
        puts = [l for l in self.legs if l.put_call == 'PUT']
        calls = [l for l in self.legs if l.put_call == 'CALL']
        all_ok = True
        for vlegs in (puts, calls):
            ld = [{'identifier': l.identifier, 'side': l.side,
                   'put_call': l.put_call, 'strike': l.strike} for l in vlegs]
            cost = net_credit(ld, qbi, 'mid', closing=True)
            limit = round(abs(cost), 2) if cost is not None else None
            try:
                oid = self._td.place_combo(self.symbol, self.expiry, ld, _COMBO_VERTICAL,
                                           _CLOSE_ACTION, self.qty, limit,
                                           self._td.new_dedup_tag())
                st = self._await_fill(oid)
                if st['status'] != _FILLED:
                    all_ok = False
                    logger.error('平垂直未确认成交 order_id=%s，下个 tick 重试', oid)
            except (OpenRejected, CloseRejected) as e:
                all_ok = False
                logger.error('平垂直被拒，将重试: %s', e)
        if all_ok:
            for l in self.legs:
                self._sink.on_position_closed(l.identifier)
            self._mark_closed()
            logger.warning('铁鹰已平仓 reason=%s -> CLOSED', reason.value)

    def _mark_closed(self):
        self.state = BotState.CLOSED
        self.legs = []
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

    # ---------- 恢复/持久化 ----------
    def resume(self):
        snap = self._load()
        if not snap:
            return False
        if snap.account != self._td.account:
            logger.warning('铁鹰快照账户≠当前账户，丢弃')
            self._clear()
            return False
        if snap.state != BotState.MONITORING.value or not snap.legs:
            self._clear()
            return False
        self.symbol, self.expiry, self.qty = snap.symbol, snap.expiry, snap.qty
        self._expiry_date = (snap.expiry[:4] + '-' + snap.expiry[4:6] + '-' + snap.expiry[6:]) \
            if snap.expiry and len(snap.expiry) == 8 else None
        self.legs = [CondorLeg(**lg) for lg in snap.legs]
        self.entry_credit, self.max_loss = snap.entry_credit, snap.max_loss
        self._opened_at, self._tag = snap.opened_at, snap.external_id
        self.combo_order_ids = snap.combo_order_ids or []
        self.state = BotState.MONITORING
        logger.info('已恢复铁鹰 %s %s 张%s -> MONITORING', self.symbol, self.expiry, self.qty)
        return True

    def _snapshot(self):
        return CondorSnapshot(
            account=self._td.account, symbol=self.symbol, expiry=self.expiry or '',
            qty=self.qty, legs=[l.__dict__ for l in self.legs],
            entry_credit=self.entry_credit or 0.0, max_loss=self.max_loss or 0.0,
            state=self.state.value, opened_at=self._opened_at, external_id=self._tag,
            combo_order_ids=self.combo_order_ids)

    def _persist(self):
        data = json.dumps(self._snapshot().to_dict(), ensure_ascii=False)
        d = os.path.dirname(os.path.abspath(self._state_path))
        fd, tmp = tempfile.mkstemp(prefix='.obcdr_', dir=d)
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
                return CondorSnapshot.from_dict(json.load(f))
        except Exception as e:  # noqa: BLE001
            logger.error('读铁鹰快照失败: %s', e)
            return None

    def _clear(self):
        if os.path.exists(self._state_path):
            try:
                os.remove(self._state_path)
            except OSError:
                pass

    def status(self):
        return {
            'mode': 'condor', 'state': self.state.value, 'symbol': self.symbol,
            'expiry': self.expiry, 'qty': self.qty, 'entry_credit': self.entry_credit,
            'max_loss': self.max_loss,
            'proposal': self.proposal,
            'legs': [{'id': l.identifier, 'side': l.side, 'pc': l.put_call,
                      'strike': l.strike, 'entry': l.entry_price} for l in self.legs],
        }


class CondorSupervisor:
    """铁鹰 bot 线程：自动评估/监控/出场；开仓经 ops approve/reject 人工确认。"""

    def __init__(self, manager, config, command_queue, sleep=time.sleep):
        self._m = manager
        self._cfg = config
        self._queue = command_queue
        self._sleep = sleep
        self._stopped = False
        self.bot_alive = False

    def stop(self):
        self._stopped = True

    def run(self):
        self.bot_alive = True
        try:
            try:
                self._m.resume()
            except Exception as e:  # noqa: BLE001
                logger.warning('铁鹰恢复失败: %s', e)
            while not self._stopped:
                self._drain()
                if self._stopped:
                    break
                interval = self._m.run_once()
                self._sleep(interval)
        except Exception as e:  # noqa: BLE001
            logger.critical('铁鹰监督器异常退出: %s', e, exc_info=True)
        finally:
            self.bot_alive = False

    def _drain(self):
        from option_bot.service import (CMD_APPROVE, CMD_CLOSE, CMD_REJECT, CMD_STOP)
        for cmd in self._queue.drain():
            try:
                if cmd == CMD_APPROVE:
                    ok, msg = self._m.approve()
                    logger.warning('[approve] %s', msg)
                elif cmd == CMD_REJECT:
                    ok, msg = self._m.reject()
                    logger.warning('[reject] %s', msg)
                elif cmd == CMD_CLOSE and self._m.state == BotState.MONITORING:
                    self._m._close_all(CloseReason.MANUAL)
                elif cmd == CMD_STOP:
                    self._stopped = True
            except Exception as e:  # noqa: BLE001
                logger.error('铁鹰命令 %s 执行失败: %s', cmd, e)

    def status(self):
        st = self._m.status()
        st['bot_alive'] = self.bot_alive
        st['queue_size'] = self._queue.size()
        return st
