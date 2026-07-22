"""Config 文件解析器（YAML/JSON/INI/env）"""
import json
from loguru import logger
from typing import List, Dict, Any

from .base import BaseParser



class ConfigParser(BaseParser):
    name = 'config'

    def parse(self, file_path: str, **options) -> List[Dict[str, Any]]:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        except Exception:
            return []

        # 尝试 YAML/JSON 解析，失败就保留原文
        parsed = None
        if file_path.endswith(('.yaml', '.yml')):
            try:
                import yaml
                parsed = yaml.safe_load(text)
            except Exception:
                pass
        elif file_path.endswith('.json'):
            try:
                parsed = json.loads(text)
            except Exception:
                pass

        content = text
        if parsed is not None:
            # 把 KV 展平输出，便于 BM25 命中
            content = _flatten(parsed)

        return [{
            'type': 'config', 'content': content[:20000],
            'section_path': 'config', 'page_number': None,
            'extra': {'file': file_path},
        }]


def _flatten(obj, prefix=''):
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f'{prefix}.{k}' if prefix else str(k)
            if isinstance(v, (dict, list)):
                lines.append(_flatten(v, key))
            else:
                lines.append(f'{key} = {v}')
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            lines.append(_flatten(v, f'{prefix}[{i}]'))
    else:
        lines.append(f'{prefix} = {obj}')
    return '\n'.join(lines)
