# -*- coding: utf-8 -*-
"""option_bot CLI 入口（设计文档 §3 CliApp / §4 Presentation 层）。

命令：
  chain  查看某美股标的的期权到期日与期权链（做多看 CALL，做空看 PUT）。
  run    选定单腿期权 -> 市价开仓 -> 轮询盈亏% -> 止盈/止损/收盘前强平 自动平仓。

凭证经 SDK 三级加载（参数 > 环境变量 TIGEROPEN_* > 配置文件）。首期跑模拟盘：
--account 传入 paper account 即可。
"""
import json
import logging
import signal
import sys

import click

from option_bot.adapters.errors import OptionBotError
from option_bot.adapters.market_data import MarketDataAdapter
from option_bot.adapters.trading import TradingAdapter
from option_bot.config.loader import load_client_config, load_strategy_config
from option_bot.config.state_store import StateStore, DEFAULT_PATH
from option_bot.domain.models import Direction
from option_bot.strategy.market_clock import MarketClock
from option_bot.strategy.monitor_loop import MonitorLoop
from option_bot.strategy.state_machine import PositionStateMachine


def _setup_logging(verbose):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s %(name)s %(levelname)s: %(message)s',
    )


def _build_clients(ctx):
    from tigeropen.quote.quote_client import QuoteClient
    from tigeropen.trade.trade_client import TradeClient
    config = load_client_config(
        private_key_path=ctx['private_key'],
        tiger_id=ctx['tiger_id'],
        account=ctx['account'],
        props_path=ctx['props_path'],
    )
    return config, QuoteClient(config), TradeClient(config)


@click.group()
@click.option('--private-key', 'private_key', default=None, help='私钥文件路径')
@click.option('--tiger-id', 'tiger_id', default=None, help='开发者应用 id')
@click.option('--account', default=None, help='授权账户（模拟盘传 paper account）')
@click.option('--props-path', 'props_path', default=None, help='SDK 配置文件路径')
@click.option('-v', '--verbose', is_flag=True, help='DEBUG 日志')
@click.pass_context
def cli(ctx, private_key, tiger_id, account, props_path, verbose):
    _setup_logging(verbose)
    ctx.obj = {
        'private_key': private_key, 'tiger_id': tiger_id,
        'account': account, 'props_path': props_path,
    }


@cli.command('chain')
@click.argument('symbol')
@click.option('--expiry', default=None, help='到期日(YYYY-MM-DD)。不填则只列到期日')
@click.option('--direction', type=click.Choice(['LONG', 'SHORT']), default=None,
              help='LONG 看 CALL / SHORT 看 PUT；不填则两者都列')
@click.pass_context
def chain_cmd(ctx, symbol, expiry, direction):
    """查看美股期权链。"""
    try:
        _config, qc, _tc = _build_clients(ctx.obj)
        md = MarketDataAdapter(qc)
        if not expiry:
            exps = md.list_expirations(symbol)
            click.echo(f'{symbol} 可选到期日：')
            for e in exps:
                click.echo(f"  {e.get('date')}  ({e.get('period_tag', '')})")
            click.echo('\n用 --expiry YYYY-MM-DD 查看具体期权链。')
            return
        put_call = Direction(direction).put_call if direction else None
        rows = md.get_chain(symbol, expiry, put_call=put_call)
        click.echo(f'{symbol} {expiry} 期权链（{put_call or "CALL+PUT"}）：')
        click.echo(f"{'identifier':<22}{'strike':>10}{'put_call':>9}"
                   f"{'bid':>9}{'ask':>9}{'last':>9}{'OI':>9}")
        for r in rows:
            click.echo(
                f"{str(r.get('identifier', '')):<22}{r.get('strike', ''):>10}"
                f"{str(r.get('put_call', '')):>9}{r.get('bid_price', ''):>9}"
                f"{r.get('ask_price', ''):>9}{r.get('latest_price', ''):>9}"
                f"{r.get('open_interest', ''):>9}")
    except OptionBotError as e:
        click.echo(f'错误: {e}', err=True)
        sys.exit(1)


@cli.command('run')
@click.argument('symbol')
@click.option('--direction', type=click.Choice(['LONG', 'SHORT']), required=True,
              help='LONG=买入 CALL（做多）/ SHORT=买入 PUT（做空）')
@click.option('--expiry', required=True, help='到期日 YYYY-MM-DD')
@click.option('--strike', required=True, type=float, help='行权价')
@click.option('--qty', default=1, type=int, help='合约数量（受 max-qty 限制）')
@click.option('--tp', 'tp_percent', default=30.0, type=float, help='止盈阈值 +%')
@click.option('--sl', 'sl_percent', default=50.0, type=float, help='止损阈值 -%（正数）')
@click.option('--close-buffer', 'close_buffer_minutes', default=5, type=int,
              help='收盘前 N 分钟强平')
@click.option('--poll-interval', default=2.0, type=float, help='监控轮询间隔(秒)')
@click.option('--max-qty', default=1, type=int, help='单笔数量上限')
@click.option('--max-spread', 'max_spread_pct', default=5.0, type=float,
              help='市价单允许的最大相对点差%(超过则拒单防滑点；流动性差/盘前可调大)')
@click.option('--strategy', 'strategy_name',
              type=click.Choice(['threshold', 'trailing', 'breakeven', 'time_in_trade', 'bracket']),
              default='threshold',
              help='平仓策略：threshold/trailing/breakeven/time_in_trade/bracket(可组合)')
@click.option('--trail-activation', default=20.0, type=float,
              help='trailing/bracket 移动止盈武装阈值%')
@click.option('--trail-giveback', default=10.0, type=float,
              help='trailing/bracket 从峰值回撤多少个点即平仓锁盈')
@click.option('--breakeven-activation', default=0.0, type=float,
              help='breakeven/bracket 保本武装阈值%(bracket 中 0=关闭)')
@click.option('--breakeven-lock', default=0.0, type=float,
              help='保本平仓线%(0=成本价)')
@click.option('--max-hold-minutes', default=0.0, type=float,
              help='持仓时长上限(分钟)；bracket 中 0=关闭')
@click.option('--enable-open/--no-enable-open', 'enable_open', default=True,
              help='是否允许开新仓（kill switch：--no-enable-open 只盯盘/平仓不开仓）')
@click.option('--early-close-file', default=None,
              help='半日市日期表 JSON: {"2025-11-28":"13:00"}（SDK 不提供，本地配置）')
@click.option('--state-file', default=DEFAULT_PATH, help='状态快照文件路径')
@click.option('--db-file', default=None, help='SQLite 持久化文件（落交易/持仓记录，供看板读取）')
@click.option('--yes', is_flag=True, help='跳过下单前确认')
@click.pass_context
def run_cmd(ctx, symbol, direction, expiry, strike, qty, tp_percent, sl_percent,
            close_buffer_minutes, poll_interval, max_qty, max_spread_pct,
            strategy_name, trail_activation, trail_giveback,
            breakeven_activation, breakeven_lock, max_hold_minutes, enable_open,
            early_close_file, state_file, db_file, yes):
    """开仓并自动盯盘平仓。"""
    log = logging.getLogger('option_bot.cli')
    try:
        early_close = {}
        if early_close_file:
            with open(early_close_file, 'r', encoding='utf-8') as f:
                early_close = json.load(f)

        cfg = load_strategy_config(
            tp_percent=tp_percent, sl_percent=sl_percent,
            close_buffer_minutes=close_buffer_minutes, poll_interval=poll_interval,
            max_qty=max_qty, max_spread_pct=max_spread_pct,
            strategy_name=strategy_name, trail_activation=trail_activation,
            trail_giveback=trail_giveback, breakeven_activation=breakeven_activation,
            breakeven_lock=breakeven_lock, max_hold_minutes=max_hold_minutes,
            enable_open=enable_open, early_close_dates=early_close,
        )

        _config, qc, tc = _build_clients(ctx.obj)
        account = ctx.obj['account'] or getattr(_config, 'account', None)
        md = MarketDataAdapter(qc)
        td = TradingAdapter(tc, account)
        store = StateStore(state_file)
        sink = None
        if db_file:
            from option_bot.persistence.db import SqliteRepo
            from option_bot.persistence.sink import SqliteSink
            sink = SqliteSink(SqliteRepo(db_file))
        sm = PositionStateMachine(td, md, store, cfg, sink=sink)
        clock = MarketClock(md, cfg)

        # 优雅停机
        stop = {'flag': False}
        def _handler(signum, frame):
            log.warning('收到信号 %s，准备停止盯盘（不自动平仓，请人工确认现有持仓）', signum)
            stop['flag'] = True
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

        # 崩溃恢复：若有遗留持仓，跳过开仓直接盯盘
        if sm.resume():
            log.info('检测到遗留持仓，直接进入监控')
        elif not enable_open:
            click.echo('开仓开关已关闭(--no-enable-open)且无遗留持仓，无需盯盘，退出。')
            return
        else:
            dir_enum = Direction(direction)
            pick = md.resolve_pick(symbol, expiry, strike, dir_enum)
            click.echo(f'将市价开仓: {dir_enum.value} {pick.identifier} x{qty} '
                       f'(止盈+{tp_percent}% / 止损-{sl_percent}% / 收盘前{close_buffer_minutes}min强平)')
            if not yes and not click.confirm('确认下单?'):
                click.echo('已取消。')
                return
            sm.open(pick, dir_enum, qty)

        loop = MonitorLoop(sm, clock, cfg, should_stop=lambda: stop['flag'])
        loop.run()
        click.echo(f'结束，最终状态: {sm.state.value}')
    except OptionBotError as e:
        click.echo(f'错误: {e}', err=True)
        sys.exit(1)


def main():
    cli()


if __name__ == '__main__':
    main()
