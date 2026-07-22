"""
短时记忆 - Redis LIST + TTL
不入库，读写快；session_id 独立 key；LTRIM 控最大轮数
"""
import json
from loguru import logger
from typing import List, Dict

from django.core.cache import cache
from django.conf import settings
from django_redis import get_redis_connection



class ShortTermMemory:
    """基于 Redis LIST 的短时对话记忆"""

    def __init__(self):
        self.ttl = settings.SHORT_TERM_TTL
        self.max_turns = settings.SHORT_TERM_MAX_TURNS

    def _key(self, session_id: int) -> str:
        return f'short_term:sess:{session_id}'

    def _client(self):
        try:
            return get_redis_connection('default')
        except Exception:
            return None

    def get_turns(self, session_id: int) -> List[Dict]:
        """获取所有轮次（最近的在尾部）"""
        r = self._client()
        if not r:
            return []
        try:
            raws = r.lrange(self._key(session_id), 0, -1)
            return [json.loads(x) for x in raws]
        except Exception as e:
            logger.warning('[ShortTerm] get failed: %s', e)
            return []

    def append_turn(self, session_id: int, question: str, answer: str):
        r = self._client()
        if not r:
            return
        try:
            key = self._key(session_id)
            r.rpush(key, json.dumps({'question': question, 'answer': answer}, ensure_ascii=False))
            r.ltrim(key, -self.max_turns, -1)
            r.expire(key, self.ttl)
        except Exception as e:
            logger.warning('[ShortTerm] append failed: %s', e)

    def clear(self, session_id: int):
        r = self._client()
        if r:
            try:
                r.delete(self._key(session_id))
            except Exception:
                pass
