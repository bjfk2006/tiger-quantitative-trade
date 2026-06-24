# -*- coding: utf-8 -*-
"""从本地 dolt 仓库读单合约期权日线（subprocess + JSON），无第三方依赖。

默认仓库 /data1/dolt/options（post-no-preference/options）。在宿主机运行（容器无 dolt）。
"""
import json
import os
import subprocess

DEFAULT_REPO = '/data1/dolt/options'


class DoltError(RuntimeError):
    pass


def _norm_put_call(pc: str) -> str:
    s = (pc or '').strip().lower()
    if s in ('call', 'c', 'long'):
        return 'Call'
    if s in ('put', 'p', 'short'):
        return 'Put'
    raise DoltError(f'非法 put_call: {pc!r}（用 Call/Put）')


def _safe(v: str, field: str) -> str:
    """拒绝含引号/分号的取值，避免拼 SQL 出意外（本地可信工具，做最小防护）。"""
    if v is None or any(c in str(v) for c in ("'", '"', ';', '\n')):
        raise DoltError(f'非法 {field}: {v!r}')
    return str(v)


def load_option_series(symbol, expiration, strike, put_call,
                       from_date, to_date, repo=DEFAULT_REPO):
    """返回 [{date,bid,ask}]（按 date 升序，已过滤缺 bid/ask）。"""
    sym = _safe(symbol, 'symbol').upper()
    exp = _safe(expiration, 'expiration')
    frm = _safe(from_date, 'from')
    to = _safe(to_date, 'to')
    pc = _norm_put_call(put_call)
    strike = float(strike)
    sql = (
        "select date, bid, ask from option_chain "
        f"where act_symbol='{sym}' and expiration='{exp}' and strike={strike} "
        f"and call_put='{pc}' and date between '{frm}' and '{to}' order by date"
    )
    rows = _run_json(sql, repo)
    out = []
    for r in rows:
        bid, ask = r.get('bid'), r.get('ask')
        if bid is None or ask is None:
            continue
        out.append({'date': str(r['date'])[:10], 'bid': float(bid), 'ask': float(ask)})
    return out


def _run_json(sql, repo):
    if not os.path.isdir(os.path.join(repo, '.dolt')):
        raise DoltError(f'{repo} 不是 dolt 仓库（缺 .dolt）')
    env = dict(os.environ)
    env.setdefault('HOME', '/root')  # dolt 需 HOME 读配置
    try:
        p = subprocess.run(['dolt', 'sql', '-q', sql, '--result-format', 'json'],
                           cwd=repo, env=env, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        raise DoltError('未找到 dolt 命令（请在装了 dolt 的宿主机运行）')
    except subprocess.TimeoutExpired:
        raise DoltError('dolt 查询超时（>180s）；缩小日期范围或确认走索引')
    if p.returncode != 0:
        raise DoltError(f'dolt 查询失败: {p.stderr.strip() or p.stdout.strip()}')
    try:
        return json.loads(p.stdout or '{}').get('rows', [])
    except json.JSONDecodeError as e:
        raise DoltError(f'解析 dolt JSON 失败: {e}; 原始输出前200字: {p.stdout[:200]!r}')
