# -*- coding: utf-8 -*-
"""SQLite 仓储（设计增量 §5）。stdlib sqlite3 + WAL，零额外依赖。

并发模型：单写者(bot 线程) + 多读者(看板线程)；每次操作开独立连接并
**显式关闭**（contextlib.closing）——避免连接/文件句柄泄漏。WAL 下读写不互斥。
真相源仍是券商侧，本库为展示/审计副本。
"""
import logging
import sqlite3
import time
from contextlib import closing

logger = logging.getLogger('option_bot.db')

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    account     TEXT    NOT NULL,
    identifier  TEXT    NOT NULL,
    symbol      TEXT    NOT NULL,
    direction   TEXT    NOT NULL,
    action      TEXT    NOT NULL,
    qty         REAL    NOT NULL,
    price       REAL,
    reason      TEXT,
    order_id    INTEGER,
    pnl_percent REAL
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

CREATE TABLE IF NOT EXISTS positions (
    identifier   TEXT PRIMARY KEY,
    account      TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    direction    TEXT NOT NULL,
    qty          REAL,
    entry_price  REAL,
    market_price REAL,
    unrealized_pnl         REAL,
    unrealized_pnl_percent REAL,
    state        TEXT,
    updated_ts   INTEGER
);

CREATE TABLE IF NOT EXISTS ops_audit (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    action    TEXT    NOT NULL,
    source_ip TEXT,
    key_id    TEXT,
    result    TEXT
);
"""


def _now_ms():
    return int(time.time() * 1000)


class SqliteRepo:
    def __init__(self, path):
        self.path = path
        self.init_schema()

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def init_schema(self):
        # closing() 确保关闭；内层 `with conn` 提交事务
        with closing(self._conn()) as conn, conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.executescript(_SCHEMA)

    # ---------- trades ----------
    def insert_trade(self, account, identifier, symbol, direction, action, qty,
                     price=None, reason=None, order_id=None, pnl_percent=None, ts=None):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                'INSERT INTO trades(ts,account,identifier,symbol,direction,action,'
                'qty,price,reason,order_id,pnl_percent) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (ts or _now_ms(), account, identifier, symbol, direction, action,
                 qty, price, reason, order_id, pnl_percent))

    def list_trades(self, limit=100):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                'SELECT * FROM trades ORDER BY ts DESC, id DESC LIMIT ?',
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- positions ----------
    def upsert_position(self, identifier, account, symbol, direction, qty=None,
                        entry_price=None, market_price=None, unrealized_pnl=None,
                        unrealized_pnl_percent=None, state=None, updated_ts=None):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                'INSERT INTO positions(identifier,account,symbol,direction,qty,'
                'entry_price,market_price,unrealized_pnl,unrealized_pnl_percent,'
                'state,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?) '
                'ON CONFLICT(identifier) DO UPDATE SET '
                'qty=excluded.qty, entry_price=excluded.entry_price, '
                'market_price=excluded.market_price, '
                'unrealized_pnl=excluded.unrealized_pnl, '
                'unrealized_pnl_percent=excluded.unrealized_pnl_percent, '
                'state=excluded.state, updated_ts=excluded.updated_ts',
                (identifier, account, symbol, direction, qty, entry_price,
                 market_price, unrealized_pnl, unrealized_pnl_percent, state,
                 updated_ts or _now_ms()))

    def delete_position(self, identifier):
        with closing(self._conn()) as conn, conn:
            conn.execute('DELETE FROM positions WHERE identifier=?', (identifier,))

    def list_positions(self):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                'SELECT * FROM positions ORDER BY updated_ts DESC').fetchall()
        return [dict(r) for r in rows]

    # ---------- ops audit ----------
    def insert_ops_audit(self, action, source_ip=None, key_id=None, result='queued', ts=None):
        with closing(self._conn()) as conn, conn:
            conn.execute(
                'INSERT INTO ops_audit(ts,action,source_ip,key_id,result) '
                'VALUES(?,?,?,?,?)',
                (ts or _now_ms(), action, source_ip, key_id, result))

    def list_ops_audit(self, limit=100):
        with closing(self._conn()) as conn:
            rows = conn.execute(
                'SELECT * FROM ops_audit ORDER BY ts DESC, id DESC LIMIT ?',
                (limit,)).fetchall()
        return [dict(r) for r in rows]
