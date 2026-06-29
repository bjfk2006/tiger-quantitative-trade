# -*- coding: utf-8 -*-
"""活 IV 历史存储 + IV 分位/IV-Rank 纯函数。

设计：docs/design/2026-06-29-condor-iv-rank-entry-gate.md
引擎/影子每交易日采一个活 ATM IV 样本，滚动保留近 L 日；入场闸用 IV Percentile 判"相对贵"。
纯标准库，原子写，dedup-by-date（同日幂等，真样本(live)优先于种子(seed)）。
"""
import datetime as _dt
import json
import logging
import os
import tempfile

logger = logging.getLogger('option_bot.iv_history')


def iv_percentile(history, iv_now):
    """IV 分位(0–100)：历史中严格小于 iv_now 的占比。空/None → None。"""
    if not history or iv_now is None:
        return None
    below = sum(1 for x in history if x < iv_now)
    return below / len(history) * 100.0


def iv_rank(history, iv_now):
    """IV-Rank(0–100)：(iv_now−min)/(max−min)。空/None/max==min → None。"""
    if not history or iv_now is None:
        return None
    lo, hi = min(history), max(history)
    if hi <= lo:
        return None
    return (iv_now - lo) / (hi - lo) * 100.0


class IVHistoryStore:
    """每交易日一条活 IV 历史，滚动保留近 lookback 条。"""

    def __init__(self, path, lookback_days=252):
        self._path = path
        self._lookback = max(1, int(lookback_days))

    def _load_raw(self):
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:  # noqa: BLE001 —— 坏文件不应中断交易
            logger.warning('读 IV 历史失败(%s): %s', self._path, e)
            return []

    def _write_raw(self, entries):
        entries = sorted(entries, key=lambda e: e.get('date', ''))[-self._lookback:]
        d = os.path.dirname(os.path.abspath(self._path)) or '.'
        try:
            os.makedirs(d, exist_ok=True)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(prefix='.ivh_', dir=d)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(entries, f, ensure_ascii=False)
            os.replace(tmp, self._path)
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def values(self):
        """近 lookback 的 iv 浮点列表（供分位/rank 用）。"""
        out = []
        for e in self._load_raw()[-self._lookback:]:
            try:
                v = float(e['iv'])
                if v == v:           # 排除 NaN
                    out.append(v)
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def __len__(self):
        return len(self.values())

    def append_daily(self, date_str, iv, src='live'):
        """写入当日样本。同日已有真(live)样本则 no-op（每日一写，省 IO）。返回是否写入。

        真样本会覆盖同日种子(seed)；并发同日写经原子替换幂等（值近似、最后写胜）。
        """
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            return False
        if iv != iv or iv <= 0:       # NaN / 非正
            return False
        entries = self._load_raw()
        by_date = {e.get('date'): e for e in entries if e.get('date')}
        cur = by_date.get(date_str)
        if cur is not None and cur.get('src') == 'live' and src == 'live':
            return False              # 今天已采过真样本
        by_date[date_str] = {'date': date_str, 'iv': iv, 'src': src}
        self._write_raw(list(by_date.values()))
        return True

    def seed_from_vix(self, vix_csv_path, gap, today_str, only_if_empty=True):
        """用 VIX 历史(close−gap 近似 ATM IV)回填近 lookback 日的 src='seed' 历史。

        仅回填库中尚无的日期，真样本不被覆盖。only_if_empty=True 时若已有任何 live 样本则不种。
        返回回填条数。VIX 单位为 vol 点(如 18.4)，iv=(close−gap)/100 存为小数。
        """
        entries = self._load_raw()
        if only_if_empty and any(e.get('src') == 'live' for e in entries):
            return 0
        try:
            from option_bot.backtest.iv_gate_freq import load as _load_vix
            rows = _load_vix(vix_csv_path)        # [(date, high, close)]
        except FileNotFoundError:
            logger.warning('IV-Rank 种子：找不到 VIX CSV %s，跳过', vix_csv_path)
            return 0
        except Exception as e:  # noqa: BLE001
            logger.warning('IV-Rank 种子：读 VIX 失败: %s', e)
            return 0
        try:
            today = _dt.datetime.strptime(today_str, '%Y-%m-%d').date()
        except (ValueError, TypeError):
            return 0
        have = {e.get('date') for e in entries}
        seeded = 0
        for d, _h, c in rows[-self._lookback:]:
            if d > today:
                continue
            ds = d.strftime('%Y-%m-%d')
            iv = (c - gap) / 100.0
            if ds in have or iv <= 0:
                continue
            entries.append({'date': ds, 'iv': iv, 'src': 'seed'})
            seeded += 1
        if seeded:
            self._write_raw(entries)
        return seeded
