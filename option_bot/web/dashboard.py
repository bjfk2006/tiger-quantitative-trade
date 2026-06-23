# -*- coding: utf-8 -*-
"""看板 server（设计增量 §6）：只读，HTTP Basic 认证，只读 SQLite。

不持有也不调用券商 SDK——数据全来自 SQLite。
"""
import datetime
import logging

import pytz
from flask import Flask, Response, jsonify, render_template, request

from option_bot.persistence.stats import (downsample, equity_curve,
                                           filter_by_close_ts, pair_round_trips,
                                           summarize)
from option_bot.web.auth import check_basic

logger = logging.getLogger('option_bot.web.dashboard')

_ET = pytz.timezone('America/New_York')


def _date_to_ms_range(from_str, to_str):
    """美东日界 -> [start_ms, end_ms)：from→当日00:00 ET，to→次日00:00 ET。"""
    start_ms = end_ms = None
    if from_str:
        d = datetime.datetime.strptime(from_str, '%Y-%m-%d')
        start_ms = int(_ET.localize(d).timestamp() * 1000)
    if to_str:
        d = datetime.datetime.strptime(to_str, '%Y-%m-%d') + datetime.timedelta(days=1)
        end_ms = int(_ET.localize(d).timestamp() * 1000)
    return start_ms, end_ms


def create_dashboard_app(repo, user, password, status_provider=None):
    app = Flask(__name__, template_folder='templates')

    @app.before_request
    def _auth():
        if request.path == '/healthz':
            return None  # 健康探针免认证
        if not check_basic(request.headers.get('Authorization'), user, password):
            return Response('unauthorized', 401,
                            {'WWW-Authenticate': 'Basic realm="option_bot"'})

    @app.route('/')
    def index():
        return render_template('dashboard.html')

    @app.route('/api/positions')
    def positions():
        try:
            return jsonify(repo.list_positions())
        except Exception as e:  # noqa: BLE001
            logger.error('读取持仓失败: %s', e)
            return jsonify({'error': str(e)}), 500

    @app.route('/api/trades')
    def trades():
        limit = request.args.get('limit', default=100, type=int)
        try:
            return jsonify(repo.list_trades(limit))
        except Exception as e:  # noqa: BLE001
            logger.error('读取交易记录失败: %s', e)
            return jsonify({'error': str(e)}), 500

    @app.route('/api/history/identifiers')
    def history_identifiers():
        account = request.args.get('account') or None
        try:
            start_ms, end_ms = _date_to_ms_range(request.args.get('from'),
                                                 request.args.get('to'))
            return jsonify(repo.distinct_identifiers_in_range(account, start_ms, end_ms))
        except ValueError:
            return jsonify({'error': 'invalid date (YYYY-MM-DD)'}), 400
        except Exception as e:  # noqa: BLE001
            logger.error('读取标识列表失败: %s', e)
            return jsonify({'error': str(e)}), 500

    @app.route('/api/history')
    def history():
        account = request.args.get('account') or None
        identifier = request.args.get('identifier') or None
        limit = request.args.get('limit', default=200, type=int)
        try:
            start_ms, end_ms = _date_to_ms_range(request.args.get('from'),
                                                 request.args.get('to'))
            # 配对需完整 OPEN，故不按 ts 过滤取数；配对后按 close_ts 过滤
            rows = repo.list_trades_in_range(account=account, identifier=identifier)
            rts = filter_by_close_ts(pair_round_trips(rows), start_ms, end_ms)
            rts.sort(key=lambda r: r.get('close_ts') or 0, reverse=True)
            return jsonify({
                'summary': summarize(rts),
                'trades': rts[:limit],
                'equity_curve': equity_curve(rts),
            })
        except ValueError:
            return jsonify({'error': 'invalid date (YYYY-MM-DD)'}), 400
        except Exception as e:  # noqa: BLE001
            logger.error('读取历史统计失败: %s', e)
            return jsonify({'error': str(e)}), 500

    @app.route('/api/ticks')
    def ticks():
        identifier = request.args.get('identifier') or None
        if not identifier:
            return jsonify({'error': 'identifier required'}), 400
        account = request.args.get('account') or None
        max_points = request.args.get('max_points', default=1000, type=int)
        try:
            start_ms, end_ms = _date_to_ms_range(request.args.get('from'),
                                                 request.args.get('to'))
            rows = repo.list_ticks_in_range(identifier, account, start_ms, end_ms)
            return jsonify({'identifier': identifier, 'count': len(rows),
                            'ticks': downsample(rows, max_points)})
        except ValueError:
            return jsonify({'error': 'invalid date (YYYY-MM-DD)'}), 400
        except Exception as e:  # noqa: BLE001
            logger.error('读取逐tick失败: %s', e)
            return jsonify({'error': str(e)}), 500

    @app.route('/healthz')
    def healthz():
        st = status_provider() if status_provider else {}
        return jsonify({'status': 'ok', 'bot_alive': bool(st.get('bot_alive', False))})

    return app
