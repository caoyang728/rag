"""
apps.knowledge.parsers.markdown_parser 单元测试 —— Markdown / TXT 解析器

覆盖范围：
- 标题/正文块切分：正文归入最近标题的 section_path
- 多级标题层级栈：h1 → h2 → 回退 h1 的 section_path 拼接
- 首段无标题正文的 section_path 为空
- 空内容块跳过、长标题截断 32 字符
- 空文件 / 文件不存在（读取失败）返回空列表

用纯 pytest + tmp_path（不依赖 DB）。
"""
import pytest

from apps.knowledge.parsers.markdown_parser import MarkdownParser


@pytest.mark.unit
def test_parse_heading_and_body(tmp_path):
    """标题下的正文应成块，section_path 为标题（标题自身不成块）"""
    md_file = tmp_path / 'a.md'
    md_file.write_text('# 简介\n这是正文内容。\n', encoding='utf-8')

    blocks = MarkdownParser().parse(str(md_file))

    assert len(blocks) == 1
    assert blocks[0]['section_path'] == '简介'
    assert blocks[0]['content'] == '这是正文内容。'
    assert blocks[0]['type'] == 'text'
    assert blocks[0]['page_number'] is None


@pytest.mark.unit
def test_parse_nested_headings(tmp_path):
    """h1 > h2 嵌套时正文应归属 'h1 > h2'"""
    md_file = tmp_path / 'nested.md'
    md_file.write_text(
        '# 一级\n段落A\n## 二级\n段落B\n', encoding='utf-8')

    blocks = MarkdownParser().parse(str(md_file))

    sections = {b['section_path']: b['content'] for b in blocks}
    assert sections['一级'] == '段落A'
    assert sections['一级 > 二级'] == '段落B'


@pytest.mark.unit
def test_parse_heading_level_rollback(tmp_path):
    """h2 后回退到同级新 h2 时 section_path 不应残留上级层级"""
    md_file = tmp_path / 'rollback.md'
    md_file.write_text(
        '# 主标题\n正文1\n## 子节A\n正文A\n## 子节B\n正文B\n', encoding='utf-8')

    blocks = MarkdownParser().parse(str(md_file))

    sections = [b['section_path'] for b in blocks]
    assert '主标题 > 子节A' in sections
    assert '主标题 > 子节B' in sections


@pytest.mark.unit
def test_parse_text_before_first_heading_has_empty_section(tmp_path):
    """首个标题前的正文 section_path 应为空字符串"""
    md_file = tmp_path / 'lead.md'
    md_file.write_text('前言文字\n# 标题\n', encoding='utf-8')

    blocks = MarkdownParser().parse(str(md_file))

    assert blocks[0]['section_path'] == ''
    assert blocks[0]['content'] == '前言文字'


@pytest.mark.unit
def test_parse_skips_empty_blocks(tmp_path):
    """两个标题之间无正文时不应产生空内容块"""
    md_file = tmp_path / 'empty.md'
    md_file.write_text('# 标题一\n# 标题二\n正文\n', encoding='utf-8')

    blocks = MarkdownParser().parse(str(md_file))

    assert len(blocks) == 1
    assert blocks[0]['section_path'] == '标题二'
    assert blocks[0]['content'] == '正文'


@pytest.mark.unit
def test_parse_long_heading_truncated(tmp_path):
    """超过 32 字符的标题应被截断为 32 字符"""
    long_title = '很' * 50
    md_file = tmp_path / 'long.md'
    md_file.write_text(f'# {long_title}\n内容\n', encoding='utf-8')

    blocks = MarkdownParser().parse(str(md_file))

    assert len(blocks) == 1
    assert blocks[0]['section_path'] == '很' * 32


@pytest.mark.unit
def test_parse_empty_file_returns_empty_list(tmp_path):
    """空文件应返回空列表"""
    md_file = tmp_path / 'empty.md'
    md_file.write_text('', encoding='utf-8')

    assert MarkdownParser().parse(str(md_file)) == []


@pytest.mark.unit
def test_parse_missing_file_returns_empty_list(tmp_path):
    """文件不存在时读取失败应返回空列表"""
    assert MarkdownParser().parse(str(tmp_path / 'none.md')) == []
