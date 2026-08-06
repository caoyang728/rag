"""
apps.llm.prompts.qa 测试 —— QA Prompt 上下文拼装

覆盖范围：
- _merge_chunks_by_group：按 (document_id, paragraph_group) 合并切片，overlap 去重
- _generate_table_summary：表格摘要生成（列名/行列数/预览行/大表截断提示）
- build_context_block：文本/表格/图片 chunk 的上下文渲染与表格降级保护
- build_qa_messages：system/user messages 拼装（默认/自定义 system、空记忆占位）

用纯 pytest（无 DB）：
prompts/qa 全部为无状态纯函数，输入输出为 dict/list/str，直接断言即可。
"""
import pytest

from apps.llm.prompts.qa import (
    SYSTEM_PROMPT,
    QA_USER_TEMPLATE,
    _merge_chunks_by_group,
    _generate_table_summary,
    build_context_block,
    build_qa_messages,
    MAX_TABLE_CONTEXT_LENGTH,
    MAX_TABLE_PREVIEW_ROWS,
)


def _chunk(doc_id, group, content, chunk_type='text', **extra):
    """构造一个检索 chunk dict"""
    return {'document_id': doc_id, 'content': content,
            'chunk_type': chunk_type, 'extra': extra}


# ============================================================================
# _merge_chunks_by_group
# ============================================================================
@pytest.mark.unit
class TestMergeChunksByGroup:
    """按组合并相邻切片测试"""

    def test_merge_empty(self):
        """空列表返回空列表"""
        assert _merge_chunks_by_group([]) == []

    def test_merge_adjacent_with_overlap(self):
        """同一组相邻切片带 overlap 前缀时应去重拼接"""
        chunks = [
            _chunk(1, 0, 'abcdefgh'),
            _chunk(1, 0, 'cdefgh'),
        ]
        merged = _merge_chunks_by_group(chunks)
        assert len(merged) == 1
        # overlap='cdefgh'（后者是前者的后缀）应去重，拼接为完整文本
        assert merged[0]['content'] == 'abcdefgh'

    def test_merge_adjacent_without_overlap(self):
        """同一组相邻切片无 overlap 时用换行连接"""
        chunks = [
            _chunk(1, 0, '第一段'),
            _chunk(1, 0, '第二段'),
        ]
        merged = _merge_chunks_by_group(chunks)
        assert len(merged) == 1
        assert merged[0]['content'] == '第一段\n第二段'

    def test_merge_different_groups_kept(self):
        """不同 (doc_id, group) 不应合并，保持独立"""
        chunks = [
            _chunk(1, 0, 'A'),
            _chunk(1, 1, 'B'),
            _chunk(2, 0, 'C'),
        ]
        merged = _merge_chunks_by_group(chunks)
        assert len(merged) == 3


# ============================================================================
# _generate_table_summary
# ============================================================================
@pytest.mark.unit
class TestGenerateTableSummary:
    """表格摘要生成测试"""

    def test_summary_contains_structure(self):
        """摘要应包含行列数、列名与数据预览"""
        content = '| 姓名 | 年龄 |\n| 张三 | 20 |'
        summary = _generate_table_summary(content, {})
        assert '共 1 行 × 2 列' in summary
        assert '列名：姓名, 年龄' in summary
        assert '张三' in summary

    def test_summary_uses_extra_rows_cols(self):
        """extra 提供 rows/cols 时追加数据行列数说明"""
        content = '| a | b |\n| 1 | 2 |'
        summary = _generate_table_summary(content, {'rows': 99, 'cols': 8})
        assert '数据行数：99' in summary
        assert '数据列数：8' in summary

    def test_summary_large_table_preview_truncated(self):
        """超过 MAX_TABLE_PREVIEW_ROWS 时仅展示前 N 行并提示总数"""
        rows = ['| H |'] + [f'| r{i} |' for i in range(15)]
        content = '\n'.join(rows)
        summary = _generate_table_summary(content, {})
        assert f'前 {MAX_TABLE_PREVIEW_ROWS} 行数据' in summary
        assert '仅展示前 10 行' in summary
        assert 'r14' not in summary


# ============================================================================
# build_context_block
# ============================================================================
@pytest.mark.unit
class TestBuildContextBlock:
    """上下文块渲染测试"""

    def test_empty_chunks_placeholder(self):
        """无命中 chunk 时返回占位文本"""
        assert build_context_block([]) == '（无相关知识片段）'

    def test_text_chunk_rendered(self):
        """文本 chunk 渲染带来源标题与正文"""
        out = build_context_block([
            {'doc_title': '员工手册', 'content': '入职流程', 'chunk_type': 'text'},
        ])
        assert '来源：《员工手册》' in out
        assert '入职流程' in out

    def test_table_chunk_small_uses_full_content(self):
        """小表格 full_content 不超过阈值时原样展示"""
        full = '| a | b |\n| 1 | 2 |'
        out = build_context_block([_chunk(1, 0, '摘要', chunk_type='table',
                                          full_content=full)])
        assert '【表格】' in out
        assert full in out

    def test_table_chunk_large_uses_summary(self):
        """大表格 full_content 超过阈值时降级为摘要"""
        rows = ['| H |'] + [f'| r{i} |' for i in range(30)]
        full = '\n'.join(rows)
        assert len(full) > MAX_TABLE_CONTEXT_LENGTH
        out = build_context_block([_chunk(1, 0, '摘要', chunk_type='table',
                                          full_content=full)])
        assert '表格摘要' in out
        assert full not in out

    def test_image_chunk_with_base64(self):
        """图片 chunk 有 base64 数据时输出已提取提示"""
        out = build_context_block([_chunk(
            1, 0, 'img', chunk_type='image', base64_data='xxx', width=100, height=50)])
        assert '100×50像素' in out

    def test_image_chunk_without_base64(self):
        """图片 chunk 无 base64 时输出未提取提示"""
        out = build_context_block([_chunk(1, 0, 'img', chunk_type='image')])
        assert '图片数据未提取' in out


# ============================================================================
# build_qa_messages
# ============================================================================
@pytest.mark.unit
class TestBuildQaMessages:
    """QA messages 拼装测试"""

    def test_messages_default_system(self):
        """默认使用 SYSTEM_PROMPT，user 消息包含问题与上下文"""
        msgs = build_qa_messages('今天要做什么', [{'content': '片段', 'chunk_type': 'text'}])
        assert msgs[0]['role'] == 'system'
        assert msgs[0]['content'] == SYSTEM_PROMPT
        assert '今天要做什么' in msgs[1]['content']
        assert '片段' in msgs[1]['content']

    def test_messages_custom_system(self):
        """传入 system_prompt 时覆盖默认值"""
        msgs = build_qa_messages('q', [], system_prompt='自定义系统提示')
        assert msgs[0]['content'] == '自定义系统提示'

    def test_messages_empty_memory_placeholder(self):
        """未传 memory_block 时使用占位文本（无历史记忆）"""
        msgs = build_qa_messages('q', [])
        assert '（无历史记忆）' in msgs[1]['content']

    def test_messages_with_memory(self):
        """传入 memory_block 时拼入 user 内容"""
        msgs = build_qa_messages('q', [], memory_block='用户偏好：简洁回答')
        assert '用户偏好：简洁回答' in msgs[1]['content']

    def test_template_placeholders_format(self):
        """QA_USER_TEMPLATE 的三个占位符均可格式化"""
        out = QA_USER_TEMPLATE.format(memory_block='m', context_block='c', question='q')
        assert 'm' in out and 'c' in out and 'q' in out
