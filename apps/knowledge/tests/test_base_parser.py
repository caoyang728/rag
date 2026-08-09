"""
apps.knowledge.parsers.base 单元测试 —— 解析器基类与文件类型路由

覆盖范围：
- BaseParser 抽象基类：parse 未实现时应抛 NotImplementedError
- get_parser 扩展名路由：pdf/docx/markdown/spreadsheet/presentation/code/config
  各扩展名与 WPS 别名（doc/wps/et/dps）、大小写归一化、未知类型与空值兜底

用纯 pytest（不依赖 DB，不依赖外部解析库）：
get_parser 内部延迟导入各解析器，模块级不引入外部库，直接调用即可。
"""
import pytest

from apps.knowledge.parsers.base import BaseParser, get_parser
from apps.knowledge.parsers.pdf_parser import PDFParser
from apps.knowledge.parsers.docx_parser import DocxParser
from apps.knowledge.parsers.markdown_parser import MarkdownParser
from apps.knowledge.parsers.spreadsheet_parser import SpreadsheetParser
from apps.knowledge.parsers.presentation_parser import PresentationParser
from apps.knowledge.parsers.code_parser import CodeParser
from apps.knowledge.parsers.config_parser import ConfigParser


@pytest.mark.unit
def test_base_parser_parse_raises_not_implemented():
    """抽象基类 parse 未实现时抛 NotImplementedError（直接调用方法体验证）"""
    with pytest.raises(NotImplementedError):
        BaseParser.parse(None, 'x.pdf')


@pytest.mark.unit
@pytest.mark.parametrize('file_type, expected_cls', [
    ('pdf', PDFParser),
    ('docx', DocxParser),
    ('doc', DocxParser),
    ('wps', DocxParser),
    ('markdown', MarkdownParser),
    ('md', MarkdownParser),
    ('txt', MarkdownParser),
    ('csv', SpreadsheetParser),
    ('spreadsheet', SpreadsheetParser),
    ('xlsx', SpreadsheetParser),
    ('xls', SpreadsheetParser),
    ('et', SpreadsheetParser),
    ('ppt', PresentationParser),
    ('pptx', PresentationParser),
    ('presentation', PresentationParser),
    ('dps', PresentationParser),
    ('code', CodeParser),
    ('py', CodeParser),
    ('config', ConfigParser),
])
def test_get_parser_mapping(file_type, expected_cls):
    """get_parser 应返回与扩展名对应的解析器实例"""
    assert isinstance(get_parser(file_type), expected_cls)


@pytest.mark.unit
def test_get_parser_uppercase_normalized():
    """大写扩展名应归一化后路由到对应解析器"""
    assert isinstance(get_parser('PDF'), PDFParser)
    assert isinstance(get_parser('XLSX'), SpreadsheetParser)


@pytest.mark.unit
def test_get_parser_unknown_type_falls_back_to_markdown():
    """未知扩展名应兜底为 MarkdownParser"""
    assert isinstance(get_parser('exe'), MarkdownParser)
    assert isinstance(get_parser(''), MarkdownParser)


@pytest.mark.unit
def test_get_parser_none_falls_back_to_markdown():
    """file_type 为 None 时应兜底为 MarkdownParser"""
    assert isinstance(get_parser(None), MarkdownParser)
