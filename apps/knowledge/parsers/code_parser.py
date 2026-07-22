"""
Python AST 代码切片器
- 使用 ast 模块按函数/类/方法切片，保留 signature/docstring
- 生成 params 结构化字段，便于代码问答
"""
import ast
from loguru import logger
from typing import List, Dict, Any

from .base import BaseParser



class CodeParser(BaseParser):
    name = 'code'

    def parse(self, file_path: str, **options) -> List[Dict[str, Any]]:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                source = f.read()
        except Exception:
            logger.exception('read code fail')
            return []

        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError:
            logger.warning('AST parse fail, fallback to markdown chunker')
            return [{'type': 'code', 'content': source[:20000],
                     'section_path': 'module', 'page_number': None, 'extra': {'error': 'syntax'}}]

        blocks: List[Dict[str, Any]] = []
        source_lines = source.splitlines()

        # 模块文档
        mod_doc = ast.get_docstring(tree)
        if mod_doc:
            blocks.append({
                'type': 'code', 'content': f'# Module docstring\n"""{mod_doc}"""',
                'section_path': 'module',
                'page_number': None,
                'extra': {'symbol_type': 'module', 'symbol_name': '__module__',
                          'signature': '', 'params': [], 'docstring': mod_doc,
                          'start_line': 1, 'end_line': 1, 'parent_symbol': '',
                          'language': 'python'},
            })

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                blocks.append(self._pack_func(node, source_lines, parent=''))
            elif isinstance(node, ast.ClassDef):
                # 类整体
                sig = f'class {node.name}({", ".join(_base_name(b) for b in node.bases)})'
                doc = ast.get_docstring(node) or ''
                start, end = node.lineno, getattr(node, 'end_lineno', node.lineno)
                content = '\n'.join(source_lines[start - 1:end])
                blocks.append({
                    'type': 'code', 'content': content[:8000],
                    'section_path': node.name, 'page_number': None,
                    'extra': {'symbol_type': 'class', 'symbol_name': node.name,
                              'signature': sig, 'params': [], 'docstring': doc,
                              'start_line': start, 'end_line': end, 'parent_symbol': '',
                              'language': 'python'},
                })
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        blocks.append(self._pack_func(sub, source_lines, parent=node.name))
        return blocks

    def _pack_func(self, node, source_lines, parent=''):
        args = node.args
        params = []
        for a in args.args:
            params.append({
                'name': a.arg,
                'type': ast.unparse(a.annotation) if a.annotation else '',
                'default': '',
            })
        ret = ast.unparse(node.returns) if node.returns else ''
        prefix = 'async def' if isinstance(node, ast.AsyncFunctionDef) else 'def'
        sig = f'{prefix} {node.name}({", ".join(p["name"] for p in params)})'
        if ret:
            sig += f' -> {ret}'
        doc = ast.get_docstring(node) or ''
        start = node.lineno
        end = getattr(node, 'end_lineno', start)
        content = '\n'.join(source_lines[start - 1:end])
        symbol_type = 'method' if parent else 'function'
        return {
            'type': 'code', 'content': content[:4000],
            'section_path': f'{parent}.{node.name}' if parent else node.name,
            'page_number': None,
            'extra': {'symbol_type': symbol_type, 'symbol_name': node.name,
                      'signature': sig, 'params': params, 'docstring': doc,
                      'start_line': start, 'end_line': end, 'parent_symbol': parent,
                      'language': 'python'},
        }


def _base_name(node):
    try:
        return ast.unparse(node)
    except Exception:
        return getattr(node, 'id', '')
