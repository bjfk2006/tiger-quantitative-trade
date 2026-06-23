# -*- coding: utf-8 -*-
"""监控主循环（设计文档 §5 Flow 5.2/5.4 / §9 kill switch）。

每 tick：查持仓盈亏% + 距收盘分钟 -> RiskGuard 评估 -> 触发平仓。
连续数据失败达阈值触发 kill switch；临近收盘自动收紧轮询间隔。
"""
import logging
import time

from option_bot.adapters.errors import CloseRejected, DataUnavailable
from option_bot.domain.models import BotState
from option_bot.strategy.close_strategies import StrategyContext

logger = logging.getLogger('option_bot.loop')


class MonitorLoop:
    def __init__(self, state_machine, market_clock, config,
                 sleep=time.sleep, should_stop=None):
        self._sm = state_machine
        self._clock = market_clock
        self._cfg = config
        self._sleep = sleep
        self._should_stop = should_stop or (lambda: False)
        self._data_failures = 0

    def run(self):
        """阻塞运行直到平仓完成 / kill switch / 外部停止。"""
        logger.info('监控开始: tp=+%.1f%% sl=-%.1f%% close_buffer=%dmin',
                    self._cfg.tp_percent, self._cfg.sl_percent,
                    self._cfg.close_buffer_minutes)
        while self._sm.state == BotState.MONITORING and not self._should_stop():
            interval = self.run_once()
            if self._sm.state != BotState.MONITORING:
                break
            self._sleep(interval)
        logger.info('监控结束，最终状态: %s', self._sm.state.value)

    def run_once(self):
        """单次评估，返回下次睡眠间隔（秒）。异常不抛出，转为 kill switch 判定。

        供 service.Supervisor 在「排空命令 + 单 tick」外层循环中复用。
        """
        try:
            mtc = self._clock.minutes_to_close()
            pnl, pos = self._sm.current_pnl_percent()
            self._data_failures = 0
        except DataUnavailable as e:
            self._data_failures += 1
            logger.warning('数据拉取失败 (%d/%d): %s',
                           self._data_failures, self._cfg.max_data_failures, e)
            if self._data_failures >= self._cfg.max_data_failures:
                logger.critical('连续数据失败达上限，触发 kill switch，停止盯盘待人工接管')
                self._sm.state = BotState.ERROR
            return self._cfg.poll_interval

        if pnl is None and mtc is None:
            return self._next_interval(mtc)

        ctx = StrategyContext(pnl_percent=pnl, minutes_to_close=mtc,
                              market_price=(pos.market_price if pos else None),
                              entry_price=self._sm.entry_price,
                              now_ts=int(time.time() * 1000))
        reason = self._sm.decide_close(ctx)
        if reason is not None:
            pnl_disp = f'{pnl:.1f}%' if pnl is not None else 'n/a'
            logger.info('触发平仓 reason=%s pnl=%s 距收盘=%s min',
                        reason.value, pnl_disp,
                        f'{mtc:.1f}' if mtc is not None else 'n/a')
            try:
                self._sm.close(reason)
            except CloseRejected as e:
                logger.error('平仓失败将重试: %s', e)  # 下一 tick 重试（已退回 MONITORING）
        return self._next_interval(mtc)

    def _next_interval(self, mtc):
        """临近收盘窗口收紧轮询间隔，确保不漏触发时间强平。"""
        if mtc is not None and mtc <= (self._cfg.close_buffer_minutes + 1):
            return min(self._cfg.poll_interval, self._cfg.near_close_poll_interval)
        return self._cfg.poll_interval
