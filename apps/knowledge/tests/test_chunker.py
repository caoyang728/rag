"""
apps.knowledge.chunker 单元测试 —— 语义感知切片

覆盖范围：
- 短 block 保留 / 长 block 按段落切分并保留 overlap
- 表格 block 不切分（双层存储：> 阈值用摘要 embedding，完整内容入 extra.full_content）
- paragraph_group 递增（同一切分源共享组号，不同源递增）
- _split_long 的段落聚合与 overlap 衔接
- _generate_table_summary 的标题/列名/数据预览/大表截断

用纯 pytest（不依赖 DB）：
chunk_blocks / _split_long / _generate_table_summary 均为无状态纯函数，
输入输出为 dict/list，不涉及 ORM，用 pytest 函数式断言即可。
"""
import pytest

from apps.knowledge.chunker import (
    chunk_blocks,
    _split_long,
    _generate_table_summary,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_CHUNK_OVERLAP,
    TABLE_SUMMARY_THRESHOLD,
)


# ============================================================================
# chunk_blocks：短 block 保留
# ============================================================================
@pytest.mark.unit
def test_short_block_preserved():
    """短 block（content <= chunk_size*2）应原样输出为单个切片，不切分"""
    block = {'type': 'text', 'content': '短文本', 'extra': {}}
    out = chunk_blocks([block], chunk_size=10, overlap=5)
    assert len(out) == 1
    assert out[0]['content'] == '短文本'
    # 短 block 不应产生 piece 编号
    assert 'piece' not in out[0]['extra']


@pytest.mark.unit
def test_empty_blocks():
    """空 blocks 列表应返回空列表"""
    assert chunk_blocks([], chunk_size=10, overlap=5) == []


# ============================================================================
# chunk_blocks：长 block 切分
# ============================================================================
@pytest.mark.unit
def test_long_block_split():
    """长 block（content > chunk_size*2）应被切成多个 piece，且共享同一 paragraph_group"""
    # chunk_size=10 → 阈值 20；3 段每段 7 字符共 21 字符 > 20，触发切分
    content = 'aaaaaaa\nbbbbbbb\nccccccc'
    block = {'type': 'text', 'content': content, 'extra': {}}
    out = chunk_blocks([block], chunk_size=10, overlap=5)
    assert len(out) > 1
    # 各 piece 应带 piece 序号，且共享同一 paragraph_group（便于 LLM 上下文合并）
    groups = {b['extra']['paragraph_group'] for b in out}
    assert len(groups) == 1
    assert [b['extra']['piece'] for b in out] == list(range(len(out)))


# ============================================================================
# chunk_blocks：表格不切分 + 双层存储
# ============================================================================
@pytest.mark.unit
def test_table_not_split():
    """表格 block 无论多大都不切分（保留完整结构），仅作为一个切片输出"""
    # 构造一个大于 chunk_size*2 但小于摘要阈值的表格，验证不被切分
    content = '| a | b |\n' + '| 1 | 2 |\n' * 50  # < 2000 字符
    block = {'type': 'table', 'content': content, 'extra': {}}
    out = chunk_blocks([block], chunk_size=10, overlap=5)
    assert len(out) == 1
    # 小表格 content 保持原样
    assert out[0]['content'] == content


@pytest.mark.unit
def test_table_small():
    """小表格（<= 阈值）：content 不变，完整内容存入 extra.full_content"""
    content = '| 姓名 | 年龄 |\n| 张三 | 20 |'
    block = {'type': 'table', 'content': content, 'extra': {}}
    out = chunk_blocks([block], chunk_size=10, overlap=5)
    assert len(out) == 1
    assert out[0]['content'] == content
    assert out[0]['extra']['full_content'] == content


@pytest.mark.unit
def test_table_summary_threshold():
    """大表格（> 阈值）：content 替换为摘要用于 embedding，原表存入 extra.full_content

    双层存储：大表直接 embedding 会撑爆 token 预算且语义稀疏，
    摘要保留结构信息供检索，完整内容在 extra 中供详情页渲染。
    """
    # 构造 > TABLE_SUMMARY_THRESHOLD(2000) 字符的表格
    rows = ['| c1 | c2 |']
    for i in range(200):
        rows.append(f'| r{i} | v{i} |')
    content = '\n'.join(rows)
    assert len(content) > TABLE_SUMMARY_THRESHOLD

    block = {'type': 'table', 'content': content, 'extra': {}}
    out = chunk_blocks([block], chunk_size=10, overlap=5)
    assert len(out) == 1
    # content 被替换为摘要（不再是原表）
    assert out[0]['content'] != content
    # 原表完整保留在 extra.full_content
    assert out[0]['extra']['full_content'] == content


# ============================================================================
# chunk_blocks：paragraph_group 递增
# ============================================================================
@pytest.mark.unit
def test_paragraph_group_incremented():
    """不同 block 应获得不同的 paragraph_group，便于后续按组检索相邻上下文"""
    blocks = [
        {'type': 'text', 'content': '第一段', 'extra': {}},
        {'type': 'text', 'content': '第二段', 'extra': {}},
    ]
    out = chunk_blocks(blocks, chunk_size=10, overlap=5)
    assert len(out) == 2
    assert out[0]['extra']['paragraph_group'] == 0
    assert out[1]['extra']['paragraph_group'] == 1


# ============================================================================
# _split_long：段落聚合
# ============================================================================
@pytest.mark.unit
def test_split_long_aggregation():
    """多个短段落应聚合到同一 chunk，直到累计长度超过 chunk_size"""
    # chunk_size=20，3 段每段 3 字符，累计 11 <= 20，应聚合为单个 chunk
    text = 'aaa\nbbb\nccc'
    chunks = _split_long(text, chunk_size=20, overlap=5)
    assert len(chunks) == 1
    assert 'aaa' in chunks[0]
    assert 'bbb' in chunks[0]
    assert 'ccc' in chunks[0]


@pytest.mark.unit
def test_split_long_overlap():
    """相邻 chunk 应保留 overlap 字符的衔接，避免语义在切片边界被切断"""
    # chunk_size=10，每段 7 字符，第 2 段会触发新 chunk
    text = 'aaaaaaa\nbbbbbbb\nccccccc'
    chunks = _split_long(text, chunk_size=10, overlap=5)
    assert len(chunks) >= 2
    # 后一个 chunk 的开头应等于前一个 chunk 末尾 overlap 个字符
    for i in range(len(chunks) - 1):
        tail = chunks[i][-5:]
        assert chunks[i + 1].startswith(tail)


# ============================================================================
# _generate_table_summary
# ============================================================================
@pytest.mark.unit
def test_generate_table_summary():
    """表格摘要应包含标题、结构（行×列）、列名、说明及数据预览"""
    content = '| 姓名 | 年龄 |\n| 张三 | 20 |\n| 李四 | 30 |'
    extra = {'title': '员工表', 'caption': '2024年数据'}
    summary = _generate_table_summary(content, extra)
    assert '表格标题：员工表' in summary
    assert '表格结构：2行 × 2列' in summary
    assert '列名：姓名, 年龄' in summary
    assert '表格说明：2024年数据' in summary
    # 数据行数 <= 10 时展示全部数据
    assert '表格数据：' in summary
    assert '张三' in summary
    assert '李四' in summary


@pytest.mark.unit
def test_generate_table_summary_large():
    """大于 10 行的表格：仅预览前 5 行并标注总行数，避免摘要过长"""
    rows = ['| H |']
    for i in range(12):
        rows.append(f'| r{i} |')
    content = '\n'.join(rows)  # 1 表头 + 12 数据行 = 13 行，total_rows=12
    summary = _generate_table_summary(content, {})
    assert '表格前5行数据：' in summary
    assert '...（共12行）' in summary
    # 前 5 行应出现，第 6 行不应出现
    assert 'r0' in summary
    assert 'r4' in summary
    assert 'r5' not in summary
