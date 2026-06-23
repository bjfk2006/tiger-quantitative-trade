# -*- coding: utf-8 -*-
"""服务监督器与命令队列（设计增量 §3/§10）。

操作面只往 CommandQueue 入队；Supervisor 在 bot 线程里排空并执行——
状态机始终单线程变更，既有「平仓幂等靠单线程」的正确性不变，无需加锁。
"""
import json
import logging
import os
import queue as _queue
import threading
import time

from option_bot.adapters.market_data import MarketDataAdapter
from option_bot.adapters.trading import TradingAdapter
from option_bot.config.loader import (load_client_config_from_env,
                                      load_strategy_config)
from option_bot.config.state_store import StateStore
from option_bot.domain.models import BotState, CloseReason, Direction
from option_bot.persistence.db import SqliteRepo
from option_bot.persistence.sink import SqliteSink
from option_bot.strategy.market_clock import MarketClock
from option_bot.strategy.monitor_loop import MonitorLoop
from option_bot.strategy.state_machine import PositionStateMachine

logger = logging.getLogger('option_bot.service')

# 操作面支持的命令（与 §6 ops 路由一一对应）
CMD_CLOSE = 'close'
CMD_DISABLE_OPEN = 'disable_open'
CMD_ENABLE_OPEN = 'enable_open'
CMD_STOP = 'stop'
VALID_COMMANDS = {CMD_CLOSE, CMD_DISABLE_OPEN, CMD_ENABLE_OPEN, CMD_STOP}


class CommandQueue:
    """线程安全命令队列：操作 server 入队，bot 线程排空。"""

    def __init__(self):
        self._q = _queue.Queue()

    def put(self, action):
        if action not in VALID_COMMANDS:
            raise ValueError(f'未知命令: {action}')
        self._q.put(action)

    def drain(self):
        items = []
        while True:
            try:
                items.append(self._q.get_nowait())
            except _queue.Empty:
                break
        return items

    def size(self):
        return self._q.qsize()


class Supervisor:
    """bot 线程主体：装配好的状态机 + 监控循环 + 命令排空。"""

    def __init__(self, sm, loop, config, command_queue, md=None, open_spec=None,
                 sleep=time.sleep):
        self._sm = sm
        self._loop = loop
        self._cfg = config
        self._queue = command_queue
        self._md = md
        self._open_spec = open_spec
        self._sleep = sleep
        self._stopped = False
        self.bot_alive = False

    def stop(self):
        self._stopped = True

    def run(self):
        """bot 线程入口。异常不外泄，只置 bot_alive=False（不拖垮看板）。"""
        self.bot_alive = True
        try:
            self._startup_position()
            while not self._stopped:
                self._drain_commands()
                if self._stopped:
                    break
                if self._sm.state == BotState.MONITORING:
                    interval = self._loop.run_once()
                else:
                    interval = self._cfg.poll_interval
                self._sleep(interval)
        except Exception as e:  # noqa: BLE001 —— 看门狗：任何异常都不应杀死 web
            logger.critical('监督器线程异常退出: %s', e, exc_info=True)
        finally:
            self.bot_alive = False
            logger.info('监督器结束，最终状态: %s', self._sm.state.value)

    def _startup_position(self):
        recovered = False
        try:
            recovered = self._sm.resume()
        except Exception as e:  # noqa: BLE001
            logger.warning('启动恢复持仓失败，稍后由监控重试: %s', e)
        if recovered:
            logger.info('已恢复遗留持仓，进入监控')
        elif self._open_spec and self._md is not None:
            self._do_open_on_start()

    def _do_open_on_start(self):
        spec = self._open_spec
        try:
            d = Direction(spec['direction'])
            pick = self._md.resolve_pick(spec['symbol'], spec['expiry'],
                                         spec['strike'], d)
            self._sm.open(pick, d, int(spec['qty']))
        except Exception as e:  # noqa: BLE001 —— 开仓失败不影响看板/服务存活
            logger.error('OPEN_ON_START 开仓失败(看板仍可用): %s', e)

    def _drain_commands(self):
        for cmd in self._queue.drain():
            try:
                self._handle(cmd)
            except Exception as e:  # noqa: BLE001
                logger.error('执行命令 %s 失败: %s', cmd, e)

    def _handle(self, cmd):
        if cmd == CMD_CLOSE:
            if self._sm.state == BotState.MONITORING:
                from option_bot.adapters.errors import CloseRejected
                try:
                    self._sm.close(CloseReason.MANUAL)
                except CloseRejected as e:
                    logger.error('手动平仓未完成将重试: %s', e)
            else:
                logger.info('收到 close 但当前无持仓(state=%s)，忽略', self._sm.state.value)
        elif cmd == CMD_DISABLE_OPEN:
            self._cfg.enable_open = False
            logger.warning('kill switch: 已停止开仓')
        elif cmd == CMD_ENABLE_OPEN:
            self._cfg.enable_open = True
            logger.info('已恢复开仓')
        elif cmd == CMD_STOP:
            self._stopped = True
            logger.warning('收到停止命令，结束盯盘')

    def status(self):
        return {
            'state': self._sm.state.value,
            'enable_open': self._cfg.enable_open,
            'bot_alive': self.bot_alive,
            'queue_size': self._queue.size(),
            'identifier': self._sm.pick.identifier if self._sm.pick else None,
            'entry_price': self._sm.entry_price,
        }


# ---------- env 解析 helpers ----------
def _f(v, default):
    try:
        return float(v) if v not in (None, '') else default
    except (TypeError, ValueError):
        return default


def _i(v, default):
    try:
        return int(v) if v not in (None, '') else default
    except (TypeError, ValueError):
        return default


def _b(v):
    return str(v).lower() in ('1', 'true', 'yes', 'on') if v is not None else False


def _load_json(path):
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def build_bot_from_env(env_get=os.environ.get):
    """从环境变量装配 bot（凭证走 SDK 的 TIGEROPEN_*；策略走 OBOT_*）。

    返回 (supervisor, repo, command_queue)。
    """
    from tigeropen.quote.quote_client import QuoteClient
    from tigeropen.trade.trade_client import TradeClient

    cfg = load_strategy_config(
        tp_percent=_f(env_get('OBOT_TP'), 30.0),
        sl_percent=_f(env_get('OBOT_SL'), 50.0),
        close_buffer_minutes=_i(env_get('OBOT_CLOSE_BUFFER'), 5),
        poll_interval=_f(env_get('OBOT_POLL_INTERVAL'), 2.0),
        max_qty=_i(env_get('OBOT_MAX_QTY'), 1),
        max_spread_pct=_f(env_get('OBOT_MAX_SPREAD'), 5.0),
        strategy_name=env_get('OBOT_STRATEGY') or 'threshold',
        trail_activation=_f(env_get('OBOT_TRAIL_ACTIVATION'), 20.0),
        trail_giveback=_f(env_get('OBOT_TRAIL_GIVEBACK'), 10.0),
        trail_relative_ratio=_f(env_get('OBOT_TRAIL_RELATIVE_RATIO'), 0.0),
        trail_relative_threshold=_f(env_get('OBOT_TRAIL_RELATIVE_THRESHOLD'), 50.0),
        breakeven_activation=_f(env_get('OBOT_BREAKEVEN_ACTIVATION'), 0.0),
        breakeven_lock=_f(env_get('OBOT_BREAKEVEN_LOCK'), 0.0),
        max_hold_minutes=_f(env_get('OBOT_MAX_HOLD_MINUTES'), 0.0),
        mode=env_get('OBOT_MODE') or 'single',
        leg_stop=_f(env_get('OBOT_STRADDLE_LEG_STOP'), 10.0),
        straddle_tp_mode=env_get('OBOT_STRADDLE_TP_MODE') or 'trailing',
        straddle_tp=_f(env_get('OBOT_STRADDLE_TP'), 10.0),
        straddle_trail_activation=_f(env_get('OBOT_STRADDLE_TRAIL_ACTIVATION'), 10.0),
        straddle_trail_giveback=_f(env_get('OBOT_STRADDLE_TRAIL_GIVEBACK'), 10.0),
        early_close_dates=_load_json(env_get('OBOT_EARLY_CLOSE_FILE')),
    )
    config = load_client_config_from_env(props_path=env_get('TIGEROPEN_PROPS_PATH'))
    account = config.account
    qc, tc = QuoteClient(config), TradeClient(config)
    md, td = MarketDataAdapter(qc), TradingAdapter(tc, account)

    repo = SqliteRepo(env_get('OBOT_DB_FILE') or 'data/option_bot.db')
    sink = SqliteSink(repo, tick_retention_days=_i(env_get('OBOT_TICK_RETENTION_DAYS'), 7))
    state_file = env_get('OBOT_STATE_FILE') or 'data/option_bot_state.json'
    cmd_queue = CommandQueue()

    # ---- 跨式(straddle)多腿模式 ----
    if (cfg.mode or 'single').lower() == 'straddle':
        from option_bot.strategy.straddle import StraddleManager, StraddleSupervisor
        straddle_state = state_file.rsplit('.json', 1)[0] + '_straddle.json'
        mgr = StraddleManager(td, md, cfg, MarketClock(md, cfg), straddle_state, sink=sink)
        s_open = None
        if _b(env_get('OBOT_OPEN_ON_START')):
            s_open = {'symbol': env_get('OBOT_SYMBOL'), 'expiry': env_get('OBOT_EXPIRY'),
                      'strike': _f(env_get('OBOT_STRIKE'), None), 'qty': _i(env_get('OBOT_QTY'), 1)}
        sup = StraddleSupervisor(mgr, cfg, cmd_queue, open_spec=s_open,
                                 allow_live_open=_b(env_get('OBOT_ALLOW_LIVE_AUTO_OPEN')),
                                 is_paper=config.is_paper)
        return sup, repo, cmd_queue

    # ---- 单腿模式（默认）----
    store = StateStore(state_file)
    sm = PositionStateMachine(td, md, store, cfg, sink=sink)
    loop = MonitorLoop(sm, MarketClock(md, cfg), cfg)

    open_spec = None
    if _b(env_get('OBOT_OPEN_ON_START')):
        # C3 安全闸：实盘账户(is_paper=False)默认禁止自动开仓，避免误下真实订单。
        # 确需实盘自动开仓必须显式设置 OBOT_ALLOW_LIVE_AUTO_OPEN=true。
        if not config.is_paper and not _b(env_get('OBOT_ALLOW_LIVE_AUTO_OPEN')):
            logger.critical(
                '实盘账户 %s (is_paper=False) 默认禁止自动开仓，已跳过 OPEN_ON_START。'
                '如确需实盘自动开仓，请显式设置 OBOT_ALLOW_LIVE_AUTO_OPEN=true。',
                account)
        else:
            open_spec = {
                'symbol': env_get('OBOT_SYMBOL'),
                'direction': env_get('OBOT_DIRECTION'),
                'expiry': env_get('OBOT_EXPIRY'),
                'strike': _f(env_get('OBOT_STRIKE'), None),
                'qty': _i(env_get('OBOT_QTY'), 1),
            }
    sup = Supervisor(sm, loop, cfg, cmd_queue, md=md, open_spec=open_spec)
    return sup, repo, cmd_queue


def start_bot_thread(supervisor):
    t = threading.Thread(target=supervisor.run, name='bot-supervisor', daemon=True)
    t.start()
    return t
