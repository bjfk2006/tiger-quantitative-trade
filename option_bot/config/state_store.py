# -*- coding: utf-8 -*-
"""本地 JSON 状态快照（崩溃恢复用）。

设计文档 §7：快照是辅助，真相源永远是券商侧 get_positions/get_order。
写入采用「先写临时文件再原子 rename」避免半写。
"""
import json
import logging
import os
import tempfile
from typing import Optional

from option_bot.domain.models import TradeSnapshot

logger = logging.getLogger('option_bot.state')

DEFAULT_PATH = 'option_bot_state.json'


class StateStore:
    def __init__(self, path: str = DEFAULT_PATH):
        self.path = path

    def save(self, snapshot: TradeSnapshot) -> None:
        data = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2)
        d = os.path.dirname(os.path.abspath(self.path))
        fd, tmp = tempfile.mkstemp(prefix='.obstate_', dir=d)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(data)
            os.replace(tmp, self.path)  # 原子替换
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def load(self) -> Optional[TradeSnapshot]:
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, 'r', encoding='utf-8') as f:
                return TradeSnapshot.from_dict(json.load(f))
        except Exception as e:
            logger.error('读取状态快照失败: %s', e, exc_info=True)
            return None

    def clear(self) -> None:
        if os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError as e:
                logger.warning('清除状态快照失败: %s', e)
