"""
apps.knowledge.parsers.docx_parser 单元测试 —— Docx 解析器

覆盖范围：
- 标题/普通段落提取：标题作为 section_path，普通段落归入最近标题
- Title 样式识别、空段落跳过
- 表格提取（单元格用 | 拼接）
- python-docx 未安装 / 打开失败 → 空列表

用纯 pytest + tmp_path + monkeypatch（不依赖 DB，不依赖 python-docx 安装状态）：
通过向 sys.modules 注入 fake docx 模块模拟 python-docx 行为。
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from apps.knowledge.parsers.docx_parser import DocxParser


def _make_para(text, style_name=None):
    """构造模拟段落：style 为 None 时验证空 style 分支"""
    para = MagicMock()
    para.text = text
    para.style = None if style_name is None else MagicMock(name=style_name)
    if style_name is not None:
        para.style.name = style_name
    return para


def _make_table(rows_data):
    """构造模拟表格：rows -> cells -> text"""
    table = MagicMock()
    rows = []
    for cells in rows_data:
        row = MagicMock()
        row.cells = [MagicMock(text=c) for c in cells]
        rows.append(row)
    table.rows = rows
    return table


def _install_docx(monkeypatch, doc):
    """向 sys.modules 注入 fake docx 模块，Document() 返回给定 doc"""
    fake_docx = types.ModuleType('docx')
    fake_docx.Document = MagicMock(return_value=doc)
    monkeypatch.setitem(sys.modules, 'docx', fake_docx)
    return fake_docx


@pytest.mark.unit
def test_parse_heading_and_paragraphs(tmp_path, monkeypatch):
    """标题段生成自身块，其后普通段落归入标题 section_path"""
    doc = MagicMock()
    doc.paragraphs = [
        _make_para('第一章', 'Heading 1'),
        _make_para('第一章的正文'),
        _make_para('普通正文'),
    ]
    doc.tables = []
    _install_docx(monkeypatch, doc)

    blocks = DocxParser().parse(str(tmp_path / 'a.docx'))

    assert [b['section_path'] for b in blocks] == ['第一章', '第一章', '第一章']
    assert blocks[0]['extra']['style'] == 'heading 1'
    assert blocks[2]['extra'] == {}


@pytest.mark.unit
def test_parse_title_style_recognized(tmp_path, monkeypatch):
    """Title 样式段落应被识别为标题"""
    doc = MagicMock()
    doc.paragraphs = [_make_para('文档标题', 'Title'), _make_para('内容')]
    doc.tables = []
    _install_docx(monkeypatch, doc)

    blocks = DocxParser().parse(str(tmp_path / 't.docx'))

    assert blocks[0]['section_path'] == '文档标题'
    assert blocks[0]['extra']['style'] == 'title'


@pytest.mark.unit
def test_parse_empty_paragraph_skipped(tmp_path, monkeypatch):
    """空白段落应被跳过，不产生块"""
    doc = MagicMock()
    doc.paragraphs = [_make_para('  '), _make_para('有效内容')]
    doc.tables = []
    _install_docx(monkeypatch, doc)

    blocks = DocxParser().parse(str(tmp_path / 'e.docx'))

    assert len(blocks) == 1
    assert blocks[0]['content'] == '有效内容'


@pytest.mark.unit
def test_parse_table_extraction(tmp_path, monkeypatch):
    """表格应按行以 | 拼接输出为 table 块"""
    doc = MagicMock()
    doc.paragraphs = [_make_para('数据表', 'Heading 1')]
    doc.tables = [_make_table([['姓名', '年龄'], ['张三', '30']])]
    _install_docx(monkeypatch, doc)

    blocks = DocxParser().parse(str(tmp_path / 't.docx'))

    table_block = blocks[1]
    assert table_block['type'] == 'table'
    assert table_block['content'] == '姓名 | 年龄\n张三 | 30'
    assert table_block['section_path'] == '数据表'


@pytest.mark.unit
def test_parse_open_error_returns_empty_list(tmp_path, monkeypatch):
    """文件打开失败应返回空列表"""
    fake_docx = types.ModuleType('docx')
    fake_docx.Document = MagicMock(side_effect=FileNotFoundError('no file'))
    monkeypatch.setitem(sys.modules, 'docx', fake_docx)

    with patch('apps.knowledge.parsers.docx_parser.logger'):
        assert DocxParser().parse(str(tmp_path / 'bad.docx')) == []


@pytest.mark.unit
def test_parse_docx_not_installed_returns_empty_list(tmp_path, monkeypatch):
    """python-docx 未安装时应返回空列表"""
    monkeypatch.setitem(sys.modules, 'docx', None)

    assert DocxParser().parse(str(tmp_path / 'a.docx')) == []
