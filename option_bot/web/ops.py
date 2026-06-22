# -*- coding: utf-8 -*-
"""操作 server（设计增量 §6/§8）：apikey 认证，能力=平仓+开关，写=入队。

不直接动状态机：所有命令入 CommandQueue，由 bot 线程排空执行（§10 并发）。
每条命令落 ops_audit。无远程开仓。
"""
import logging

from flask import Flask, jsonify, request

from option_bot.service import (CMD_CLOSE, CMD_DISABLE_OPEN, CMD_ENABLE_OPEN,
                                CMD_STOP)
from option_bot.web.auth import check_apikey, extract_apikey, mask_key

logger = logging.getLogger('option_bot.web.ops')


def create_ops_app(command_queue, repo, api_key, status_provider=None):
    app = Flask(__name__)

    @app.before_request
    def _auth():
        if not check_apikey(extract_apikey(request.headers), api_key):
            return jsonify({'error': 'unauthorized'}), 401

    def _enqueue(action):
        command_queue.put(action)
        try:
            repo.insert_ops_audit(action, source_ip=request.remote_addr,
                                  key_id=mask_key(api_key), result='queued')
        except Exception as e:  # noqa: BLE001
            logger.warning('写操作审计失败: %s', e)
        logger.info('操作入队 action=%s from=%s', action, request.remote_addr)
        return jsonify({'queued': action}), 202

    @app.route('/ops/status')
    def status():
        return jsonify(status_provider() if status_provider else {})

    @app.route('/ops/close', methods=['POST'])
    def close():
        return _enqueue(CMD_CLOSE)

    @app.route('/ops/disable-open', methods=['POST'])
    def disable_open():
        return _enqueue(CMD_DISABLE_OPEN)

    @app.route('/ops/enable-open', methods=['POST'])
    def enable_open():
        return _enqueue(CMD_ENABLE_OPEN)

    @app.route('/ops/stop', methods=['POST'])
    def stop():
        return _enqueue(CMD_STOP)

    return app
