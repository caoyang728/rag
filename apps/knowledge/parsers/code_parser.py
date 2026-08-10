"""
多语言代码切片器
- Python：使用 ast 模块按函数/类/方法切片，保留 signature/docstring
- 其他语言（JS/TS/Java/Go/C/C++/Rust 等）：无第三方 AST 依赖，
  用启发式切片（括号深度扫描 + 顶层声明关键词识别）切出模块 + 各顶层函数/类块
- 生成 params 结构化字段，便于代码问答
"""
import ast
import os
import re
from loguru import logger
from typing import List, Dict, Any

from .base import BaseParser


# 顶层声明识别：跳过 export/public/static 等修饰符前缀。
# 第一条分支覆盖 function/class/interface/fn/func 等关键字声明；
# 第二条分支覆盖 const/let/var 箭头函数与函数/类表达式（避免把普通赋值拆成碎片块）。
_DECL_RE = re.compile(
    r'^\s*(?:'
    r'(?:export|default|public|private|protected|static|async|abstract|final|'
    r'readonly|pub|unsafe|synchronized)\s+)*'
    r'(?:function|class|interface|type|enum|struct|impl|fn|func|trait|union)\b'
    r'|'
    r'^\s*(?:export\s+)?(?:const|let|var)\s+[A-Za-z_$][\w$]*\s*=\s*(?:async\s*)?(?:\(|function|class)'
)

# 类/类型类声明的关键字（用于判断块的 symbol_type）
_CLASS_KW_RE = re.compile(r'(?:class|interface|enum|struct|trait|union|impl|type)\b')
# 声明修饰符关键字（回退取名时跳过，避免 section_path 变成 export/public 等）
_MODIFIER_WORDS = {'export', 'default', 'public', 'private', 'protected', 'static',
                   'async', 'abstract', 'final', 'readonly', 'pub', 'unsafe'}


def _detect_language(file_path: str) -> str:
    """按扩展名识别代码语言；无法识别时按 python 处理（与历史行为一致）"""
    ext = os.path.splitext(file_path)[1].lower()
    _LANG_MAP = {
        '.py': 'python', '.pyw': 'python',
        '.js': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript', '.jsx': 'javascript',
        '.ts': 'typescript', '.tsx': 'typescript',
        '.java': 'java', '.go': 'go',
        '.c': 'c', '.h': 'c',
        '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp',
        '.rs': 'rust',
    }
    return _LANG_MAP.get(ext, 'python')


def _scan_brace_depths(source: str):
    """逐行扫描括号深度，返回每行的起始深度与结束深度

    跳过字符串（'\"）、模板串（`...`，含 ${} 内联）与注释（//、/* */），
    使模板串/字符串/注释内的花括号不影响块边界统计。
    注：正则字面量（如 /}/）无法与除法可靠区分，含花括号的正则可能造成
    少量深度误差，属启发式切片的已知局限。
    """
    starts, ends = [], []
    depth = 0
    in_block_comment = False
    for line in source.splitlines():
        starts.append(depth)
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            if in_block_comment:
                end = line.find('*/', i)
                if end == -1:
                    i = n
                    continue
                i = end + 2
                in_block_comment = False
                continue
            if ch == '/' and i + 1 < n:
                nxt = line[i + 1]
                if nxt == '/':
                    break  # 行注释，跳过整行
                if nxt == '*':
                    in_block_comment = True
                    i += 2
                    continue
            if ch in '\'"':
                quote = ch
                i += 1
                while i < n:
                    if line[i] == '\\':
                        i += 2
                        continue
                    if line[i] == quote:
                        i += 1
                        break
                    i += 1
                continue
            if ch == '`':
                # 模板串：跳到闭合反引号（嵌套 ${...} 内的反引号极罕见，忽略）
                i += 1
                while i < n:
                    if line[i] == '\\':
                        i += 2
                        continue
                    if line[i] == '`':
                        i += 1
                        break
                    i += 1
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth = max(0, depth - 1)
            i += 1
        ends.append(depth)
    return starts, ends


def _block_symbol(first_line: str):
    """从声明首行提取符号名与类型（class/function）"""
    m = re.search(r'(?:function|class|interface|type|enum|struct|trait|union|fn|func|impl)\s+'
                  r'([A-Za-z_$][\w$]*)', first_line)
    if m:
        name = m.group(1)
        stype = 'class' if _CLASS_KW_RE.search(first_line) else 'function'
        return name, stype
    m = re.search(r'(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=', first_line)
    if m:
        return m.group(1), 'function'
    m = re.search(r'([A-Za-z_$][\w$]*)', first_line)
    name = m.group(1) if m else first_line.strip()[:60]
    if name in _MODIFIER_WORDS:
        return first_line.strip()[:60], 'function'
    return name, 'function'


def _split_code_blocks(source: str, language: str) -> List[Dict[str, Any]]:
    """启发式代码分块：按顶层声明切分（模块 + 各函数/类块）

    供非 Python 语言使用：基于括号深度扫描 + 顶层声明关键词识别，
    把文件切成与 Python AST 输出结构一致的块，供 RAG 按符号粒度检索。
    """
    if not source.strip():
        return []
    starts, ends = _scan_brace_depths(source)
    lines = source.splitlines()
    blocks: List[Dict[str, Any]] = []
    module_lines = []  # (行号, 文本)，声明块之间的顶层代码
    n = len(lines)

    def flush_module():
        if not module_lines:
            return
        content = '\n'.join(line for _, line in module_lines)
        blocks.append({
            'type': 'code', 'content': content[:20000],
            'section_path': 'module', 'page_number': None,
            'extra': {'symbol_type': 'module', 'symbol_name': '__module__',
                      'signature': '', 'params': [], 'docstring': '',
                      'start_line': module_lines[0][0] + 1,
                      'end_line': module_lines[-1][0] + 1,
                      'parent_symbol': '', 'language': language},
        })
        module_lines.clear()

    i = 0
    while i < n:
        if starts[i] == 0 and _DECL_RE.match(lines[i]):
            flush_module()
            # 声明块：从本行起，直到括号深度归零（含闭合行）；单行声明则仅本行
            j = i
            while j < n and ends[j] != 0:
                j += 1
            first = lines[i]
            name, stype = _block_symbol(first)
            content = '\n'.join(lines[i:j + 1])[:8000]
            blocks.append({
                'type': 'code', 'content': content,
                'section_path': name, 'page_number': None,
                'extra': {'symbol_type': stype, 'symbol_name': name,
                          'signature': first.strip()[:200], 'params': [],
                          'docstring': '', 'start_line': i + 1, 'end_line': j + 1,
                          'parent_symbol': '', 'language': language},
            })
            i = j + 1
        else:
            module_lines.append((i, lines[i]))
            i += 1
    flush_module()
    return blocks


class CodeParser(BaseParser):
    name = 'code'

    def parse(self, file_path: str, **options) -> List[Dict[str, Any]]:
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                source = f.read()
        except Exception:
            logger.exception('read code fail')
            return []

        language = _detect_language(file_path)
        if language != 'python':
            # 非 Python 语言无 ast 可用，走启发式分块（JS/TS/Java/Go/C/C++/Rust 等）
            return _split_code_blocks(source, language)

        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as e:
            # Python 语法错误时降级为整块代码，保留 error 标记供排查
            logger.warning(f'Python AST 解析失败: {e}，降级为整块代码')
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
