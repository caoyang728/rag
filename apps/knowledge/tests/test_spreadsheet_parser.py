"""
apps.knowledge.parsers.spreadsheet_parser 单元测试 —— 电子表格解析器

覆盖范围：
- parse 扩展名路由：csv / xlsx / et / xls（降级提示）/ 未知扩展名（按 xlsx 处理）
- CSV：多编码自动检测（utf-8/gbk/latin-1）、分隔符自动嗅探（逗号/分号）、空文件、
  文件不存在、管道符转义、内容截断（50000 字符）
- XLSX：多 sheet / 空 sheet 跳过 / 全空行跳过 / None 单元格转空串 /
  打开失败 / openpyxl 未安装 / 内容截断
- .xls 旧格式降级返回「解析提示」文本块

用纯 pytest + mock（不依赖 DB）：
CSV 用真实临时文件验证编码/分隔符行为；XLSX 通过 patch openpyxl.load_workbook
模拟工作簿结构，不涉及 ORM。

已知实现缺陷（仅测试如实记录，不修改源码）：
- _parse_csv 基于 content.splitlines() 逐行读取，引号内换行会被吞掉（换行消失
  而非替换为空格），见 test_parse_csv_quoted_newline_mangled。
"""
import sys
from unittest.mock import MagicMock, patch

from apps.knowledge.parsers.spreadsheet_parser import SpreadsheetParser


# ============================================================================
# 测试辅助：构造 openpyxl 工作簿 mock
# ============================================================================
def _make_worksheet(title, rows):
    """构造模拟的 openpyxl worksheet：iter_rows(values_only=True) 返回给定行"""
    ws = MagicMock()
    ws.title = title
    ws.iter_rows.return_value = rows
    return ws


def _make_workbook(*worksheets):
    """构造模拟的工作簿对象"""
    wb = MagicMock()
    wb.worksheets = list(worksheets)
    return wb


# ============================================================================
# CSV 解析
# ============================================================================
def test_parse_csv_basic(tmp_path):
    """普通 UTF-8 逗号分隔 CSV：应输出 markdown 表格（表头 + 分隔行）与行/列统计"""
    csv_file = tmp_path / 'basic.csv'
    csv_file.write_text('name,age\nAlice,30\nBob,25', encoding='utf-8')

    blocks = SpreadsheetParser().parse(str(csv_file))

    assert len(blocks) == 1
    b = blocks[0]
    assert b['type'] == 'table'
    assert b['content'] == 'name | age\n--- | ---\nAlice | 30\nBob | 25'
    assert b['section_path'] == 'CSV 数据'
    assert b['page_number'] is None
    assert b['extra'] == {'rows': 3, 'cols': 2, 'format': 'csv'}


def test_parse_csv_gbk_encoding(tmp_path):
    """GBK 编码 CSV：utf-8 解码失败后应自动回退到 gbk"""
    csv_file = tmp_path / 'gbk.csv'
    csv_file.write_bytes('姓名,年龄\n张三,30'.encode('gbk'))

    blocks = SpreadsheetParser().parse(str(csv_file))

    assert len(blocks) == 1
    assert blocks[0]['content'].splitlines()[0] == '姓名 | 年龄'
    assert '张三' in blocks[0]['content']


def test_parse_csv_latin1_encoding(tmp_path):
    """latin-1 编码 CSV：utf-8/gbk 均失败后应回退到 latin-1（兜底编码）"""
    csv_file = tmp_path / 'latin1.csv'
    csv_file.write_bytes('caf\xe9,1\ncaf\xe9,2'.encode('latin-1'))  # é 的 latin-1 字节

    blocks = SpreadsheetParser().parse(str(csv_file))

    assert len(blocks) == 1
    assert 'café | 1' in blocks[0]['content']


@patch('apps.knowledge.parsers.spreadsheet_parser.logger')
def test_parse_csv_missing_file(mock_logger, tmp_path):
    """文件不存在：所有编码尝试均失败（文件打开异常），应记录错误并返回空列表"""
    blocks = SpreadsheetParser().parse(str(tmp_path / 'nope.csv'))
    assert blocks == []
    mock_logger.error.assert_called_once()


def test_parse_csv_empty_file(tmp_path):
    """空 CSV 文件：无任何行，返回空列表"""
    csv_file = tmp_path / 'empty.csv'
    csv_file.write_text('', encoding='utf-8')

    blocks = SpreadsheetParser().parse(str(csv_file))
    assert blocks == []


def test_parse_csv_semicolon_delimiter(tmp_path):
    """分隔符嗅探：分号分隔的 CSV 应被正确识别并按分号切分"""
    csv_file = tmp_path / 'semi.csv'
    csv_file.write_text('a;b\n1;2', encoding='utf-8')

    blocks = SpreadsheetParser().parse(str(csv_file))

    assert len(blocks) == 1
    assert blocks[0]['content'] == 'a | b\n--- | ---\n1 | 2'
    assert blocks[0]['extra']['cols'] == 2


def test_parse_csv_pipe_escape(tmp_path):
    """单元格中的管道符 | 应被转义为 \\|，避免破坏 markdown 表格结构"""
    csv_file = tmp_path / 'pipe.csv'
    csv_file.write_text('name,note\nA,x|y', encoding='utf-8')

    blocks = SpreadsheetParser().parse(str(csv_file))

    assert 'A | x\\|y' in blocks[0]['content']


def test_parse_csv_quoted_newline_mangled(tmp_path):
    """引号内换行（实现缺陷）：基于 splitlines() 的逐行读取会把引号内换行吞掉
    （'x\\ny' 变为 'xy'，换行既未保留也未替换为空格），此处如实记录当前行为"""
    csv_file = tmp_path / 'quoted.csv'
    csv_file.write_text('name,note\nA,"x\ny"', encoding='utf-8')

    blocks = SpreadsheetParser().parse(str(csv_file))

    assert len(blocks) == 1
    assert 'A | xy' in blocks[0]['content']


def test_parse_csv_long_content_truncated(tmp_path):
    """大 CSV：表格内容超过 50000 字符时应截断"""
    csv_file = tmp_path / 'large.csv'
    rows = [f'r{i},cell data value {i}' for i in range(4000)]
    csv_file.write_text('\n'.join(rows), encoding='utf-8')

    blocks = SpreadsheetParser().parse(str(csv_file))

    assert len(blocks) == 1
    assert len(blocks[0]['content']) == 50000
    assert blocks[0]['extra']['rows'] == 4000


# ============================================================================
# XLSX / ET 解析（openpyxl mock）
# ============================================================================
def test_parse_xlsx_basic(tmp_path):
    """普通 XLSX：保留 sheet 结构，输出 markdown 表格与行/列统计；
    load_workbook 应以 read_only + data_only 模式打开，并正确关闭工作簿"""
    ws = _make_worksheet('Sheet1', [('a', 'b'), ('1', '2')])
    wb = _make_workbook(ws)

    with patch('openpyxl.load_workbook', return_value=wb) as mock_load:
        blocks = SpreadsheetParser().parse(str(tmp_path / 'data.xlsx'))

    assert len(blocks) == 1
    b = blocks[0]
    assert b['type'] == 'table'
    assert b['content'] == 'a | b\n--- | ---\n1 | 2'
    assert b['section_path'] == 'Sheet: Sheet1'
    assert b['page_number'] is None
    assert b['extra'] == {'rows': 2, 'cols': 2, 'sheet_name': 'Sheet1', 'format': 'xlsx'}
    mock_load.assert_called_once_with(str(tmp_path / 'data.xlsx'), read_only=True, data_only=True)
    wb.close.assert_called_once()


def test_parse_xlsx_multiple_sheets(tmp_path):
    """多 sheet 工作簿：每个非空 sheet 各输出一个 table block，保留 sheet 名"""
    ws1 = _make_worksheet('数据1', [('a', 'b'), ('1', '2')])
    ws2 = _make_worksheet('数据2', [('x',)])
    wb = _make_workbook(ws1, ws2)

    with patch('openpyxl.load_workbook', return_value=wb):
        blocks = SpreadsheetParser().parse(str(tmp_path / 'multi.xlsx'))

    assert len(blocks) == 2
    assert blocks[0]['section_path'] == 'Sheet: 数据1'
    assert blocks[0]['extra']['rows'] == 2
    assert blocks[1]['section_path'] == 'Sheet: 数据2'
    assert blocks[1]['extra'] == {'rows': 1, 'cols': 1, 'sheet_name': '数据2', 'format': 'xlsx'}


def test_parse_xlsx_empty_sheet_skipped(tmp_path):
    """空 sheet：无数据行时不输出 block（全部 sheet 为空则整体返回空列表）"""
    ws = _make_worksheet('空表', [])
    wb = _make_workbook(ws)

    with patch('openpyxl.load_workbook', return_value=wb):
        blocks = SpreadsheetParser().parse(str(tmp_path / 'empty.xlsx'))

    assert blocks == []
    wb.close.assert_called_once()


def test_parse_xlsx_empty_rows_skipped(tmp_path):
    """全空行（空字符串 / None）应被跳过，非空行正常保留"""
    ws = _make_worksheet('Sheet1', [('', ''), (None, None), ('a', 'b')])
    wb = _make_workbook(ws)

    with patch('openpyxl.load_workbook', return_value=wb):
        blocks = SpreadsheetParser().parse(str(tmp_path / 'blank.xlsx'))

    assert len(blocks) == 1
    assert blocks[0]['extra']['rows'] == 1
    assert blocks[0]['content'] == 'a | b\n--- | ---'


def test_parse_xlsx_none_cells_to_empty(tmp_path):
    """None 单元格应转为空字符串（行仍保留，列数不变）"""
    ws = _make_worksheet('Sheet1', [(None, 5), ('x', None)])
    wb = _make_workbook(ws)

    with patch('openpyxl.load_workbook', return_value=wb):
        blocks = SpreadsheetParser().parse(str(tmp_path / 'none.xlsx'))

    assert len(blocks) == 1
    assert blocks[0]['extra'] == {'rows': 2, 'cols': 2, 'sheet_name': 'Sheet1', 'format': 'xlsx'}
    assert ' | 5' in blocks[0]['content']
    assert 'x | ' in blocks[0]['content']


@patch('apps.knowledge.parsers.spreadsheet_parser.logger')
def test_parse_xlsx_open_error(mock_logger, tmp_path):
    """load_workbook 抛异常（文件损坏）：应记录异常并返回空列表"""
    with patch('openpyxl.load_workbook', side_effect=Exception('corrupt xlsx')):
        blocks = SpreadsheetParser().parse(str(tmp_path / 'bad.xlsx'))
    assert blocks == []
    mock_logger.exception.assert_called_once()


@patch('apps.knowledge.parsers.spreadsheet_parser.logger')
def test_parse_xlsx_import_error(mock_logger, tmp_path):
    """openpyxl 未安装：import 抛 ImportError，应记录错误并返回空列表"""
    with patch.dict(sys.modules, {'openpyxl': None}):
        blocks = SpreadsheetParser().parse(str(tmp_path / 'data.xlsx'))
    assert blocks == []
    mock_logger.error.assert_called_once_with('[SpreadsheetParser] openpyxl 未安装，无法解析 XLSX')


def test_parse_xlsx_long_content_truncated(tmp_path):
    """大 XLSX：表格内容超过 50000 字符时应截断"""
    ws = _make_worksheet('Sheet1', [[f'cell{i:06d}', f'value{i:06d}'] for i in range(4000)])
    wb = _make_workbook(ws)

    with patch('openpyxl.load_workbook', return_value=wb):
        blocks = SpreadsheetParser().parse(str(tmp_path / 'large.xlsx'))

    assert len(blocks) == 1
    assert len(blocks[0]['content']) == 50000
    assert blocks[0]['extra']['rows'] == 4000


# ============================================================================
# .xls 降级 / 扩展名路由
# ============================================================================
@patch('apps.knowledge.parsers.spreadsheet_parser.logger')
def test_parse_xls_fallback(mock_logger, tmp_path):
    """.xls 旧版二进制格式：openpyxl 不支持，应返回「解析提示」文本块"""
    blocks = SpreadsheetParser().parse(str(tmp_path / 'legacy.xls'))

    assert len(blocks) == 1
    b = blocks[0]
    assert b['type'] == 'text'
    assert b['section_path'] == '解析提示'
    assert '暂不支持自动解析' in b['content']
    assert b['extra'] == {'format': 'xls', 'parse_error': 'unsupported_legacy_format'}
    mock_logger.warning.assert_called()


def test_parse_et_extension(tmp_path):
    """.et（WPS 表格）按 xlsx 方式解析"""
    ws = _make_worksheet('Sheet1', [('a', 'b')])
    wb = _make_workbook(ws)

    with patch('openpyxl.load_workbook', return_value=wb) as mock_load:
        blocks = SpreadsheetParser().parse(str(tmp_path / 'data.et'))

    assert len(blocks) == 1
    assert blocks[0]['extra']['format'] == 'xlsx'
    mock_load.assert_called_once()


def test_parse_unknown_extension(tmp_path):
    """未知扩展名：按 xlsx 方式尝试解析"""
    ws = _make_worksheet('Sheet1', [('a', 'b')])
    wb = _make_workbook(ws)

    with patch('openpyxl.load_workbook', return_value=wb):
        blocks = SpreadsheetParser().parse(str(tmp_path / 'data.dat'))

    assert len(blocks) == 1
    assert blocks[0]['extra']['format'] == 'xlsx'


@patch('apps.knowledge.parsers.spreadsheet_parser.logger')
def test_parse_unknown_extension_open_error(mock_logger, tmp_path):
    """未知扩展名且 xlsx 打开失败：返回空列表"""
    with patch('openpyxl.load_workbook', side_effect=Exception('corrupt')):
        blocks = SpreadsheetParser().parse(str(tmp_path / 'data.unk'))
    assert blocks == []
