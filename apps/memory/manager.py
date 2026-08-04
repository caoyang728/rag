"""
四层记忆管理器
分层加载 + Token 预算控制
拼装顺序：全局记忆 → 用户长期 → 会话摘要 → 短时对话 → 当前问题 → RAG 检索
Token 预算 8000（默认），按分层等比截断
"""
from loguru import logger
import time
from typing import Dict, List, Any

from django.conf import settings

from apps.memory.models import Session, SessionMemory, UserMemory, GlobalMemory
from .short_term import ShortTermMemory


def estimate_tokens(text: str) -> int:
    """精确 Token 估算：中文 1 字 ≈ 1.5 token，英文 4 字符 ≈ 1 token"""
    if not text:
        return 0
    zh = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en = len(text) - zh
    return int(zh * 1.5 + en // 4 + 1)


def truncate_text(text: str, max_tokens: int) -> str:
    """安全截断文本，确保不切断中文"""
    if not text or estimate_tokens(text) <= max_tokens:
        return text
    
    chars_per_token = 2
    max_chars = max_tokens * chars_per_token
    
    truncated = text[:max_chars]
    if truncated != text:
        while truncated and truncated[-1] > '\u9fff':
            truncated = truncated[:-1]
        if truncated:
            truncated += '...'
    return truncated


class MemoryManager:
    """记忆总管"""
    _global_cache = {}
    _global_cache_time = 0

    def __init__(self, budget: int = None):
        self.budget = budget or settings.MEMORY_TOKEN_BUDGET
        self.short_term = ShortTermMemory()

    def load_context(self, user, session: Session, question: str,
                     root_type: str = 'company_doc') -> Dict[str, Any]:
        """加载完整上下文，带异常处理"""
        t0 = time.time()
        parts = {
            'global': '',
            'user': '',
            'session': '',
            'short_term': [],
        }

        try:
            parts['global'] = self._load_global(root_type)
        except Exception as e:
            logger.error(f'[Memory] load global failed: {e}')

        try:
            parts['user'] = self._load_user(user)
        except Exception as e:
            logger.error(f'[Memory] load user failed: {e}')

        try:
            parts['session'] = self._load_session(session)
        except Exception as e:
            logger.error(f'[Memory] load session failed: {e}')

        try:
            parts['short_term'] = self.short_term.get_turns(session.id)
        except Exception as e:
            logger.error(f'[Memory] load short_term failed: {e}')

        memory_block = self._assemble(parts)

        logger.info(f'[Memory] load ctx tokens≈{estimate_tokens(memory_block)} cost={int((time.time() - t0) * 1000)}ms')
        return {
            'memory_block': memory_block,
            'parts': parts,
        }

    def _load_global(self, root_type: str) -> str:
        """全局记忆：带缓存，按 scope_root_types 过滤"""
        now = time.time()
        if now - MemoryManager._global_cache_time < 300:
            cached = MemoryManager._global_cache.get(root_type)
            if cached is not None:
                return cached

        try:
            gms = GlobalMemory.objects.filter(is_enabled=True).order_by('-priority')
            lines = []
            for gm in gms:
                scopes = gm.scope_root_types or []
                if scopes and root_type not in scopes and 'all' not in scopes:
                    continue
                lines.append(f'- {gm.content}')
            result = '\n'.join(lines) if lines else ''
            MemoryManager._global_cache[root_type] = result
            MemoryManager._global_cache_time = now
            return result
        except Exception:
            return ''

    def _load_user(self, user) -> str:
        """用户长期记忆"""
        if not user or not user.is_authenticated:
            return ''
        um = UserMemory.objects.filter(user=user).first()
        if not um or not um.profile_text:
            return ''
        return um.profile_text

    def _load_session(self, session: Session) -> str:
        """会话摘要"""
        try:
            sm = session.memory
        except SessionMemory.DoesNotExist:
            return ''
        if not sm.summary:
            return ''
        entities = ', '.join(sm.entities or [])
        return f'{sm.summary}' + (f'\n关键实体：{entities}' if entities else '')

    def _assemble(self, parts: Dict[str, Any]) -> str:
        """拼装 memory_block，按层级优先级逐步截断"""
        max_mem = int(self.budget * 0.5)
        
        blocks = []
        if parts['global']:
            blocks.append({'content': '【全局记忆 · 公司规则】\n' + parts['global'], 'priority': 4})
        if parts['user']:
            blocks.append({'content': '【用户画像】\n' + parts['user'], 'priority': 3})
        if parts['session']:
            blocks.append({'content': '【会话摘要】\n' + parts['session'], 'priority': 2})
        if parts['short_term']:
            short_lines = []
            for turn in parts['short_term'][-settings.SHORT_TERM_MAX_TURNS:]:
                q = turn.get('question', '')
                a = turn.get('answer', '')
                short_lines.append(f'Q: {q}\nA: {a[:300]}')
            blocks.append({'content': '【最近对话】\n' + '\n---\n'.join(short_lines), 'priority': 1})

        blocks.sort(key=lambda x: x['priority'], reverse=True)

        text = '\n\n'.join(b['content'] for b in blocks)
        total_tokens = estimate_tokens(text)

        if total_tokens <= max_mem:
            return text

        for i in range(len(blocks)):
            block = blocks[i]
            current_tokens = estimate_tokens(block['content'])
            needed_reduction = total_tokens - max_mem
            if current_tokens <= needed_reduction:
                blocks[i]['content'] = ''
                total_tokens -= current_tokens
            else:
                truncated = truncate_text(block['content'], current_tokens - needed_reduction)
                blocks[i]['content'] = truncated
                total_tokens = estimate_tokens('\n\n'.join(b['content'] for b in blocks if b['content']))
            if total_tokens <= max_mem:
                break

        return '\n\n'.join(b['content'] for b in blocks if b['content'])

    def append_turn(self, session: Session, question: str, answer: str):
        """记录一轮对话到短时记忆，每5轮触发会话记忆提炼"""
        self.short_term.append_turn(session.id, question, answer)
        session.turn_count = (session.turn_count or 0) + 1
        session.save(update_fields=['turn_count', 'last_active_at'])
        
        if session.turn_count % 5 == 0:
            try:
                from apps.memory.tasks import refine_session_memory
                refine_session_memory.delay(session.id)
            except Exception as e:
                logger.error(f'[Memory] trigger session refine failed: {e}')