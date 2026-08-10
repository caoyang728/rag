"""
apps.knowledge.parsers.pdf_parser 单元测试 —— PDF 解析器

覆盖范围：
- parse 主流程：多页 / 单页 / 空 PDF / 扫描件（纯图片）检测 / 打开失败 / PyMuPDF 未安装
- 页眉页脚检测与过滤、多栏布局检测与重排、跨页句子合并（影响章节识别）
- 章节标题识别（正则 + 字体特征）、表格提取与跨页表格合并
- 文本块提取（含表格区域排除）、图片提取（含异常降级）
- 各辅助方法（_pre_scan / _detect_multi_column / _detect_header_footer 等）

用纯 pytest + mock（不依赖 DB）：
解析器输入是文件路径 + 外部库（PyMuPDF），通过 patch 替换 sys.modules['fitz']
即可完全模拟各种 PDF 形态，无需真实 PDF 文件，也不涉及 ORM。

已知实现缺陷（仅测试如实记录，不修改源码）：
1. 图片提取的 NameError（fitz 局部 import）缺陷已修复：fitz 改为 _get_fitz()
   延迟获取，端到端图片提取可成功（见 test_parse_image_extraction）。
2. 多栏重排与跨页句子合并的结果只影响章节识别，不影响最终文本块内容
   （_extract_text_blocks 内部会基于原始 text 重新切行），见对应测试注释。
3. parse 不提供 page-range / chunk_size 选项（切片在 chunker 中处理），
   传入额外 options 会被静默忽略（见 test_parse_extra_options_ignored）。
"""
import base64
import sys
from unittest.mock import MagicMock, patch

import pytest

from apps.knowledge.parsers.pdf_parser import PDFParser


# ============================================================================
# 测试辅助：构造 page / doc 的 mock
# ============================================================================
def _word(text, x0, y0, size=10.0, font='Body', bold=False):
    """构造 fitz get_text('words') 返回的单词元组（x0,y0,x1,y1,text,size,font,flags）"""
    font_name = 'Bold-' + font if bold else font
    return (x0, y0, x0 + 20.0, y0 + 5.0, text, size, font_name, 0)


def _make_page(text='', words=None, tables=None, images=None, is_inside=None):
    """构造一个模拟的 fitz 页面对象"""
    page = MagicMock()
    # is_cross_page 计算需要 page.rect 的数值边界（y0/y1）
    page.rect = MagicMock(y0=0, y1=842)
    word_tuples = list(words) if words is not None else []

    def _get_text(mode):
        if mode == 'text':
            return text
        return word_tuples

    page.get_text.side_effect = _get_text
    page.find_tables.return_value = MagicMock(tables=list(tables) if tables is not None else [])
    page.get_images.return_value = list(images) if images is not None else []
    if is_inside is not None:
        page.is_inside.side_effect = is_inside
    return page


def _make_doc(pages):
    """构造可重复迭代的文档对象（parse 中 _pre_scan 与主循环各迭代一次，必须每次返回新迭代器）"""
    doc = MagicMock()
    doc.__iter__.side_effect = lambda: iter(pages)
    return doc


def _run_parse(parser, pages, open_error=None):
    """模拟 fitz 环境并执行 parse：默认 open 返回构造的 doc，open_error 用于注入打开失败"""
    fitz_mock = MagicMock()
    if open_error is None:
        fitz_mock.open.return_value = _make_doc(pages)
    else:
        fitz_mock.open.side_effect = open_error
    with patch.dict('sys.modules', {'fitz': fitz_mock}):
        return parser.parse('/tmp/sample.pdf')


def _make_table(content='| h1 | h2 |\n| a | b |', has_header=True, rows=2, cols=2,
                is_spanning=False):
    """构造一个模拟的 PyMuPDF 1.28 表格对象（row_count/col_count/bbox API）"""
    tab = MagicMock()
    tab.to_markdown.return_value = content
    tab.header = None if not has_header else MagicMock()
    tab.row_count = rows
    tab.col_count = cols
    # is_cross_page 由 bbox 是否超出页面上下边界计算得出（页面高 842）
    tab.bbox = (50, 100, 500, 900) if is_spanning else (50, 100, 500, 220)
    return tab


# ============================================================================
# parse 主流程
# ============================================================================
@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_normal_multipage(mock_logger):
    """多页 PDF：每页文本应生成一个 text block，page_number 从 1 递增"""
    parser = PDFParser()
    pages = [_make_page(text=f'第{i}页正文内容，这是测试段落。') for i in range(3)]
    blocks = _run_parse(parser, pages)

    assert len(blocks) == 3
    assert all(b['type'] == 'text' for b in blocks)
    assert blocks[0]['content'] == '第0页正文内容，这是测试段落。'
    assert [b['page_number'] for b in blocks] == [1, 2, 3]
    assert all(b['section_path'] == '' for b in blocks)


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_single_page(mock_logger):
    """单页 PDF：页眉页脚检测因页数 < 3 跳过，正常输出一个文本块"""
    parser = PDFParser()
    blocks = _run_parse(parser, [_make_page(text='单页内容，仅此一段。')])
    assert len(blocks) == 1
    assert blocks[0]['content'] == '单页内容，仅此一段。'
    assert blocks[0]['page_number'] == 1


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_empty_pdf(mock_logger):
    """空 PDF（每页无可提取文本）：不产出任何 block"""
    parser = PDFParser()
    blocks = _run_parse(parser, [_make_page(text=''), _make_page(text='')])
    assert blocks == []


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_scanned_pdf_warning(mock_logger):
    """扫描件（纯图片）PDF：文本占比低于阈值，应记录 warning 提示 OCR"""
    parser = PDFParser()
    # 每页仅 20 字符，avg/5000 = 0.004 < 0.01，触发扫描件判定
    blocks = _run_parse(parser, [_make_page(text='x' * 20)])
    assert len(blocks) == 1
    mock_logger.warning.assert_called_once_with('[PDFParser] 检测到扫描件PDF，文本提取可能不完整')


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_open_error(mock_logger):
    """fitz.open 抛异常（文件损坏/加密）：应记录异常并返回空列表"""
    parser = PDFParser()
    blocks = _run_parse(parser, [], open_error=Exception('corrupt pdf'))
    assert blocks == []
    mock_logger.exception.assert_called_once()


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_fitz_not_installed(mock_logger):
    """PyMuPDF 未安装：import fitz 抛 ImportError，应记录错误并返回空列表"""
    parser = PDFParser()
    # sys.modules 中 fitz 置为 None 会让 import fitz 抛 ImportError
    with patch.dict('sys.modules', {'fitz': None}):
        blocks = parser.parse('/tmp/sample.pdf')
    assert blocks == []
    mock_logger.error.assert_called_once_with('[PDFParser] PyMuPDF未安装，无法解析PDF')


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_extra_options_ignored(mock_logger):
    """parse 接受任意 **options（page-range / chunk_size 等）但实现中无对应逻辑，
    传入后行为不变——切片/分块由 chunker 模块处理，此处如实记录"""
    parser = PDFParser()
    pages = [_make_page(text=f'第{i}页。') for i in range(2)]
    with patch.dict('sys.modules', {'fitz': MagicMock(open=MagicMock(return_value=_make_doc(pages)))}):
        blocks = parser.parse('/tmp/sample.pdf', max_pages=1, chunk_size=10, overlap=5)
    # 无页面数限制：仍返回 2 个 block
    assert len(blocks) == 2
    assert blocks[1]['page_number'] == 2


# ============================================================================
# parse：页眉页脚过滤
# ============================================================================
@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_header_footer_filtered(mock_logger):
    """页眉页脚过滤：3 页相同页眉「公司机密文件」与页脚「第 1 页」应被剔除，
    仅保留正文行"""
    parser = PDFParser()
    pages = [
        _make_page(text=f'公司机密文件\n这是第{i}页的正文内容，测试段落。\n补充说明文字。\n第 1 页')
        for i in range(3)
    ]
    blocks = _run_parse(parser, pages)

    assert len(blocks) == 3
    for b in blocks:
        # 页眉页脚行均被过滤，只保留正文两行（合并为同一个文本块）
        assert '公司机密文件' not in b['content']
        assert '第 1 页' not in b['content']
        assert '正文内容' in b['content']
        assert '补充说明文字' in b['content']


# ============================================================================
# parse：多栏布局
# ============================================================================
@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_multi_column_detected(mock_logger):
    """多栏布局：单词 x 坐标分布分散（左栏 x≈10 / 右栏 x≈400）应触发多栏检测
    注意：多栏重排只影响章节识别，最终文本块内容仍来自原始 text
    （_extract_text_blocks 内部重新按原始文本切行，属实现现状）"""
    parser = PDFParser()
    words = [_word(f'L{i}', 10.0, i * 10.0) for i in range(5)] + \
            [_word(f'R{i}', 400.0, i * 10.0) for i in range(5)]
    page = _make_page(text='L0 L1 L2 L3 L4 R0 R1 R2 R3 R4', words=words)
    blocks = _run_parse(parser, [page])

    mock_logger.info.assert_called_once_with('[PDFParser] 检测到多栏布局')
    assert len(blocks) == 1
    assert blocks[0]['content'] == 'L0 L1 L2 L3 L4 R0 R1 R2 R3 R4'


# ============================================================================
# parse：章节标题识别 + 跨页句子合并
# ============================================================================
@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_heading_section(mock_logger):
    """章节标题：正则命中的标题行（第一章 绪论）应更新 section_path 并传递给文本块"""
    parser = PDFParser()
    page = _make_page(text='第一章 绪论\n这是第一段的正文内容。')
    blocks = _run_parse(parser, [page])

    assert len(blocks) == 1
    assert blocks[0]['section_path'] == '第一章 绪论'
    assert '这是第一段的正文内容。' in blocks[0]['content']


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_cross_page_merge_affects_section(mock_logger):
    """跨页句子合并：上一页末尾「第一章 绪论」（无终止标点）应与下一页开头
    「的内容如下。」拼接后再做章节识别——合并只影响章节/标题判定，不影响
    文本块内容本身（文本块由各页原始 text 生成）"""
    parser = PDFParser()
    page1 = _make_page(text='这是第一页的内容。\n第一章 绪论')
    page2 = _make_page(text='的内容如下。\n正文开始，这是第二页的内容。')
    blocks = _run_parse(parser, [page1, page2])

    assert len(blocks) == 2
    # 第一页自身先识别到「第一章 绪论」
    assert blocks[0]['section_path'] == '第一章 绪论'
    # 第二页开头的合并行「第一章 绪论 的内容如下。」命中章节正则
    assert blocks[1]['section_path'] == '第一章 绪论 的内容如下。'
    # 文本块内容仍是第二页原始文本（未包含拼接后的上一页末尾）
    assert blocks[1]['content'].startswith('的内容如下。')


# ============================================================================
# parse：表格提取与跨页表格合并
# ============================================================================
@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_cross_page_table_merge(mock_logger):
    """跨页表格合并：第 2 页表格无页眉且首行与第 1 页页眉相同（延续），
    应合并为单个 table block，并标注 is_cross_page / pages"""
    parser = PDFParser()
    tab1 = _make_table(content='| h1 | h2 |\n| a | b |', has_header=True, is_spanning=True)
    tab2 = _make_table(content='| h1 | h2 |\n| c | d |', has_header=False, is_spanning=True)
    pages = [
        _make_page(text='', tables=[tab1]),
        _make_page(text='', tables=[tab2]),
    ]
    blocks = _run_parse(parser, pages)

    assert len(blocks) == 1
    table = blocks[0]
    assert table['type'] == 'table'
    assert table['content'] == '| h1 | h2 |\n| a | b |\n| c | d |'
    assert table['extra']['is_cross_page'] is True
    assert table['extra']['rows'] == 4
    assert table['extra']['cols'] == 2
    assert table['extra']['has_header'] is True
    assert table['extra']['pages'] == [1, 2]


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_parse_image_extraction(mock_logger):
    """图片提取（端到端）：fitz 通过 _get_fitz() 延迟获取，图片应成功提取为
    base64 数据（修复前因模块级 fitz 缺失会抛 NameError 而降级为空 base64）"""
    parser = PDFParser()
    pix = MagicMock()
    pix.n = 4
    pix.width = 100
    pix.height = 50
    pix.tobytes.return_value = b'PNGDATA'
    fitz_mock = MagicMock()
    fitz_mock.Pixmap.return_value = pix
    with patch.dict('sys.modules', {'fitz': fitz_mock}):
        doc = _make_doc([_make_page(text='', images=[(1, 0, 0, 0, 0, 0, 0)])])
        fitz_mock.open.return_value = doc
        blocks = parser.parse('/tmp/sample.pdf')

    assert len(blocks) == 1
    img = blocks[0]
    assert img['type'] == 'image'
    assert img['content'] == '[图片 P1#1]'
    assert img['extra']['base64_data'] == base64.b64encode(b'PNGDATA').decode('utf-8')
    assert img['extra']['width'] == 100
    assert img['extra']['height'] == 50


# ============================================================================
# _pre_scan
# ============================================================================
@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_pre_scan(mock_logger):
    """预扫描：应返回页面纯文本列表与单词位置信息（含字体/加粗特征）"""
    parser = PDFParser()
    page = _make_page(text='页A文本', words=[_word('Hello', 10.0, 20.0, size=12.0, font='Body', bold=True)])
    texts, word_infos = parser._pre_scan(_make_doc([page]))

    assert texts == ['页A文本']
    assert len(word_infos) == 1
    w = word_infos[0][0]
    assert w == {
        'text': 'Hello',
        'x0': 10.0,
        'y0': 20.0,
        'font_size': 12.0,
        'font_name': 'Bold-Body',
        'bold': True,
    }


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_pre_scan_words_error_ignored(mock_logger):
    """预扫描容错：get_text('words') 抛异常时应跳过该页单词信息，不影响文本提取"""
    parser = PDFParser()
    page = MagicMock()

    def _boom():
        raise Exception('boom')

    page.get_text.side_effect = lambda mode: ('正文' if mode == 'text' else _boom())
    page.find_tables.return_value = MagicMock(tables=[])
    page.get_images.return_value = []
    texts, word_infos = parser._pre_scan(_make_doc([page]))

    assert texts == ['正文']
    assert word_infos == [[]]


# ============================================================================
# 多栏布局检测与重排
# ============================================================================
def test_detect_multi_column_empty():
    """无单词信息时不应判定为多栏"""
    parser = PDFParser()
    assert parser._detect_multi_column([]) is False
    assert parser._detect_multi_column([[]]) is False


def test_detect_multi_column_too_few_words():
    """单词总数 < 10 时无法做标准差统计，判定为非多栏"""
    parser = PDFParser()
    word_infos = [[{'text': f'w{i}', 'x0': 10.0, 'y0': float(i)} for i in range(5)]]
    assert parser._detect_multi_column(word_infos) is False


def test_detect_multi_column_clustered():
    """x 坐标聚拢（所有 x0 相同，标准差为 0）时判定为非多栏"""
    parser = PDFParser()
    word_infos = [[{'text': f'w{i}', 'x0': 10.0, 'y0': float(i)} for i in range(10)]]
    assert parser._detect_multi_column(word_infos) is False


def test_detect_multi_column_spread():
    """x 坐标分两栏（x0=10 与 x0=400，标准差超过页宽 10%）时判定为多栏"""
    parser = PDFParser()
    words = [{'text': f'L{i}', 'x0': 10.0, 'y0': float(i)} for i in range(5)] + \
            [{'text': f'R{i}', 'x0': 400.0, 'y0': float(i)} for i in range(5)]
    assert parser._detect_multi_column([words]) is True


def test_reorder_multi_column_no_word_info():
    """无单词位置信息时原样返回行列表"""
    parser = PDFParser()
    lines = ['第一行', '第二行']
    assert parser._reorder_multi_column(lines, []) == lines
    assert parser._reorder_multi_column(lines, None) == lines


def test_reorder_multi_column_two_columns():
    """多栏重排：按 x 坐标分左右栏、按 y 坐标排序后先左后右拼接"""
    parser = PDFParser()
    word_info = [
        {'text': 'L0', 'x0': 10.0, 'y0': 30.0},
        {'text': 'L1', 'x0': 10.0, 'y0': 10.0},
        {'text': 'R0', 'x0': 400.0, 'y0': 20.0},
        {'text': 'R1', 'x0': 400.0, 'y0': 5.0},
    ]
    out = parser._reorder_multi_column(['ignored'], word_info)
    assert out == ['L1 L0', 'R1 R0']


def test_reorder_multi_column_missing_x0():
    """单词缺少 x0 坐标（字典结构不完整）时：列表推导直接抛 KeyError，
    （源码中 `if not x_coords` 兜底分支实际不可达，属于防御性死代码）"""
    parser = PDFParser()
    word_info = [{'text': 'w1'}, {'text': 'w2'}]  # 无 x0 键
    with pytest.raises(KeyError):
        parser._reorder_multi_column(['行'], word_info)


# ============================================================================
# 页眉页脚检测与过滤
# ============================================================================
def test_detect_header_footer_too_few_pages():
    """页数 < 3 时不检测页眉页脚（无法统计频率）"""
    parser = PDFParser()
    assert parser._detect_header_footer(['a', 'b']) == ([], [])


def test_detect_header_footer_repeated_lines():
    """重复出现在超过 60% 页面的短行应被识别为页眉/页脚"""
    parser = PDFParser()
    page_texts = [f'公司机密\n正文第{i}段。\n第 1 页' for i in range(3)]
    header_lines, footer_lines = parser._detect_header_footer(page_texts)
    # 3 行页面下，前 3 行与后 3 行完全重叠，故公司机密与第 1 页同时进入两类候选
    assert '公司机密' in header_lines
    assert '第 1 页' in footer_lines


def test_detect_header_footer_below_threshold():
    """每页行均不同（出现频率低于阈值）时不产生页眉页脚"""
    parser = PDFParser()
    page_texts = [f'独特行{i}。\n正文内容第{i}段。' for i in range(5)]
    header_lines, footer_lines = parser._detect_header_footer(page_texts)
    assert header_lines == []
    assert footer_lines == []


def test_detect_header_footer_empty_page():
    """存在空白页（无文本行）时跳过该页统计，不影响其他页面检测"""
    parser = PDFParser()
    page_texts = ['公司机密\n正文。', '', '公司机密\n正文2。']
    header_lines, footer_lines = parser._detect_header_footer(page_texts)
    # 公司机密出现 2 次 >= 阈值 2
    assert '公司机密' in header_lines


def test_is_likely_header_footer():
    """页眉页脚候选判定：页码模式 / 短文本无标点 → True；过长或含句末标点 → False"""
    parser = PDFParser()
    assert parser._is_likely_header_footer('第 1 页') is True
    assert parser._is_likely_header_footer('1/10') is True
    assert parser._is_likely_header_footer('Page 3') is True
    assert parser._is_likely_header_footer('3-5') is True
    assert parser._is_likely_header_footer('x') is False       # 过短
    assert parser._is_likely_header_footer('a' * 61) is False  # 过长
    assert parser._is_likely_header_footer('完整句子。') is False  # 含句末标点
    assert parser._is_likely_header_footer('公司机密') is True     # 短文本无标点


def test_filter_header_footer():
    """过滤页眉页脚：命中列表的行被剔除，其余行保留"""
    parser = PDFParser()
    text = '公司机密\n这是正文。\n第 1 页'
    out = parser._filter_header_footer(text, ['公司机密'], ['第 1 页'])
    assert out == '这是正文。'


# ============================================================================
# 跨页句子合并 / 扫描件检测
# ============================================================================
def test_detect_trailing_incomplete():
    """末尾不完整句检测：空文本 / 以终止标点结尾 / 超长行 → 不合并；短行无标点 → 合并"""
    parser = PDFParser()
    assert parser._detect_trailing_incomplete('') == ''
    assert parser._detect_trailing_incomplete('完整句子。') == ''
    assert parser._detect_trailing_incomplete('短句未完成') == '短句未完成'
    # 超过阈值(100)的行视为完整段落，不合并
    assert parser._detect_trailing_incomplete('x' * 100) == ''
    # 取最后一行判断
    assert parser._detect_trailing_incomplete('第一行。\n末尾未完成') == '末尾未完成'


def test_detect_scanned_pdf():
    """扫描件检测：无页面 / 每页平均文本占比低于 1% → True；正常文本 → False"""
    parser = PDFParser()
    assert parser._detect_scanned_pdf([], MagicMock()) is False
    assert parser._detect_scanned_pdf([''], MagicMock()) is True            # 空页
    assert parser._detect_scanned_pdf(['x' * 20], MagicMock()) is True       # 每页 20 字符
    assert parser._detect_scanned_pdf(['x' * 5000], MagicMock()) is False    # 每页 5000 字符


# ============================================================================
# 章节标题识别
# ============================================================================
def test_match_heading_pattern():
    """标题正则：第X章 / 编号小节 / 全大写短语命中；普通段落不命中"""
    parser = PDFParser()
    assert parser._match_heading_pattern('第一章 绪论') is True
    assert parser._match_heading_pattern('1.2 背景介绍') is True
    assert parser._match_heading_pattern('INTRO SECTION') is True
    assert parser._match_heading_pattern('普通段落内容。') is False


def test_detect_heading_by_font_too_few_words():
    """单词总数 < 10 时无法估计正文平均字号，判定非标题"""
    parser = PDFParser()
    word_info = [{'text': '标题', 'font_size': 20.0, 'bold': False}]
    assert parser._detect_heading_by_font('标题', word_info) is False


def test_detect_heading_by_font_empty_word_info():
    """无任何单词信息时直接判定非标题"""
    parser = PDFParser()
    assert parser._detect_heading_by_font('标题', []) is False


def test_detect_heading_by_font_no_matching_words():
    """当前行没有匹配的单词位置信息时判定非标题"""
    parser = PDFParser()
    word_info = [{'text': '正文', 'font_size': 10.0, 'bold': False} for _ in range(10)]
    assert parser._detect_heading_by_font('不存在的行', word_info) is False


def test_detect_heading_by_font_larger_size():
    """字号明显大于正文（≥ 1.3 倍）应判定为标题"""
    parser = PDFParser()
    word_info = [{'text': '正文', 'font_size': 10.0, 'bold': False} for _ in range(9)]
    word_info.append({'text': '大标题', 'font_size': 20.0, 'bold': False})
    assert parser._detect_heading_by_font('大标题', word_info) is True


def test_detect_heading_by_font_bold():
    """加粗且字号不小于正文平均字号应判定为标题"""
    parser = PDFParser()
    word_info = [{'text': '正文', 'font_size': 10.0, 'bold': False} for _ in range(9)]
    word_info.append({'text': '加粗词', 'font_size': 10.0, 'bold': True})
    assert parser._detect_heading_by_font('加粗词', word_info) is True


def test_detect_heading_by_font_plain():
    """普通字号且不加粗不应判定为标题"""
    parser = PDFParser()
    word_info = [{'text': '正文', 'font_size': 10.0, 'bold': False} for _ in range(9)]
    word_info.append({'text': '普通行', 'font_size': 10.0, 'bold': False})
    assert parser._detect_heading_by_font('普通行', word_info) is False


def test_is_heading():
    """标题判定：正则命中或字体特征命中即为标题"""
    parser = PDFParser()
    assert parser._is_heading('第一章 绪论', []) is True
    # 正则未命中但字体特征命中
    word_info = [{'text': '正文', 'font_size': 10.0, 'bold': False} for _ in range(9)]
    word_info.append({'text': '重大发现', 'font_size': 20.0, 'bold': False})
    assert parser._is_heading('重大发现', word_info) is True
    # 两者均未命中
    assert parser._is_heading('普通正文。', []) is False


# ============================================================================
# 表格提取
# ============================================================================
@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_extract_tables_normal(mock_logger):
    """表格提取：应保留 to_markdown 内容与行/列/页眉/跨页等结构信息"""
    parser = PDFParser()
    tab = _make_table(content='| a | b |\n| 1 | 2 |', has_header=True, rows=2, cols=2, is_spanning=True)
    page = MagicMock()
    page.rect = MagicMock(y0=0, y1=842)  # is_cross_page 计算需要数值边界
    page.find_tables.return_value = MagicMock(tables=[tab])
    tables = parser._extract_tables(page, 3, '章节')

    assert len(tables) == 1
    t = tables[0]
    assert t['type'] == 'table'
    assert t['content'] == '| a | b |\n| 1 | 2 |'
    assert t['section_path'] == '章节'
    assert t['page_number'] == 3
    assert t['extra']['rows'] == 2
    assert t['extra']['cols'] == 2
    assert t['extra']['has_header'] is True
    assert t['extra']['is_cross_page'] is True
    assert t['extra']['merge_info'] == []


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_extract_tables_error(mock_logger):
    """表格提取异常：find_tables 抛异常时应降级为空列表并记录 warning"""
    parser = PDFParser()
    page = MagicMock()
    page.find_tables.side_effect = Exception('no table support')
    assert parser._extract_tables(page, 1, '') == []
    mock_logger.warning.assert_called_once()


def test_extract_merge_info_returns_empty():
    """合并单元格信息提取：extract 网格中没有 None 单元格（无合并）时返回空列表"""
    parser = PDFParser()
    tab = MagicMock()
    tab.extract.return_value = [['a', 'b'], ['c', 'd']]
    assert parser._extract_merge_info(tab) == []


def test_extract_merge_info_merged_cell():
    """存在合并单元格（extract 网格中出现 None）时记录其位置"""
    parser = PDFParser()
    tab = MagicMock()
    tab.extract.return_value = [['a', None], ['c', 'd']]
    assert parser._extract_merge_info(tab) == [{'row': 0, 'col': 1, 'span': 'merged'}]


def test_extract_merge_info_exception():
    """遍历表格单元格抛异常时静默降级，返回空列表"""
    parser = PDFParser()
    tab = MagicMock()
    tab.extract.side_effect = Exception('boom')
    assert parser._extract_merge_info(tab) == []


# ============================================================================
# 文本块提取
# ============================================================================
@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_extract_text_blocks_basic(mock_logger):
    """文本块提取：多行连续文本合并为一个 block，带页码/章节/序号信息"""
    parser = PDFParser()
    page = MagicMock()
    page.find_tables.return_value = MagicMock(tables=[])
    blocks = parser._extract_text_blocks(page, '第一行\n第二行', [], [], 2, '章节', [])

    assert len(blocks) == 1
    assert blocks[0]['content'] == '第一行\n第二行'
    assert blocks[0]['page_number'] == 2
    assert blocks[0]['section_path'] == '章节'
    assert blocks[0]['extra'] == {'source': 'pdf', 'text_block_index': 0}


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_extract_text_blocks_exclude_table_region(mock_logger):
    """表格区域排除：单词位置落在表格 bbox 内的行不进入文本块"""
    parser = PDFParser()
    tab = MagicMock()
    tab.bbox = (0, 0, 200, 200)
    page = MagicMock()
    page.find_tables.return_value = MagicMock(tables=[tab])
    page.is_inside.side_effect = lambda pt, rect: pt[0] < 100  # x0 < 100 视为表格内
    word_info = [
        {'text': '表格内文本', 'x0': 50.0, 'y0': 10.0},
        {'text': '表格外文本', 'x0': 500.0, 'y0': 10.0},
    ]
    blocks = parser._extract_text_blocks(page, '表格内文本\n表格外文本', [], [], 1, '', word_info)

    assert len(blocks) == 1
    assert blocks[0]['content'] == '表格外文本'


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_extract_text_blocks_table_region_closes_block(mock_logger):
    """表格区域前已有文本：遇到表格行时应结束当前文本块（丢弃表格内行）"""
    parser = PDFParser()
    tab = MagicMock()
    tab.bbox = (0, 0, 200, 200)
    page = MagicMock()
    page.find_tables.return_value = MagicMock(tables=[tab])
    page.is_inside.side_effect = lambda pt, rect: pt[0] < 100  # x0 < 100 视为表格内
    word_info = [
        {'text': '普通文本', 'x0': 500.0, 'y0': 5.0},
        {'text': '表格内文本', 'x0': 50.0, 'y0': 10.0},
    ]
    blocks = parser._extract_text_blocks(page, '普通文本\n表格内文本', [], [], 1, '', word_info)

    assert len(blocks) == 1
    assert blocks[0]['content'] == '普通文本'


@patch('apps.knowledge.parsers.pdf_parser.logger')
def test_extract_text_blocks_find_tables_error(mock_logger):
    """find_tables 抛异常时降级：表格区域视为空，所有行进入文本块"""
    parser = PDFParser()
    page = MagicMock()
    page.find_tables.side_effect = Exception('boom')
    blocks = parser._extract_text_blocks(page, '第一行\n第二行', [], [], 1, '章节', [])
    assert len(blocks) == 1
    assert blocks[0]['content'] == '第一行\n第二行'


# ============================================================================
# 图片提取（直接打桩模块级 fitz，覆盖成功路径与异常路径）
# ============================================================================
def _run_extract_images(parser, pixmap_behavior):
    """直接调用 _extract_images 并打桩模块属性 fitz（create=True）：
    _extract_images 通过 _get_fitz() 读取模块级 fitz，这里打桩以测试方法逻辑
    本身（成功路径）；端到端 parse 层路径见 test_parse_image_extraction"""
    fitz_mock = MagicMock()
    fitz_mock.csRGB = 'csRGB'
    fitz_mock.Pixmap.side_effect = pixmap_behavior
    page = MagicMock()
    page.get_images.return_value = [(1, 0, 0, 0, 0, 0, 0)]
    pdf_module = sys.modules['apps.knowledge.parsers.pdf_parser']
    with patch.object(pdf_module, 'fitz', fitz_mock, create=True):
        return parser._extract_images(page, MagicMock(), 1, '章节')


def test_extract_images_success():
    """图片提取成功：RGBA/灰度图（n=4）直接转 PNG 并 base64 编码"""
    parser = PDFParser()
    pix = MagicMock()
    pix.n = 4
    pix.width = 100
    pix.height = 50
    pix.tobytes.return_value = b'PNGDATA'
    blocks = _run_extract_images(parser, lambda doc, xref: pix)

    assert len(blocks) == 1
    img = blocks[0]
    assert img['type'] == 'image'
    assert img['content'] == '[图片 P1#1]'
    assert img['extra']['base64_data'] == base64.b64encode(b'PNGDATA').decode('utf-8')
    assert img['extra']['width'] == 100
    assert img['extra']['height'] == 50
    assert img['extra']['size_bytes'] == len(b'PNGDATA')
    assert img['extra']['mime_type'] == 'image/png'


def test_extract_images_transparency_conversion():
    """透明背景图（n >= 5，如 CMYK+Alpha）：应转换为 RGB 后再编码"""
    parser = PDFParser()
    pix_original = MagicMock()
    pix_original.n = 5
    pix_converted = MagicMock()
    pix_converted.n = 3
    pix_converted.width = 10
    pix_converted.height = 20
    pix_converted.tobytes.return_value = b'RGBDATA'
    calls = iter([pix_original, pix_converted])
    blocks = _run_extract_images(parser, lambda doc, xref: next(calls))

    assert len(blocks) == 1
    assert blocks[0]['extra']['base64_data'] == base64.b64encode(b'RGBDATA').decode('utf-8')
    assert blocks[0]['extra']['size_bytes'] == len(b'RGBDATA')


def test_extract_images_error():
    """图片提取异常：单图失败不中断，降级为带 error 信息的空 base64 图片块"""
    parser = PDFParser()
    blocks = _run_extract_images(parser, Exception('bad pix'))
    assert len(blocks) == 1
    img = blocks[0]
    assert img['content'] == '[图片 P1#1 xref=1]'
    assert img['extra']['base64_data'] == ''
    assert img['extra']['error'] == 'bad pix'
    assert img['extra']['width'] == 0


# ============================================================================
# 跨页表格合并辅助方法
# ============================================================================
def _table_block(content, page_number, section='章节', has_header=True, cols=2, rows=2):
    """构造 _merge_cross_page_tables 使用的 table block 字典"""
    return {
        'type': 'table',
        'content': content,
        'section_path': section,
        'page_number': page_number,
        'extra': {'has_header': has_header, 'cols': cols, 'rows': rows},
    }


def test_merge_cross_page_tables_single():
    """少于 2 个表格时原样返回"""
    parser = PDFParser()
    one = [_table_block('h', 1)]
    assert parser._merge_cross_page_tables(one) == one
    assert parser._merge_cross_page_tables([]) == []


def test_merge_cross_page_tables_merge():
    """同一跨页表格应合并为一个"""
    parser = PDFParser()
    t1 = _table_block('| h1 | h2 |\n| a | b |', 1, has_header=True)
    t2 = _table_block('| h1 | h2 |\n| c | d |', 2, has_header=False)
    merged = parser._merge_cross_page_tables([t1, t2])
    assert len(merged) == 1
    assert merged[0]['extra']['is_cross_page'] is True
    assert merged[0]['content'] == '| h1 | h2 |\n| a | b |\n| c | d |'


def test_merge_cross_page_tables_not_same():
    """非同一表格（不同列数）不应合并"""
    parser = PDFParser()
    t1 = _table_block('| h1 | h2 |\n| a | b |', 1, has_header=True, cols=2)
    t2 = _table_block('| h1 | h2 | h3 |', 2, has_header=True, cols=3)
    merged = parser._merge_cross_page_tables([t1, t2])
    assert len(merged) == 2


def test_merge_cross_page_tables_chain():
    """连续多页同一表格应链式合并（合并结果继续与后续页表格比对）"""
    parser = PDFParser()
    t1 = _table_block('| h1 | h2 |\n| a | b |', 1, has_header=True)
    t2 = _table_block('| h1 | h2 |\n| c | d |', 2, has_header=False)
    t3 = _table_block('| h1 | h2 |\n| e | f |', 2, has_header=False)
    merged = parser._merge_cross_page_tables([t1, t2, t3])
    assert len(merged) == 1
    assert merged[0]['extra']['rows'] == 6
    assert merged[0]['content'] == '| h1 | h2 |\n| a | b |\n| c | d |\n| e | f |'


def test_merge_cross_page_tables_chain_break():
    """链式合并中断：后续表格不是同一表格（页码不连续）时停止合并，剩余表格单独输出"""
    parser = PDFParser()
    t1 = _table_block('| h1 | h2 |\n| a | b |', 1, has_header=True)
    t2 = _table_block('| h1 | h2 |\n| c | d |', 2, has_header=False)
    t3 = _table_block('| h1 | h2 |\n| e | f |', 3, has_header=False)
    merged = parser._merge_cross_page_tables([t1, t2, t3])
    assert len(merged) == 2
    assert merged[0]['extra']['rows'] == 4
    assert merged[1] == t3


def test_is_same_table():
    """跨页表格判定：章节/页码连续/列数/页眉重复任一不满足则不是同一表格"""
    parser = PDFParser()
    t1 = _table_block('| h1 | h2 |\n| a | b |', 1, section='S1')
    t2 = _table_block('| h1 | h2 |\n| c | d |', 2, section='S1', has_header=False)

    assert parser._is_same_table(t1, t2) is True  # 第二页无页眉 → 延续

    # 章节不同
    t3 = _table_block('| h1 | h2 |', 2, section='S2', has_header=False)
    assert parser._is_same_table(t1, t3) is False

    # 页码不连续
    t4 = _table_block('| h1 | h2 |', 3, section='S1', has_header=False)
    assert parser._is_same_table(t1, t4) is False

    # 列数不同
    t5 = _table_block('| h1 | h2 | h3 |', 2, section='S1', has_header=False, cols=3)
    assert parser._is_same_table(t1, t5) is False

    # 内容行数不足（无法取页眉）
    t6 = {'type': 'table', 'content': '| x |', 'section_path': 'S1', 'page_number': 2,
          'extra': {'has_header': False, 'cols': 2, 'rows': 1}}
    assert parser._is_same_table(t1, t6) is False

    # 第二页带页眉且与第一页相同 → 重复页眉，同一表格
    t7 = _table_block('| h1 | h2 |\n| e | f |', 2, section='S1', has_header=True)
    assert parser._is_same_table(t1, t7) is True

    # 第二页带不同页眉且第一页无页眉 → 非同一表格（需保持同一章节，才能走到页眉比较逻辑）
    t8 = _table_block('| h3 | h4 |\n| g | h |', 2, section='S1', has_header=True)
    t1_no_header = _table_block('| h1 | h2 |\n| a | b |', 1, section='S1', has_header=False)
    assert parser._is_same_table(t1_no_header, t8) is False

    # 第二页无页眉且首行与第一页页眉不同 → 仍视为同一表格的延续（无页眉即续接）
    t9 = _table_block('| x | y |\n| m | n |', 2, section='S1', has_header=False)
    assert parser._is_same_table(t1, t9) is True


def test_merge_two_tables():
    """两个表格合并：去除重复页眉、内容拼接、行数累加、页码归并"""
    parser = PDFParser()
    t1 = _table_block('| h1 | h2 |\n| a | b |', 1, has_header=True)
    t2 = _table_block('| h1 | h2 |\n| c | d |', 2, has_header=False)
    merged = parser._merge_two_tables(t1, t2)

    assert merged['content'] == '| h1 | h2 |\n| a | b |\n| c | d |'
    assert merged['extra']['rows'] == 4
    assert merged['extra']['cols'] == 2
    assert merged['extra']['has_header'] is True
    assert merged['extra']['is_cross_page'] is True
    assert merged['extra']['pages'] == [1, 2]


def test_merge_pages():
    """页码归并：无历史页时从当前页开始；已有历史页时追加不重复"""
    parser = PDFParser()
    assert parser._merge_pages({}, {}, 1, 2) == [1, 2]
    assert parser._merge_pages({'pages': [1]}, {}, 1, 2) == [1, 2]
    assert parser._merge_pages({'pages': [1, 2]}, {}, 1, 3) == [1, 2, 3]
    # 页码已存在时不重复添加
    assert parser._merge_pages({'pages': [1, 2]}, {}, 1, 2) == [1, 2]
