"""
四层记忆管理器
分层加载 + Token 预算控制
拼装顺序：全局记忆 → 用户长期 → 会话摘要 → 短时对话 → 当前问题 → RAG 检索
Token 预算 8000（默认），按分层等比截断
"""
from loguru import logger
import json
import time
from typing import Dict, List, Any, Optional

from django.conf import settings

from apps.memory.models import Session, SessionMemory, UserMemory, GlobalMemory
from .short_term import ShortTermMemory



# 简易 Token 估算：中文 1 字 ≈ 1 token，英文 4 字符 ≈ 1 token
def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    zh = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
    en = len(text) - zh
    return zh + en // 4 + 1


class MemoryManager:
    """记忆总管"""

    def __init__(self, budget: int = None):
        self.budget = budget or settings.MEMORY_TOKEN_BUDGET
        self.short_term = ShortTermMemory()

    def load_context(self, user, session: Session, question: str,
                     root_type: str = 'company_doc') -> Dict[str, Any]:
        """加载完整上下文"""
        t0 = time.time()
        parts = {
            'global': self._load_global(root_type),
            'user': self._load_user(user),
            'session': self._load_session(session),
            'short_term': self.short_term.get_turns(session.id),
        }

        # 组装成 Prompt 中的 memory_block 文本
        memory_block = self._assemble(parts)

        logger.info('[Memory] load ctx tokens≈%d cost=%dms',
                    estimate_tokens(memory_block), int((time.time() - t0) * 1000))
        return {
            'memory_block': memory_block,
            'parts': parts,
        }

    def _load_global(self, root_type: str) -> str:
        """全局记忆：按 scope_root_types 过滤"""
        gms = GlobalMemory.objects.filter(is_enabled=True).order_by('-priority')
        lines = []
        for gm in gms:
            scopes = gm.scope_root_types or []
            if scopes and root_type not in scopes and 'all' not in scopes:
                continue
            lines.append(f'- {gm.content}')
        return '\n'.join(lines) if lines else ''

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
        """拼装 memory_block，按预算截断"""
        blocks = []
        if parts['global']:
            blocks.append('【全局记忆 · 公司规则】\n' + parts['global'])
        if parts['user']:
            blocks.append('【用户画像】\n' + parts['user'])
        if parts['session']:
            blocks.append('【会话摘要】\n' + parts['session'])
        if parts['short_term']:
            short_lines = []
            for turn in parts['short_term'][-settings.SHORT_TERM_MAX_TURNS:]:
                q = turn.get('question', '')
                a = turn.get('answer', '')
                short_lines.append(f'Q: {q}\nA: {a[:300]}')
            blocks.append('【最近对话】\n' + '\n---\n'.join(short_lines))

        text = '\n\n'.join(blocks)
        # 按 Token 预算截断（简单从头保留）
        max_mem = int(self.budget * 0.5)  # 记忆最多占 50%
        if estimate_tokens(text) > max_mem:
            # 从后往前保留
            text = text[-(max_mem * 3):]  # 粗略切片
        return text

    def append_turn(self, session: Session, question: str, answer: str):
        """记录一轮对话到短时记忆"""
        self.short_term.append_turn(session.id, question, answer)
        session.turn_count = (session.turn_count or 0) + 1
        session.save(update_fields=['turn_count', 'last_active_at'])
