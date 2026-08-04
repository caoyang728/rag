"""
短时记忆 - Redis LIST + TTL
不入库，读写快；session_id 独立 key；LTRIM 控最大轮数
"""
import json
from loguru import logger
from typing import List, Dict

from django.conf import settings
from django_redis import get_redis_connection


class ShortTermMemory:
    """基于 Redis LIST 的短时对话记忆"""

    def __init__(self):
        self.ttl = settings.SHORT_TERM_TTL
        self.max_turns = settings.SHORT_TERM_MAX_TURNS
        self._client = None

    def _key(self, session_id: int) -> str:
        return f'short_term:sess:{session_id}'

    def _get_client(self):
        """缓存 Redis 连接，失败时记录告警"""
        if self._client is not None:
            return self._client
        try:
            self._client = get_redis_connection('default')
            return self._client
        except Exception as e:
            logger.error(f'[ShortTerm] Redis connection failed: {e}')
            return None

    def get_turns(self, session_id: int) -> List[Dict]:
        """获取所有轮次（最近的在尾部）"""
        r = self._get_client()
        if not r:
            return []
        try:
            raws = r.lrange(self._key(session_id), 0, -1)
            return [json.loads(x) for x in raws]
        except Exception as e:
            logger.warning(f'[ShortTerm] get failed: {e}')
            return []

    def append_turn(self, session_id: int, question: str, answer: str):
        """使用 pipeline 合并 rpush + ltrim + expire"""
        r = self._get_client()
        if not r:
            return
        try:
            key = self._key(session_id)
            pipe = r.pipeline()
            pipe.rpush(key, json.dumps({'question': question, 'answer': answer}, ensure_ascii=False))
            pipe.ltrim(key, -self.max_turns, -1)
            pipe.expire(key, self.ttl)
            pipe.execute()
        except Exception as e:
            logger.warning(f'[ShortTerm] append failed: {e}')

    def clear(self, session_id: int):
        r = self._get_client()
        if r:
            try:
                r.delete(self._key(session_id))
            except Exception as e:
                logger.warning(f'[ShortTerm] clear failed: {e}')