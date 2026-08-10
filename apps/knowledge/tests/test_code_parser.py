"""
apps.knowledge.parsers.code_parser 单元测试 —— Python AST 代码切片器

覆盖范围：
- 模块 docstring → module 块
- 普通函数/带注解参数与返回值的函数 → function 块（signature/params/docstring 结构化）
- async 函数 → async def 签名
- 类（含基类）与类内方法 → class 块 + method 块
- 语法错误 → 降级为 module 错误块
- 文件不存在 → 空列表

用纯 pytest + tmp_path（不依赖 DB）。
"""
import pytest

from apps.knowledge.parsers.code_parser import CodeParser


@pytest.mark.unit
def test_parse_module_docstring_creates_module_block(tmp_path):
    """模块 docstring 应生成 module 块"""
    py_file = tmp_path / 'mod.py'
    py_file.write_text(
        '"""模块说明文档"""\n\nVERSION = 1\n', encoding='utf-8')

    blocks = CodeParser().parse(str(py_file))

    assert blocks[0]['section_path'] == 'module'
    assert blocks[0]['extra']['symbol_type'] == 'module'
    assert blocks[0]['extra']['docstring'] == '模块说明文档'


@pytest.mark.unit
def test_parse_function_with_annotations(tmp_path):
    """带参数注解与返回注解的函数应生成完整 signature 与 params"""
    py_file = tmp_path / 'funcs.py'
    py_file.write_text(
        'def add(a: int, b: int) -> int:\n'
        '    """两数相加"""\n'
        '    return a + b\n', encoding='utf-8')

    blocks = CodeParser().parse(str(py_file))

    assert len(blocks) == 1
    b = blocks[0]
    assert b['section_path'] == 'add'
    assert b['extra']['symbol_type'] == 'function'
    assert b['extra']['signature'] == 'def add(a, b) -> int'
    assert b['extra']['params'] == [{'name': 'a', 'type': 'int', 'default': ''},
                                    {'name': 'b', 'type': 'int', 'default': ''}]
    assert b['extra']['docstring'] == '两数相加'


@pytest.mark.unit
def test_parse_async_function(tmp_path):
    """async 函数签名应以 async def 开头"""
    py_file = tmp_path / 'async_mod.py'
    py_file.write_text(
        'async def fetch(url: str):\n'
        '    return await get(url)\n', encoding='utf-8')

    blocks = CodeParser().parse(str(py_file))

    assert blocks[0]['extra']['signature'] == 'async def fetch(url)'


@pytest.mark.unit
def test_parse_class_with_bases_and_methods(tmp_path):
    """类应生成 class 块，类内方法应生成 method 块且 parent 为类名"""
    py_file = tmp_path / 'cls.py'
    py_file.write_text(
        'class Service(BaseService):\n'
        '    """服务类"""\n'
        '\n'
        '    def run(self):\n'
        '        return "ok"\n', encoding='utf-8')

    blocks = CodeParser().parse(str(py_file))

    by_section = {b['section_path']: b['extra'] for b in blocks}
    assert 'Service' in by_section
    assert by_section['Service']['symbol_type'] == 'class'
    assert by_section['Service']['signature'] == 'class Service(BaseService)'
    assert 'Service.run' in by_section
    assert by_section['Service.run']['symbol_type'] == 'method'
    assert by_section['Service.run']['parent_symbol'] == 'Service'


@pytest.mark.unit
def test_parse_function_without_docstring(tmp_path):
    """无 docstring 的函数 docstring 应为空字符串，无模块 docstring 时不生成 module 块"""
    py_file = tmp_path / 'nofunc.py'
    py_file.write_text('def f():\n    pass\n', encoding='utf-8')

    blocks = CodeParser().parse(str(py_file))

    assert len(blocks) == 1
    assert blocks[0]['extra']['docstring'] == ''
    assert blocks[0]['extra']['signature'] == 'def f()'


@pytest.mark.unit
def test_parse_syntax_error_falls_back(tmp_path):
    """语法错误的代码应降级为 module 错误块"""
    py_file = tmp_path / 'bad.py'
    py_file.write_text('def broken(:\n', encoding='utf-8')

    blocks = CodeParser().parse(str(py_file))

    assert len(blocks) == 1
    assert blocks[0]['section_path'] == 'module'
    assert blocks[0]['extra']['error'] == 'syntax'


@pytest.mark.unit
def test_parse_missing_file_returns_empty_list(tmp_path):
    """文件不存在时返回空列表"""
    assert CodeParser().parse(str(tmp_path / 'none.py')) == []


@pytest.mark.unit
def test_parse_content_limit_truncated(tmp_path):
    """超大函数内容应被截断到 4000 字符"""
    body = '    x = 1  # 填充\n' * 1000
    py_file = tmp_path / 'big.py'
    py_file.write_text(f'def big():\n{body}', encoding='utf-8')

    blocks = CodeParser().parse(str(py_file))

    assert len(blocks) == 1
    assert len(blocks[0]['content']) <= 4000


# ============================================================================
# 非 Python 语言：启发式分块（JS/TS/Go 等，无第三方 AST 依赖）
# ============================================================================
@pytest.mark.unit
def test_parse_js_functions_and_classes(tmp_path):
    """JS 文件应按顶层函数/类切块，import 等顶层代码归入 module 块"""
    js_file = tmp_path / 'app.js'
    js_file.write_text(
        'import { x } from "./x";\n'
        '\n'
        'export function add(a, b) {\n'
        '    return a + b;\n'
        '}\n'
        '\n'
        'class Service {\n'
        '    run() {\n'
        '        return "ok";\n'
        '    }\n'
        '}\n', encoding='utf-8')

    blocks = CodeParser().parse(str(js_file))

    by_section = {b['section_path']: b['extra'] for b in blocks}
    assert 'module' in by_section
    assert 'import' in [b for b in blocks if b['section_path'] == 'module'][0]['content']
    assert by_section['add']['symbol_type'] == 'function'
    assert by_section['add']['language'] == 'javascript'
    assert by_section['Service']['symbol_type'] == 'class'
    assert by_section['Service']['language'] == 'javascript'


@pytest.mark.unit
def test_parse_js_arrow_function_with_template(tmp_path):
    """JS 箭头函数应切块，模板串内的花括号不应影响块边界"""
    js_file = tmp_path / 'arrow.js'
    js_file.write_text(
        'const greet = (name) => {\n'
        '    return `Hello ${name} {not a brace}`;\n'
        '};\n'
        '\n'
        'const n = 42;\n', encoding='utf-8')

    blocks = CodeParser().parse(str(js_file))

    by_section = {b['section_path']: b['extra'] for b in blocks}
    assert by_section['greet']['symbol_type'] == 'function'
    # 模板串内的花括号不应导致函数块提前闭合（content 应包含 return 行）
    assert 'return `Hello' in [b for b in blocks if b['section_path'] == 'greet'][0]['content']
    # 普通赋值不拆块，留在 module
    assert 'const n = 42;' in [b for b in blocks if b['section_path'] == 'module'][0]['content']


@pytest.mark.unit
def test_parse_ts_interface_and_function(tmp_path):
    """TS 文件应识别 interface（class 型）与函数"""
    ts_file = tmp_path / 'types.ts'
    ts_file.write_text(
        'export interface User {\n'
        '    id: number;\n'
        '    name: string;\n'
        '}\n'
        '\n'
        'export function format(u: User): string {\n'
        '    return u.name;\n'
        '}\n', encoding='utf-8')

    blocks = CodeParser().parse(str(ts_file))

    by_section = {b['section_path']: b['extra'] for b in blocks}
    assert by_section['User']['symbol_type'] == 'class'
    assert by_section['User']['language'] == 'typescript'
    assert by_section['format']['symbol_type'] == 'function'


@pytest.mark.unit
def test_parse_go_func(tmp_path):
    """Go 文件的 func 声明应切块"""
    go_file = tmp_path / 'main.go'
    go_file.write_text(
        'package main\n'
        '\n'
        'func Add(a, b int) int {\n'
        '    return a + b\n'
        '}\n', encoding='utf-8')

    blocks = CodeParser().parse(str(go_file))

    by_section = {b['section_path']: b['extra'] for b in blocks}
    assert by_section['Add']['symbol_type'] == 'function'
    assert by_section['Add']['language'] == 'go'
    assert by_section['Add']['start_line'] == 3
    assert by_section['Add']['end_line'] == 5


@pytest.mark.unit
def test_parse_js_comments_and_strings_ignored(tmp_path):
    """JS 注释/字符串中的括号不应计入深度（//、/* */、字符串字面量）"""
    js_file = tmp_path / 'comments.js'
    js_file.write_text(
        'function a() {\n'
        '    // } 注释中的右花括号\n'
        '    /* { 块注释 } */\n'
        '    const s = "}";\n'
        '}\n', encoding='utf-8')

    blocks = CodeParser().parse(str(js_file))

    by_section = {b['section_path']: b['extra'] for b in blocks}
    # 函数块应完整闭合（end_line 覆盖到真实 }），不应被注释/字符串中的 } 提前截断
    a_block = [b for b in blocks if b['section_path'] == 'a'][0]
    assert a_block['extra']['end_line'] == 5
    # 末行是真实的闭合括号，而非注释/字符串里的 } 截断点
    assert a_block['content'].splitlines()[-1].strip() == '}'
