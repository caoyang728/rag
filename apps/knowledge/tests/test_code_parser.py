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
