# -*- coding: utf-8 -*-
"""看板 server（设计增量 §6）：只读，HTTP Basic 认证，只读 SQLite。

不持有也不调用券商 SDK——数据全来自 SQLite。
"""
import logging

from flask import Flask, Response, jsonify, render_template, request

from option_bot.web.auth import check_basic

logger = logging.getLogger('option_bot.web.dashboard')


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

    @app.route('/healthz')
    def healthz():
        st = status_provider() if status_provider else {}
        return jsonify({'status': 'ok', 'bot_alive': bool(st.get('bot_alive', False))})

    return app
