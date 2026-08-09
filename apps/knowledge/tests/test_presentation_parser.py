"""
apps.knowledge.parsers.presentation_parser 单元测试 —— PPTX/PPT 解析器

覆盖范围：
- PPTX 幻灯片文本提取（标题 = 首个文本）、页码与 section_path
- PPTX 表格提取（表头后插入 --- 分隔行）
- 无文本幻灯片以「幻灯片 N」作标题
- .ppt 旧版格式降级提示块
- python-pptx 未安装 / 打开失败 → 空列表

用纯 pytest + tmp_path + monkeypatch（不依赖 DB，不依赖 python-pptx 安装状态）：
通过向 sys.modules 注入 fake pptx 模块模拟 python-pptx 行为。
"""
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from apps.knowledge.parsers.presentation_parser import PresentationParser


def _make_shape(texts=None, table_rows=None):
    """构造模拟 shape：texts 为段落文本列表，table_rows 为单元格二维列表"""
    shape = MagicMock()
    shape.has_text_frame = texts is not None
    if texts is not None:
        paras = []
        for t in texts:
            p = MagicMock()
            p.text = t
            paras.append(p)
        shape.text_frame.paragraphs = paras
    shape.has_table = table_rows is not None
    if table_rows is not None:
        table = MagicMock()
        rows = []
        for cells in table_rows:
            row = MagicMock()
            row.cells = [MagicMock(text=c) for c in cells]
            rows.append(row)
        table.rows = rows
        table.columns = [None] * len(table_rows[0]) if table_rows else []
        shape.table = table
    return shape


def _install_pptx(monkeypatch, prs):
    """向 sys.modules 注入 fake pptx 模块，Presentation() 返回给定 prs"""
    fake_pptx = types.ModuleType('pptx')
    fake_pptx.Presentation = MagicMock(return_value=prs)
    monkeypatch.setitem(sys.modules, 'pptx', fake_pptx)
    return fake_pptx


@pytest.mark.unit
def test_parse_pptx_text_and_table(tmp_path, monkeypatch):
    """幻灯片应产出文本块（标题=首个文本）与表格块（含 --- 分隔行）"""
    slide = MagicMock()
    slide.shapes = [
        _make_shape(texts=['产品介绍', '这是一段说明']),
        _make_shape(table_rows=[['指标', '数值'], ['QPS', '100']]),
    ]
    prs = MagicMock()
    prs.slides = [slide]
    _install_pptx(monkeypatch, prs)

    blocks = PresentationParser().parse(str(tmp_path / 'a.pptx'))

    text_block = blocks[0]
    assert text_block['type'] == 'text'
    assert text_block['content'] == '产品介绍\n这是一段说明'
    assert text_block['page_number'] == 1
    assert text_block['extra']['title'] == '产品介绍'
    assert text_block['section_path'] == '幻灯片 1: 产品介绍'

    table_block = blocks[1]
    assert table_block['type'] == 'table'
    assert table_block['content'] == '指标 | 数值\n--- | ---\nQPS | 100'
    assert table_block['extra']['format'] == 'pptx_table'


@pytest.mark.unit
def test_parse_pptx_slide_without_text(tmp_path, monkeypatch):
    """无文本幻灯片不产出文本块，标题回退为「幻灯片 N」"""
    slide = MagicMock()
    slide.shapes = []
    prs = MagicMock()
    prs.slides = [slide]
    _install_pptx(monkeypatch, prs)

    blocks = PresentationParser().parse(str(tmp_path / 'b.pptx'))

    assert blocks == []


@pytest.mark.unit
def test_parse_pptx_multiple_slides(tmp_path, monkeypatch):
    """多页幻灯片应逐页编号"""
    prs = MagicMock()
    slides = []
    for i in range(2):
        s = MagicMock()
        s.shapes = [_make_shape(texts=[f'第{i + 1}页'])]
        slides.append(s)
    prs.slides = slides
    _install_pptx(monkeypatch, prs)

    blocks = PresentationParser().parse(str(tmp_path / 'c.pptx'))

    assert [b['page_number'] for b in blocks] == [1, 2]


@pytest.mark.unit
def test_parse_ppt_legacy_fallback(tmp_path):
    """.ppt 旧格式应返回降级提示块"""
    with patch('apps.knowledge.parsers.presentation_parser.logger'):
        blocks = PresentationParser().parse(str(tmp_path / 'old.ppt'))

    assert len(blocks) == 1
    assert blocks[0]['extra'] == {'format': 'ppt', 'parse_error': 'unsupported_legacy_format'}
    assert blocks[0]['section_path'] == '解析提示'


@pytest.mark.unit
def test_parse_pptx_not_installed_returns_empty_list(tmp_path, monkeypatch):
    """python-pptx 未安装时应返回空列表"""
    monkeypatch.setitem(sys.modules, 'pptx', None)

    with patch('apps.knowledge.parsers.presentation_parser.logger'):
        assert PresentationParser().parse(str(tmp_path / 'a.pptx')) == []


@pytest.mark.unit
def test_parse_pptx_open_error_returns_empty_list(tmp_path, monkeypatch):
    """打开 PPTX 失败应返回空列表"""
    fake_pptx = types.ModuleType('pptx')
    fake_pptx.Presentation = MagicMock(side_effect=Exception('corrupt'))
    monkeypatch.setitem(sys.modules, 'pptx', fake_pptx)

    with patch('apps.knowledge.parsers.presentation_parser.logger'):
        assert PresentationParser().parse(str(tmp_path / 'bad.pptx')) == []


@pytest.mark.unit
def test_parse_unknown_extension_treated_as_pptx(tmp_path, monkeypatch):
    """未知扩展名应按 PPTX 逻辑解析"""
    slide = MagicMock()
    slide.shapes = [_make_shape(texts=['默认标题'])]
    prs = MagicMock()
    prs.slides = [slide]
    _install_pptx(monkeypatch, prs)

    blocks = PresentationParser().parse(str(tmp_path / 'a.weird'))

    assert len(blocks) == 1
    assert blocks[0]['extra']['title'] == '默认标题'
