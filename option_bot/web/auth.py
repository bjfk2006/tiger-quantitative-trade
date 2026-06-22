# -*- coding: utf-8 -*-
"""认证工具（设计增量 §8）：看板用 HTTP Basic，操作面用 apikey。

凭证比较一律用 hmac.compare_digest 常量时间，防计时侧信道。
"""
import base64
import hmac


def check_basic(auth_header, user, password):
    """校验 HTTP Basic。看板要求 user/password 均已配置。"""
    if not user or not password:
        return False
    if not auth_header or not auth_header.startswith('Basic '):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode('utf-8')
    except Exception:
        return False
    u, sep, p = decoded.partition(':')
    if not sep:
        return False
    return hmac.compare_digest(u, user) and hmac.compare_digest(p, password)


def extract_apikey(headers):
    """从 X-API-Key 或 Authorization: Bearer 取 apikey。"""
    key = headers.get('X-API-Key')
    if key:
        return key
    auth = headers.get('Authorization', '')
    if auth.startswith('Bearer '):
        return auth[7:]
    return None


def check_apikey(provided, expected):
    if not provided or not expected:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def mask_key(key):
    """审计用：只记前 4 位，不存全量。"""
    if not key:
        return None
    return key[:4] + '***'
