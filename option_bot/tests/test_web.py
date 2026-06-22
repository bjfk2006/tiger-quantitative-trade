# -*- coding: utf-8 -*-
"""看板 + 操作面 Web 单测（Flask test_client）。对应设计增量 §6/§8。"""
import base64
import unittest

from option_bot.service import CommandQueue
from option_bot.web.dashboard import create_dashboard_app
from option_bot.web.ops import create_ops_app


class FakeRepo:
    def __init__(self):
        self.positions = [{'identifier': 'AAPL  250815C00200000', 'direction': 'LONG',
                           'qty': 1, 'entry_price': 8.0, 'market_price': 9.0,
                           'unrealized_pnl': 100.0, 'unrealized_pnl_percent': 12.5,
                           'state': 'MONITORING'}]
        self.trades = [{'ts': 1000, 'action': 'OPEN', 'identifier': 'X', 'direction': 'LONG',
                        'qty': 1, 'price': 8.0, 'reason': 'OPEN', 'pnl_percent': None}]
        self.audits = []

    def list_positions(self):
        return self.positions

    def list_trades(self, limit=100):
        return self.trades[:limit]

    def insert_ops_audit(self, action, **kw):
        self.audits.append((action, kw))


def _basic(user, pw):
    raw = base64.b64encode(f'{user}:{pw}'.encode()).decode()
    return {'Authorization': 'Basic ' + raw}


class TestDashboard(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        app = create_dashboard_app(self.repo, 'admin', 'secret',
                                   status_provider=lambda: {'bot_alive': True})
        app.config['TESTING'] = True
        self.c = app.test_client()

    def test_positions_requires_auth(self):
        self.assertEqual(self.c.get('/api/positions').status_code, 401)

    def test_positions_with_auth(self):
        r = self.c.get('/api/positions', headers=_basic('admin', 'secret'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()[0]['unrealized_pnl_percent'], 12.5)

    def test_wrong_password_rejected(self):
        self.assertEqual(
            self.c.get('/api/positions', headers=_basic('admin', 'nope')).status_code, 401)

    def test_trades_with_auth(self):
        r = self.c.get('/api/trades', headers=_basic('admin', 'secret'))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()), 1)

    def test_healthz_no_auth(self):
        r = self.c.get('/healthz')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['bot_alive'])


class TestOps(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepo()
        self.q = CommandQueue()
        app = create_ops_app(self.q, self.repo, 'topsecret',
                             status_provider=lambda: {'state': 'MONITORING'})
        app.config['TESTING'] = True
        self.c = app.test_client()

    def test_close_requires_apikey(self):
        self.assertEqual(self.c.post('/ops/close').status_code, 401)

    def test_close_with_apikey_enqueues(self):
        r = self.c.post('/ops/close', headers={'X-API-Key': 'topsecret'})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.get_json()['queued'], 'close')
        self.assertEqual(self.q.drain(), ['close'])
        self.assertEqual(self.repo.audits[0][0], 'close')

    def test_wrong_apikey_rejected(self):
        self.assertEqual(
            self.c.post('/ops/close', headers={'X-API-Key': 'bad'}).status_code, 401)

    def test_bearer_token_accepted(self):
        r = self.c.post('/ops/stop', headers={'Authorization': 'Bearer topsecret'})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(self.q.drain(), ['stop'])

    def test_status_with_apikey(self):
        r = self.c.get('/ops/status', headers={'X-API-Key': 'topsecret'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['state'], 'MONITORING')


if __name__ == '__main__':
    unittest.main()
