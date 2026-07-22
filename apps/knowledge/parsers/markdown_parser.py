"""Markdown / TXT 解析器"""
from loguru import logger
import re
from typing import List, Dict, Any

from .base import BaseParser


_HEADING = re.compile(r'^(#{1,6})\s+(.+)$')


class MarkdownParser(BaseParser):
    name = 'markdown'

    def parse(self, file_path: str, **options) -> List[Dict[str, Any]]:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        except Exception:
            logger.exception('read md fail')
            return []

        blocks: List[Dict[str, Any]] = []
        section_stack: List[str] = []
        buf: List[str] = []

        def flush(section: str):
            if buf:
                content = '\n'.join(buf).strip()
                if content:
                    blocks.append({
                        'type': 'text', 'content': content,
                        'section_path': section,
                        'page_number': None, 'extra': {},
                    })
                buf.clear()

        for line in text.splitlines():
            m = _HEADING.match(line)
            if m:
                # 遇到新标题：先把 buf flush
                section = ' > '.join(section_stack) if section_stack else ''
                flush(section)
                level = len(m.group(1))
                title = m.group(2).strip()
                # 维护 stack
                section_stack = section_stack[:level - 1]
                section_stack.append(title[:32])
            else:
                buf.append(line)

        # flush 最后一段
        section = ' > '.join(section_stack) if section_stack else ''
        flush(section)
        return blocks
