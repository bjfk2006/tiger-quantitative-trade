# -*- coding: utf-8 -*-
"""服务入口（设计增量 §3）：单进程起 3 线程——看板 / 操作 / bot 监督器。

env：
  凭证(SDK)  TIGEROPEN_TIGER_ID / TIGEROPEN_ACCOUNT / TIGEROPEN_PRIVATE_KEY ...
  看板       OBOT_WEB_USER / OBOT_WEB_PASSWORD（必填）/ OBOT_WEB_HOST(默认0.0.0.0) / OBOT_WEB_PORT(8000)
  操作面     OBOT_OPS_API_KEY（不填则不启动操作面）/ OBOT_OPS_PORT(8001) / OBOT_OPS_EXPOSE(默认false=仅本机)
  策略       OBOT_TP / OBOT_SL / OBOT_CLOSE_BUFFER / OBOT_POLL_INTERVAL / OBOT_MAX_QTY / OBOT_EARLY_CLOSE_FILE
  持久化     OBOT_DB_FILE(data/option_bot.db) / OBOT_STATE_FILE
  自动开仓   OBOT_OPEN_ON_START(默认false) + OBOT_SYMBOL/OBOT_DIRECTION/OBOT_EXPIRY/OBOT_STRIKE/OBOT_QTY
"""
import logging
import os
import threading

from option_bot.service import _b, build_bot_from_env, start_bot_thread
from option_bot.web.dashboard import create_dashboard_app
from option_bot.web.ops import create_ops_app

logger = logging.getLogger('option_bot.web.server')


def _run_flask(app, host, port):
    # 生产应前置反代 + gunicorn -w 1；自带 server 仅供个人/内网（见 runbook）
    app.run(host=host, port=port, threaded=True, use_reloader=False, debug=False)


def main(env=None):
    env = env if env is not None else os.environ
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(name)s %(levelname)s: %(message)s')
    g = env.get

    user, password = g('OBOT_WEB_USER'), g('OBOT_WEB_PASSWORD')
    if not user or not password:
        raise SystemExit('必须设置 OBOT_WEB_USER / OBOT_WEB_PASSWORD 才能启动看板'
                         '（避免无密码暴露公网）')

    supervisor, repo, cmd_queue = build_bot_from_env(g)
    start_bot_thread(supervisor)

    # 操作面：仅当配置了 apikey 才启动；默认仅绑本机
    api_key = g('OBOT_OPS_API_KEY')
    if api_key:
        ops_host = '0.0.0.0' if _b(g('OBOT_OPS_EXPOSE')) else '127.0.0.1'
        ops_port = int(g('OBOT_OPS_PORT') or 8001)
        ops_app = create_ops_app(cmd_queue, repo, api_key, supervisor.status)
        threading.Thread(target=_run_flask, args=(ops_app, ops_host, ops_port),
                         name='ops-server', daemon=True).start()
        logger.info('操作面已启动 http://%s:%s (apikey)', ops_host, ops_port)
    else:
        logger.warning('未设置 OBOT_OPS_API_KEY，操作面未启动')

    # 看板在主线程阻塞运行
    dash_host = g('OBOT_WEB_HOST') or '0.0.0.0'
    dash_port = int(g('OBOT_WEB_PORT') or 8000)
    dash_app = create_dashboard_app(repo, user, password, supervisor.status)
    logger.info('看板已启动 http://%s:%s (Basic auth)', dash_host, dash_port)
    _run_flask(dash_app, dash_host, dash_port)


if __name__ == '__main__':
    main()
