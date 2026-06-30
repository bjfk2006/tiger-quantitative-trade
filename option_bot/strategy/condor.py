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
import math
import os
import tempfile
import time

from option_bot.adapters.errors import CloseRejected, DataUnavailable, OpenRejected
from option_bot.domain.models import (BotState, CloseReason, CondorLeg,
                                      CondorSnapshot, OptionPick, PositionView)
from option_bot.persistence.sink import NullSink
from option_bot.strategy.close_strategies import (StrategyContext,
                                                  build_condor_close_strategy)
from option_bot.strategy.iv_history import (IVHistoryStore, iv_percentile,
                                            iv_rank)

logger = logging.getLogger('option_bot.condor')

_FILLED = 'FILLED'
_COMBO_VERTICAL = 'VERTICAL'
_COMBO_CUSTOM = 'CUSTOM'        # 单笔 4 腿原子组合（避免两垂直间半成交）
# combo 动作/净价约定（2026-06-26 paper 实测确认）：action='BUY'，开仓 limit 取负=收款(信用)，
# 成交 avg_fill 为负=收到权利金。平仓镜像之：显式翻转每条腿 BUY/SELL，action='BUY'，limit 取正=付债买回。
_OPEN_ACTION = 'BUY'


def _reverse_legs(legs):
    """翻转每条腿 BUY<->SELL（平仓/回滚：把"开仓腿动作"变成"减仓腿动作"）。"""
    flip = {'BUY': 'SELL', 'SELL': 'BUY'}
    out = []
    for l in legs:
        nl = dict(l)
        nl['side'] = flip.get(str(l['side']).upper(), str(l['side']).upper())
        out.append(nl)
    return out


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


def passes_entry_gate(iv_now, iv_min, rth, has_position,
                      mode='absolute', ivp=None, min_rank=50.0,
                      rank_floor=0.0, history_ok=True):
    """开仓提案前的入场闸。返回 (ok: bool, reason: str)。

    短路顺序：已有持仓 → 非 RTH → IV 不可得 → 按 mode 判 IV。
    mode：absolute=IV≥iv_min(默认,今日行为) / rank=IVP≥min_rank /
          both=IV≥rank_floor 且 IVP≥min_rank。
    暖机未满(history_ok=False)时 rank/both **一律回退 absolute(用 iv_min)**——数据不足不乱开仓。
    """
    if has_position:
        return False, '已有持仓，单仓模式不再开新仓'
    if not rth:
        return False, '非常规交易时段(RTH)，不开仓'
    if iv_now is None:
        return False, 'ATM 隐含波动率不可得'
    eff = mode if (mode == 'absolute' or history_ok) else 'absolute'
    if eff == 'absolute':
        if iv_now < iv_min:
            return False, f'IV {iv_now:.1%} < 入场闸 {iv_min:.1%}（溢价不够，不卖）'
        return True, 'ok'
    if ivp is None:
        return False, 'IV 分位不可得（历史不足/计算失败）'
    if eff == 'both' and rank_floor > 0 and iv_now < rank_floor:
        return False, f'IV {iv_now:.1%} < 绝对地板 {rank_floor:.1%}'
    if ivp < min_rank:
        return False, f'IV 分位 {ivp:.0f} < 入场分位 {min_rank:.0f}（相对不够贵）'
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


def build_condor(call_rows, put_rows, short_delta, wing_width, side='both'):
    """构建铁鹰四腿，或单边垂直信用价差。返回 dict{legs, put_width, call_width} 或 None。

    side: 'both'(铁鹰,默认) | 'call'(bear call: 只卖 call 价差) | 'put'(bull put: 只卖 put 价差)。
    legs：[{identifier, put_call, side(SELL/BUY), strike}]，顺序 put_long/put_short/call_short/call_long
    （单边只含对应两腿）。校验行权价成序（both: kpl<kps<kcs<kcl；call: kcs<kcl；put: kpl<kps），否则 None。
    单边时另一侧 width=0.0，使 condor_max_loss=max(width)−credit 自动取该价差宽度。
    """
    side = (side or 'both').lower()
    if side not in ('both', 'call', 'put'):
        side = 'both'

    def leg(row, act):
        return {'identifier': str(row.get('identifier')).strip(),
                'put_call': str(row.get('put_call')).upper(), 'side': act,
                'strike': float(row.get('strike'))}

    put_long = put_short = call_short = call_long = None
    kps = kpl = kcs = kcl = None
    put_width = call_width = 0.0

    if side in ('both', 'put'):
        put_short = select_by_delta(put_rows, short_delta, 'PUT')
        if not put_short:
            return None
        put_long = nearest_strike_row(put_rows, float(put_short['strike']) - wing_width, 'PUT')
        if not put_long:
            return None
        kps, kpl = float(put_short['strike']), float(put_long['strike'])
        if not (kpl < kps):
            return None
        put_width = kps - kpl

    if side in ('both', 'call'):
        call_short = select_by_delta(call_rows, short_delta, 'CALL')
        if not call_short:
            return None
        call_long = nearest_strike_row(call_rows, float(call_short['strike']) + wing_width, 'CALL')
        if not call_long:
            return None
        kcs, kcl = float(call_short['strike']), float(call_long['strike'])
        if not (kcs < kcl):
            return None
        call_width = kcl - kcs

    if side == 'both' and not (kpl < kps < kcs < kcl):
        return None

    legs = []
    if side in ('both', 'put'):
        legs += [leg(put_long, 'BUY'), leg(put_short, 'SELL')]
    if side in ('both', 'call'):
        legs += [leg(call_short, 'SELL'), leg(call_long, 'BUY')]

    return {'legs': legs, 'put_width': put_width, 'call_width': call_width}


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


def condor_pnl_percent(entry_credit, close_cost):
    """铁鹰盈亏归一化为「占入场权利金的%」，喂给可插拔平仓策略的 pnl_percent。

    pnl_percent = (入场权利金 − 当前平仓成本)/入场权利金 × 100。
    任一缺失或入场权利金≤0 → None（与既有"不可得只判时间"语义一致）。
    """
    if entry_credit is None or close_cost is None or entry_credit <= 0:
        return None
    return (entry_credit - close_cost) / entry_credit * 100.0


# ---------- 合成希腊字母兜底（券商无逐档 delta 时按 Black-Scholes 自算）----------
# 设计：docs/design/2026-06-26-condor-synthetic-greeks-fallback.md
# 起因：HK paper 账户行情不返回逐档 delta/IV（chain.delta 全 0、briefs 只给标的平值
# volatility），无法按 16Δ 选腿。兜底：put-call 平价反推现价 + briefs 平值 IV/利率，BS 自算 delta。

def _parse_pct(x):
    """'16.65%' → 0.1665；已是小数则原样；None/非数 → None。"""
    if x is None:
        return None
    if isinstance(x, str):
        s = x.strip()
        if s.endswith('%'):
            v = _num(s[:-1])
            return None if v is None else v / 100.0
        return _num(s)
    return _num(x)


def norm_cdf(x):
    """标准正态 CDF（用 math.erf，不引 scipy）。"""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot, strike, t_years, iv, r, put_call):
    """Black-Scholes delta（call 正、put 负）。参数非法返回 None。"""
    s, k, t, sig = _num(spot), _num(strike), _num(t_years), _num(iv)
    rr = _num(r) or 0.0
    if not (s and k and t and sig) or s <= 0 or k <= 0 or t <= 0 or sig <= 0:
        return None
    d1 = (math.log(s / k) + (rr + 0.5 * sig * sig) * t) / (sig * math.sqrt(t))
    nd1 = norm_cdf(d1)
    return nd1 if str(put_call).upper() == 'CALL' else nd1 - 1.0


def greeks_missing(chain_rows):
    """链中是否无任何可用逐档 delta（全为 None/0）→ 需启用合成兜底。"""
    for r in chain_rows:
        if _num(r.get('delta')):    # 非 None 且非 0
            return False
    return True


def implied_spot(chain_rows):
    """put-call 平价反推现价：近 ATM（|C−P| 最小）若干档取 S≈C_mid−P_mid+K 的中位数。

    抗噪：只用买卖价均 >0 的档；深 ITM 报价陈旧故按 |C−P| 排序取最接近平值的前若干档。
    无可用档返回 None。
    """
    by_k = {}
    for r in chain_rows:
        k = _num(r.get('strike'))
        if k is None:
            continue
        m = _mid(r)
        if m is None or m <= 0:
            continue
        pc = str(r.get('put_call', '')).upper()
        by_k.setdefault(k, {})[pc] = m
    ests = []
    for k, pc in by_k.items():
        c, p = pc.get('CALL'), pc.get('PUT')
        if c and p:
            ests.append((abs(c - p), c - p + k))   # 按 |C−P| 近平值排序，S≈C−P+K
    if not ests:
        return None
    ests.sort(key=lambda e: e[0])
    svals = sorted(e[1] for e in ests[:8])
    n = len(svals)
    return svals[n // 2] if n % 2 else (svals[n // 2 - 1] + svals[n // 2]) / 2.0


def enrich_greeks(chain_rows, spot, iv, t_years, r):
    """对缺失/为 0 delta 的行就地填入 BS 自算 delta，并补 implied_vol。返回填充行数。"""
    n = 0
    for row in chain_rows:
        if _num(row.get('delta')):      # 已有真 delta，跳过
            continue
        d = bs_delta(spot, _num(row.get('strike')), t_years, iv, r, row.get('put_call'))
        if d is None:
            continue
        row['delta'] = d
        if not _num(row.get('implied_vol')):
            row['implied_vol'] = iv
        n += 1
    return n


# ---------- 自算「活 IV」入场信号（从 ATM 期权 mid 做 BS 反推）----------
# 设计：docs/design/2026-06-27-condor-live-iv-signal.md
# 起因：briefs.volatility 是按日/陈旧的标的平值（实测整夜冻结 16.65%，含盘中），不随盘动、
# 还偏高，不适合 vol 择时；chain.implied_vol 又全 0。故从期权链已有 bid/ask 算 ATM mid，
# 反推隐含波动率作入场闸/合成 delta 的 σ（逐 tick 更新，无额外行情订阅）。

def bs_price(spot, strike, t_years, iv, r, put_call):
    """Black-Scholes 期权理论价（欧式）。参数非法返回 None。"""
    s, k, t, sig = _num(spot), _num(strike), _num(t_years), _num(iv)
    rr = _num(r) or 0.0
    if not (s and k and t and sig) or s <= 0 or k <= 0 or t <= 0 or sig <= 0:
        return None
    sq = sig * math.sqrt(t)
    d1 = (math.log(s / k) + (rr + 0.5 * sig * sig) * t) / sq
    d2 = d1 - sq
    disc = math.exp(-rr * t)
    if str(put_call).upper() == 'CALL':
        return s * norm_cdf(d1) - k * disc * norm_cdf(d2)
    return k * disc * norm_cdf(-d2) - s * norm_cdf(-d1)


def implied_vol_from_price(price, spot, strike, t_years, r, put_call,
                           lo=1e-3, hi=3.0, iters=60):
    """二分反推隐含波动率：找 σ 使 bs_price(σ)=price。

    稳健（无 vega 除零）。价低于内在价/高于无套利上界、或落在 [lo,hi] 价区间外 → 返回 None。
    """
    p, s, k, t = _num(price), _num(spot), _num(strike), _num(t_years)
    rr = _num(r) or 0.0
    if not (p and s and k and t) or p <= 0 or s <= 0 or k <= 0 or t <= 0:
        return None
    pc = str(put_call).upper()
    disc = math.exp(-rr * t)
    intrinsic = max(0.0, s - k * disc) if pc == 'CALL' else max(0.0, k * disc - s)
    upper = s if pc == 'CALL' else k * disc        # call≤S, put≤K·e^(−rT)
    if p <= intrinsic + 1e-9 or p >= upper:        # 坏价/超无套利区间
        return None
    fa = bs_price(s, k, t, lo, rr, pc)
    fb = bs_price(s, k, t, hi, rr, pc)
    if fa is None or fb is None:
        return None
    fa -= p
    fb -= p
    if fa * fb > 0:                                # price 不在 [lo,hi] 对应价区间内
        return None
    a, b = lo, hi
    for _ in range(iters):
        m = 0.5 * (a + b)
        fm = bs_price(s, k, t, m, rr, pc) - p      # bs_price 关于 σ 单调增
        if abs(fm) < 1e-8 or (b - a) < 1e-7:
            return m
        if (fa < 0) == (fm < 0):                   # 同号 → 根在 [m, b]
            a, fa = m, fm
        else:
            b = m
    return 0.5 * (a + b)


def atm_iv_live(chain_rows, spot, t_years, r, n_strikes=3, max_rel_spread=0.5):
    """近 ATM 多档 call/put 的 mid 各做 BS 反推，取中位数作活 ATM IV。无有效值→None。

    鲁棒：只取最接近 spot 的 n_strikes 档（vega 大、反推稳）；剔除 mid 缺失/≤0、
    点差/ mid 超 max_rel_spread（报价不可信）、反推失败或越界(≤1%或≥300%)的腿；
    call 与 put 互为校验。skew 影响因只取近 ATM 而轻微（仍是平值单一 IV）。
    """
    s = _num(spot)
    if s is None or s <= 0:
        return None
    strikes = sorted({_num(rw.get('strike')) for rw in chain_rows if _num(rw.get('strike'))},
                     key=lambda kk: abs(kk - s))[:n_strikes]
    kset = set(strikes)
    ivs = []
    for rw in chain_rows:
        k = _num(rw.get('strike'))
        if k is None or k not in kset:
            continue
        mid = _mid(rw)
        if mid is None or mid <= 0:
            continue
        b, a = _bid(rw), _ask(rw)
        if b is not None and a is not None and (a - b) / mid > max_rel_spread:
            continue                               # 点差过宽，mid 不可信
        iv = implied_vol_from_price(mid, s, k, t_years, r, rw.get('put_call'))
        if iv is not None and 0.01 < iv < 3.0:
            ivs.append(iv)
    if not ivs:
        return None
    ivs.sort()
    n = len(ivs)
    return ivs[n // 2] if n % 2 else (ivs[n // 2 - 1] + ivs[n // 2]) / 2.0


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

        # 可插拔平仓策略（默认 threshold=固定止盈,等价旧 exit_decision；可换 trailing 移动止盈）
        self._strategy = build_condor_close_strategy(self._cfg)
        # IV-Rank 入场闸：活 IV 历史存储（引擎/影子共用同一文件）。默认 absolute 模式不影响判定。
        iv_hist_path = (self._cfg.condor_iv_history_file
                        or os.path.join(os.path.dirname(os.path.abspath(state_path)) or '.',
                                        f'iv_history_{self._cfg.condor_underlying}.json'))
        self._iv_store = IVHistoryStore(iv_hist_path, self._cfg.condor_iv_rank_lookback_days)
        if self._cfg.condor_iv_rank_seed_from_vix:
            vix_csv = os.path.join(os.path.dirname(os.path.abspath(iv_hist_path)) or '.',
                                   'VIX_History.csv')
            try:
                n = self._iv_store.seed_from_vix(vix_csv, self._cfg.condor_iv_rank_vix_gap,
                                                 self._today_date().isoformat())
                if n:
                    logger.info('IV-Rank 种子：从 VIX 回填 %d 日历史', n)
            except Exception as e:  # noqa: BLE001 —— 种子失败不应中断启动
                logger.warning('IV-Rank 种子失败: %s', e)
        self._last_iv = self._last_ivp = self._last_ivr = None
        self.state = BotState.IDLE
        self.symbol = self._cfg.condor_underlying
        self.expiry = None              # YYYYMMDD
        self._expiry_date = None        # YYYY-MM-DD（链查询用）
        self.qty = 0
        self.legs = []                  # [CondorLeg]
        self.entry_credit = None        # 每股
        self.mid_credit = None          # 开仓时中间价信用（点差缺口/看板卡片用）
        self.max_loss = None            # 每股
        self.proposal = None            # 待批提案 dict
        self.combo_order_ids = []
        self._last_close_cost = None    # 最近一次有效盯市平仓成本（看板/状态透出）
        self._last_pnl_pct = None       # 最近一次浮盈亏%(of credit)
        self._opened_at = None
        self._tag = None
        # 提案评估限频：market_state 等接口限流(~10/min)，IDLE 时每 60s 评估一次即可
        self._last_propose_ms = 0
        self._propose_throttle_ms = 60000
        self._idle_interval = 5.0   # IDLE/PROPOSED 轮询间隔(秒)，保证 approve 命令及时响应

    # ---------- 工具 ----------
    def _leg_pick(self, leg):
        return OptionPick(symbol=self.symbol, expiry=self.expiry or '', strike=leg.strike,
                          put_call=leg.put_call, identifier=leg.identifier)

    @staticmethod
    def _leg_view(leg, mid):
        """按腿 side 算正确符号的盈亏，构造 PositionView 喂看板（卖腿盈亏与买腿相反）。"""
        sgn = 1.0 if leg.side == 'BUY' else -1.0
        upnl = uppct = None
        if leg.entry_price:
            upnl = sgn * (mid - leg.entry_price) * leg.qty * 100
            uppct = sgn * (mid - leg.entry_price) / leg.entry_price * 100.0
        return PositionView(quantity=leg.qty, salable_qty=leg.qty,
                            average_cost=leg.entry_price, market_price=mid,
                            unrealized_pnl=upnl, unrealized_pnl_percent=uppct)

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

    def _fetch_iv_rate(self, chain, spot):
        """取最接近现价一档的 brief，解析平值 IV(volatility) 与无风险利率(rates_bonds)。

        账户的 volatility 是标的层面单值(全链同值)，故取近 ATM 一档即可。返回 (iv, r)。
        """
        best, best_d = None, None
        for r in chain:
            k = _num(r.get('strike'))
            ident = r.get('identifier')
            if k is None or not ident:
                continue
            d = abs(k - spot) if spot else 0.0
            if best is None or d < best_d:
                best, best_d = str(ident).strip(), d
        if not best:
            return None, None
        try:
            q = self._md.get_option_quote(best, market='US')
        except DataUnavailable:
            return None, None
        if not q:
            return None, None
        return _parse_pct(q.get('volatility')), _num(q.get('rates_bonds'))

    def _resolve_iv(self, chain, spot, t_years, r, briefs_iv):
        """入场闸/合成 delta 用的 IV 来源（condor_iv_source）：

        'computed'(默认)=从近 ATM 期权 mid BS 反推的活 IV，反推失败回退 briefs；
        'briefs'=旧 volatility 字段（陈旧标的平值，仅作对照/兜底）。
        """
        if (self._cfg.condor_iv_source or 'computed').lower() == 'briefs':
            return briefs_iv
        live = atm_iv_live(chain, spot, t_years, r)
        if live is not None:
            logger.info('活 IV(BS反推 ATM)=%.4f（briefs volatility=%s，仅参考）', live, briefs_iv)
            return live
        logger.info('活 IV 反推失败，回退 briefs volatility=%s', briefs_iv)
        return briefs_iv

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
        # 合成 greeks 兜底：券商无逐档 delta 时按 BS 自算（平价反推现价 + briefs 平值IV/利率）
        spot = None
        if self._cfg.condor_synthetic_greeks and greeks_missing(chain):
            spot = implied_spot(chain)
            briefs_iv, rate = self._fetch_iv_rate(chain, spot)   # r + 旧字段(回退用)
            dte_now = self._dte(self._expiry_date)
            if spot is None or not dte_now or dte_now <= 0:
                logger.info('合成 greeks 失败：现价/DTE 不可得(spot=%s dte=%s)', spot, dte_now)
                return
            r_used = self._cfg.condor_risk_free or rate or 0.0
            t_years = dte_now / 365.0
            iv = self._resolve_iv(chain, spot, t_years, r_used, briefs_iv)   # 活 IV(默认) / briefs
            if iv is None:
                logger.info('合成 greeks 失败：IV 不可得(活算+briefs 均无)')
                return
            filled = enrich_greeks(chain, spot, iv, t_years, r_used)
            logger.info('合成 greeks：现价≈%.2f IV=%.4f r=%.4f DTE=%s 填充%d行',
                        spot, iv, r_used, dte_now, filled)
        else:
            iv = atm_iv(chain)
        # 活 IV 历史采样 + IV 分位/Rank（IV-Rank 入场闸；absolute 模式下不影响判定）
        if iv is not None:
            self._iv_store.append_daily(self._today_date().isoformat(), iv)
        hist = self._iv_store.values()
        self._last_iv, self._last_ivp, self._last_ivr = (
            iv, iv_percentile(hist, iv), iv_rank(hist, iv))
        ok, reason = passes_entry_gate(
            iv, self._cfg.condor_min_iv, True, False,
            mode=self._cfg.condor_iv_gate_mode, ivp=self._last_ivp,
            min_rank=self._cfg.condor_min_iv_rank, rank_floor=self._cfg.condor_iv_rank_floor,
            history_ok=len(hist) >= self._cfg.condor_iv_rank_min_history)
        if not ok:
            logger.info('铁鹰入场闸未过: %s (IV=%s IVP=%s IVR=%s 历史%d日)', reason,
                        None if iv is None else round(iv, 4),
                        None if self._last_ivp is None else round(self._last_ivp, 0),
                        None if self._last_ivr is None else round(self._last_ivr, 0), len(hist))
            return
        calls = [r for r in chain if str(r.get('put_call', '')).upper() == 'CALL']
        puts = [r for r in chain if str(r.get('put_call', '')).upper() == 'PUT']
        structure = build_condor(calls, puts, self._cfg.condor_short_delta,
                                 self._cfg.condor_wing_width,
                                 getattr(self._cfg, 'condor_side', 'both'))
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
        if spot is None:   # 合成路径已用平价反推出 spot；否则取标的现价（若有权限）
            try:
                spot = self._md.get_underlying_price(self.symbol)
            except DataUnavailable:
                pass
        self.expiry = self._expiry_date.replace('-', '')
        self.proposal = {
            'legs': legs, 'credit': round(credit, 4), 'mid_credit': net_credit(legs, qbi, 'mid'),
            'max_loss': round(maxloss, 4), 'qty': qty, 'iv': round(iv, 4),
            'ivp': None if self._last_ivp is None else round(self._last_ivp, 1),
            'ivr': None if self._last_ivr is None else round(self._last_ivr, 1),
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
        """人工批准开仓。开仓单类型由 condor_open_combo_type 决定（CUSTOM 原子 / VERTICAL 回退）。

        失败时 _submit_open 已撤单/回滚干净，状态回 IDLE，绝不留孤儿仓。返回 (ok, msg)。
        """
        if self.state != BotState.PROPOSED or self.proposal is None:
            return False, '当前无待批提案'
        if self._proposal_stale():
            self.proposal = None
            self.state = BotState.IDLE
            self._persist()
            return False, '提案已过期，已作废（下轮重评）'
        p = self.proposal
        legs = p['legs']
        qty = int(p['qty'])
        self._tag = self._td.new_dedup_tag()
        try:
            qbi = self._quotes_for(legs)
        except DataUnavailable as e:
            return False, f'批准时取行情失败: {e}'
        ok, order_ids = self._submit_open(legs, qty, qbi)
        if not ok:
            # _submit_open 内已撤单/回滚已成交腿；作废提案回 IDLE，下轮重评
            self.proposal = None
            self.state = BotState.IDLE
            self.combo_order_ids = []
            self._persist()
            return False, '开仓未完成（已撤单/回滚），回到 IDLE'
        # 成交：登记腿、进入监控
        self.combo_order_ids = order_ids
        self.qty = qty
        self.entry_credit = p['credit']
        self.mid_credit = p.get('mid_credit')
        self.max_loss = p['max_loss']
        self.legs = [CondorLeg(identifier=l['identifier'], put_call=l['put_call'],
                               side=l['side'], strike=l['strike'], qty=qty,
                               entry_price=_mid(qbi.get(l['identifier'])))
                     for l in legs]
        self._opened_at = self._now_ms()
        self.state = BotState.MONITORING
        self.proposal = None
        # 落库：CUSTOM 全部腿归唯一 oid；VERTICAL put 腿归 oid[0]、call 腿归 oid[1]
        is_custom = len(order_ids) == 1
        for leg in self.legs:
            if is_custom:
                oid = order_ids[0]
            else:
                oid = order_ids[0] if leg.put_call == 'PUT' else order_ids[-1]
            try:
                self._sink.on_open(self._td.account, self._leg_pick(leg), leg.side,
                                   leg.qty, leg.entry_price, oid)
            except Exception as e:  # noqa: BLE001 —— 落库失败不应影响交易
                logger.warning('on_open 落库失败 %s: %s', leg.identifier, e)
        self._persist()
        logger.warning('铁鹰开仓成交 %s %s 张%s 净权利金/股≈%.2f -> MONITORING',
                       self.symbol, self.expiry, qty, self.entry_credit)
        return True, '已开仓，进入监控'

    # ---------- 开仓提交（原子/回退）+ 半成交回滚 ----------
    def _submit_open(self, legs, qty, qbi):
        """按 condor_open_combo_type 提交开仓。返回 (ok, order_ids)。失败时已撤单/回滚。"""
        if (self._cfg.condor_open_combo_type or 'CUSTOM').upper() == _COMBO_VERTICAL:
            return self._open_vertical(legs, qty, qbi)
        return self._open_custom(legs, qty, qbi)

    def _open_custom(self, legs, qty, qbi):
        """单笔 4 腿 CUSTOM 原子开仓。未成交则撤单 + 防竞态复查 + 必要回滚。"""
        # marketable 限价：用保守信用(卖腿吃 bid/买腿付 ask)，跨价差才成交；
        # 入场闸已保证保守信用>0，故成交后仍正期望。mid 限价几乎不成交（实测）。
        total = net_credit(legs, qbi, 'conservative', closing=False)
        if total is None or total <= 0:
            logger.error('CUSTOM 开仓保守信用不可得/非正(%s)，放弃', total)
            return False, []
        limit = -round(abs(total), 2)        # 负=收款(信用)
        try:
            oid = self._td.place_combo(self.symbol, self.expiry, legs, _COMBO_CUSTOM,
                                       _OPEN_ACTION, qty, limit, self._td.new_dedup_tag())
        except OpenRejected as e:
            logger.error('CUSTOM 开仓被拒: %s', e)
            return False, []
        if self._await_fill(oid)['status'] == _FILLED:
            return True, [oid]
        logger.error('CUSTOM 开仓未在 %ss 内成交，撤单回滚', self._cfg.fill_timeout)
        self._cancel_quiet(oid)
        if self._poll_filled(oid):           # 撤单/成交竞态：撤后仍成交则逐腿拉平
            logger.error('CUSTOM 撤单后仍成交，逐腿回滚')
            self._unwind(legs, qty)
        return False, []

    def _open_vertical(self, legs, qty, qbi):
        """回退：两个垂直 combo。任一未成交则撤该单 + 回滚已成交的另一垂直。"""
        puts = [l for l in legs if l['put_call'] == 'PUT']
        calls = [l for l in legs if l['put_call'] == 'CALL']
        order_ids = []
        filled = []                          # 已成交垂直的腿，失败时回滚
        for vlegs in (puts, calls):
            vcredit = net_credit(vlegs, qbi, 'conservative', closing=False)   # marketable
            if vcredit is None or vcredit <= 0:
                logger.error('垂直腿保守信用不可得/非正(%s)，回滚已成交腿', vcredit)
                self._unwind([l for g in filled for l in g], qty)
                return False, order_ids
            limit = -round(abs(vcredit), 2)
            try:
                oid = self._td.place_combo(self.symbol, self.expiry, vlegs, _COMBO_VERTICAL,
                                           _OPEN_ACTION, qty, limit, self._td.new_dedup_tag())
            except OpenRejected as e:
                logger.error('垂直 combo 开仓被拒: %s，回滚已成交腿', e)
                self._unwind([l for g in filled for l in g], qty)
                return False, order_ids
            order_ids.append(oid)
            if self._await_fill(oid)['status'] == _FILLED:
                filled.append(vlegs)
            else:
                logger.error('垂直 combo 未成交 oid=%s，撤单 + 回滚', oid)
                self._cancel_quiet(oid)
                if self._poll_filled(oid):
                    filled.append(vlegs)
                self._unwind([l for g in filled for l in g], qty)
                return False, order_ids
        return True, order_ids

    def _cancel_quiet(self, oid):
        try:
            self._td.cancel_order(oid)
        except (CloseRejected, DataUnavailable) as e:  # 可能已成交/已撤
            logger.warning('撤单失败 oid=%s: %s', oid, e)

    def _poll_filled(self, oid):
        """复查某单是否（部分）成交（撤单后防竞态）。"""
        try:
            st = self._td.get_order_status(oid)
        except DataUnavailable:
            return False
        return st['status'] == _FILLED or (st.get('filled') or 0) > 0

    def _unwind(self, legs, qty):
        """逐腿反向市价拉平已成交腿（开仓BUY→SELL平、开仓SELL→BUY平）。减风险优先。"""
        for l in legs:
            pick = OptionPick(symbol=self.symbol, expiry=self.expiry or '',
                              strike=l['strike'], put_call=l['put_call'], identifier=l['identifier'])
            close_act = 'SELL' if str(l['side']).upper() == 'BUY' else 'BUY'
            try:
                self._td.flatten_leg(pick, close_act, qty, self._td.new_dedup_tag())
                logger.warning('回滚已成交腿 %s %s', close_act, l['identifier'])
            except (CloseRejected, OpenRejected) as e:
                logger.error('回滚腿失败 %s: %s —— 需人工核对持仓!', l['identifier'], e)

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
            return self._idle_interval
        if self.state in (BotState.IDLE, BotState.CLOSED):
            now = self._now_ms()
            if now - self._last_propose_ms >= self._propose_throttle_ms:
                self._last_propose_ms = now
                try:
                    self._try_propose()
                except Exception as e:  # noqa: BLE001 —— 提案失败不应杀线程
                    logger.warning('提案评估异常: %s', e)
        return self._idle_interval

    def _monitor_once(self):
        legdicts = [{'identifier': l.identifier, 'side': l.side,
                     'put_call': l.put_call, 'strike': l.strike} for l in self.legs]
        try:
            qbi = self._quotes_for(legdicts)
        except DataUnavailable as e:
            logger.warning('监控取腿行情失败: %s', e)
            return self._cfg.poll_interval
        close_cost = net_credit(legdicts, qbi, 'mid', closing=True)
        # 防脏点（与影子同护栏，实盘平仓路径）：开盘前后缺/零/陈旧报价会让 net_credit 退化成
        # ≤0 的假平仓成本（你卖出的信用价差在 DTE 还很大时不可能 0 成本买回）。喂给 trailing 会
        # 被误武装在假峰值并触发误平 → 视为不可信，跳过本 tick（不决策/不落库），下个 tick 重试。
        if close_cost is not None and close_cost < 0:   # 负成本不可能(垂直价差值∈[0,翼宽])→脏点；=0 为合法深度获利
            logger.warning('监控得到不可信平仓成本(%.4f<0)，跳过本 tick', close_cost)
            return self._cfg.poll_interval
        # 记录最近一次有效盯市值（看板卡片/状态透出，不影响判定）
        if close_cost is not None:
            self._last_close_cost = close_cost
            p = condor_pnl_percent(self.entry_credit, close_cost)
            self._last_pnl_pct = None if p is None else round(p, 1)
        # 落库：逐腿写持仓走势（自算正确符号盈亏，复用已取的 qbi，无额外券商调用）。
        for leg in self.legs:
            mid = _mid(qbi.get(leg.identifier))
            if mid is None:
                continue
            try:
                self._sink.on_position(self._td.account, self._leg_pick(leg), leg.side,
                                       leg.entry_price, self._leg_view(leg, mid))
            except Exception as e:  # noqa: BLE001
                logger.warning('on_position 落库失败 %s: %s', leg.identifier, e)
        dte = self._dte(self._expiry_date) if self._expiry_date else None
        ctx = StrategyContext(pnl_percent=condor_pnl_percent(self.entry_credit, close_cost),
                              minutes_to_close=None, dte=dte,
                              opened_at=self._opened_at, now_ts=self._now_ms())
        reason = self._strategy.decide(ctx)   # 可插拔：threshold/trailing(信用口径)，DTE 强平在基类
        if reason is not None:
            pnl = (self.entry_credit - close_cost) if close_cost is not None else None
            logger.warning('铁鹰触发出场 reason=%s 平仓成本/股≈%s pnl/股≈%s dte=%s',
                           reason.value, None if close_cost is None else round(close_cost, 2),
                           None if pnl is None else round(pnl, 2), dte)
            self._close_all(reason, qbi)
        self._persist()
        return self._cfg.poll_interval

    def _close_all(self, reason, qbi=None):
        """平仓：显式翻转每条腿 BUY/SELL 的反向 combo（镜像开仓，不依赖组合 action 翻转）。

        分组同开仓单类型：CUSTOM=单笔 4 腿；VERTICAL=两个垂直。净价取正=付债买回。
        任一未成交则不进 CLOSED，下个 tick 重试。
        """
        if not self.legs:
            self._mark_closed()
            return
        if qbi is None:
            try:
                qbi = self._quotes_for([{'identifier': l.identifier} for l in self.legs])
            except DataUnavailable as e:
                logger.error('平仓取行情失败，下个 tick 重试: %s', e)
                return
        if (self._cfg.condor_open_combo_type or 'CUSTOM').upper() == _COMBO_VERTICAL:
            groups = [[l for l in self.legs if l.put_call == 'PUT'],
                      [l for l in self.legs if l.put_call == 'CALL']]
            ctype = _COMBO_VERTICAL
        else:
            groups = [list(self.legs)]
            ctype = _COMBO_CUSTOM
        all_ok = True
        for grp in groups:
            open_ld = [{'identifier': l.identifier, 'side': l.side,
                        'put_call': l.put_call, 'strike': l.strike} for l in grp]
            close_ld = _reverse_legs(open_ld)                 # 翻转腿动作 = 减仓
            # marketable 平仓限价：保守平仓成本(买回卖腿付 ask/卖出买腿收 bid)，跨价差才成交
            val = net_credit(open_ld, qbi, 'conservative', closing=True)
            limit = round(abs(val), 2) if val is not None else None
            try:
                oid = self._td.place_combo(self.symbol, self.expiry, close_ld, ctype,
                                           _OPEN_ACTION, self.qty, limit,   # action=BUY + 正净价=付债买回
                                           self._td.new_dedup_tag())
                st = self._await_fill(oid)
                if st['status'] != _FILLED:
                    all_ok = False
                    logger.error('平仓 combo 未确认成交 order_id=%s，下个 tick 重试', oid)
            except (OpenRejected, CloseRejected) as e:
                all_ok = False
                logger.error('平仓 combo 被拒，将重试: %s', e)
        if all_ok:
            for l in self.legs:
                # 落库平仓成交（close 价取 mid 近似）+ 清活跃持仓。
                # 注意：sink.on_close 的 pnl_percent 按多头假设算，SELL 腿的历史$盈亏方向相反——
                # 看板「实时持仓」用的是 _monitor_once 自算的正确符号视图；历史表此项已知失真。
                try:
                    close_px = _mid(qbi.get(l.identifier))
                    self._sink.on_close(self._td.account, self._leg_pick(l), l.side, l.qty,
                                        close_px, reason, None, l.entry_price)
                except Exception as e:  # noqa: BLE001
                    logger.warning('on_close 落库失败 %s: %s', l.identifier, e)
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
        self.mid_credit = getattr(snap, 'mid_credit', 0.0) or None
        self._opened_at, self._tag = snap.opened_at, snap.external_id
        self.combo_order_ids = snap.combo_order_ids or []
        # 还原平仓策略运行态（trailing 的 armed/peak 跨重启不丢）；策略类型以当前配置为准
        self._strategy = build_condor_close_strategy(self._cfg)
        self._strategy.load_state(getattr(snap, 'strategy_state', {}) or {})
        # 与券商持仓逐腿对账：不一致则进 ERROR 待人工核对，不自动出场（防基于错误状态乱平）
        ok, mismatches = self._reconcile_legs()
        if not ok:
            self.state = BotState.ERROR
            logger.error('铁鹰恢复对账失败，进入 ERROR 待人工核对: %s', '; '.join(mismatches))
            self._persist()
            return True
        self.state = BotState.MONITORING
        logger.info('已恢复铁鹰 %s %s 张%s -> MONITORING', self.symbol, self.expiry, self.qty)
        return True

    def _reconcile_legs(self):
        """逐腿与券商持仓核对方向（BUY→应多头、SELL→应空头）。返回 (matched, mismatches)。

        取不到行情/持仓的腿跳过（不误判为不符）；仅在券商**确凿**无此腿或方向相反时判不符。
        """
        mismatches = []
        for l in self.legs:
            try:
                view = self._td.get_option_position(self._leg_pick(l))
            except DataUnavailable:
                continue
            qtyv = getattr(view, 'quantity', None) if view else None
            want_long = str(l.side).upper() == 'BUY'
            if not qtyv:
                mismatches.append(f'{l.identifier} 券商无持仓')
            elif (qtyv > 0) != want_long:
                mismatches.append(
                    f'{l.identifier} 方向不符(应{"多" if want_long else "空"},qty={qtyv})')
        return (not mismatches), mismatches

    def _snapshot(self):
        return CondorSnapshot(
            account=self._td.account, symbol=self.symbol, expiry=self.expiry or '',
            qty=self.qty, legs=[l.__dict__ for l in self.legs],
            entry_credit=self.entry_credit or 0.0, max_loss=self.max_loss or 0.0,
            state=self.state.value, opened_at=self._opened_at, external_id=self._tag,
            combo_order_ids=self.combo_order_ids, mid_credit=self.mid_credit or 0.0,
            strategy_name=getattr(self._strategy, 'name', 'threshold'),
            strategy_state=self._strategy.state())

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
            'mid_credit': self.mid_credit, 'max_loss': self.max_loss,
            'close_cost': self._last_close_cost, 'pnl_percent': self._last_pnl_pct,
            'dte': self._dte(self._expiry_date) if self._expiry_date else None,
            'strategy_state': self._strategy.state(),
            'iv': self._last_iv, 'ivp': self._last_ivp, 'ivr': self._last_ivr,
            'gate_mode': self._cfg.condor_iv_gate_mode,
            'iv_history_days': len(self._iv_store),
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
