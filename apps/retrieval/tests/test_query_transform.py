"""
retrieval.query_transform 单元测试
覆盖：rewrite_query / decompose_query（LLM 改写、同义词扩展、降级策略）、
search_with_transform（改写→检索→置信度不足→分解→合并去重→Rerank）、
hybrid_search 包装（开关关闭行为不变）。
LLM 与检索全部 mock，不依赖 DB。
"""
import pytest
from unittest.mock import patch, MagicMock

from apps.retrieval.query_transform import (
    rewrite_query, decompose_query, search_with_transform, build_route_trace,
)


def _mock_llm(content: str):
    """构造返回固定 content 的 LLM provider mock"""
    llm = MagicMock()
    llm.chat.return_value = {'content': content, 'prompt_tokens': 1,
                             'completion_tokens': 1, 'cost': 0.0}
    return llm


def _chunk(cid, doc_id=1, rerank_score=None):
    """构造最小 chunk 结构"""
    c = {'chunk_id': cid, 'document_id': doc_id, 'content': f'内容{cid}'}
    if rerank_score is not None:
        c['rerank_score'] = rerank_score
    return c


# ============================================================================
# rewrite_query
# ============================================================================
@pytest.mark.unit
class TestRewriteQuery:
    """LLM 改写 + 同义词扩展，失败必须降级为原始 Query"""

    @patch('apps.retrieval.query_transform.get_llm')
    def test_rewrite_success_when_llm_changes_query_then_returns_rewritten(self, mock_get_llm):
        """正常改写：返回 rewritten_query + expansions + changed=True"""
        mock_get_llm.return_value = _mock_llm(
            '{"rewritten_query": "公司年假申请流程", "expansions": ["年假审批", "休假申请"], "changed": true}')
        r = rewrite_query('年假咋申请')
        assert r['ok'] is True
        assert r['rewritten_query'] == '公司年假申请流程'
        assert r['expansions'] == ['年假审批', '休假申请']
        assert r['changed'] is True
        assert r['error'] == ''
        # 校验 prompt 传参（改写不改变原意，用原 query）
        msgs = mock_get_llm.return_value.chat.call_args.args[0]
        assert '年假咋申请' in msgs[1]['content']

    @patch('apps.retrieval.query_transform.get_llm')
    def test_rewrite_when_markdown_wrapped_then_parses(self, mock_get_llm):
        """兼容 ```json 代码块包裹"""
        mock_get_llm.return_value = _mock_llm(
            '```json\n{"rewritten_query": "报销流程", "expansions": [], "changed": true}\n```')
        r = rewrite_query('报销咋弄')
        assert r['ok'] is True
        assert r['rewritten_query'] == '报销流程'

    @patch('apps.retrieval.query_transform.get_llm')
    def test_rewrite_when_llm_unchanged_then_changed_false(self, mock_get_llm):
        """LLM 判定无需改写：changed=False，rewritten_query=原始"""
        mock_get_llm.return_value = _mock_llm(
            '{"rewritten_query": "报销流程", "expansions": [], "changed": false}')
        r = rewrite_query('报销流程')
        assert r['rewritten_query'] == '报销流程'
        assert r['changed'] is False
        assert r['ok'] is True

    @patch('apps.retrieval.query_transform.get_llm')
    def test_rewrite_when_llm_exception_then_degrade_to_original(self, mock_get_llm):
        """LLM 异常：降级为原始 Query，不抛异常、不阻断主流程"""
        mock_get_llm.return_value.chat.side_effect = RuntimeError('llm down')
        r = rewrite_query('原始问题')
        assert r['ok'] is False
        assert r['rewritten_query'] == '原始问题'
        assert r['changed'] is False
        assert 'llm down' in r['error']

    @patch('apps.retrieval.query_transform.get_llm')
    def test_rewrite_when_invalid_json_then_degrade_to_original(self, mock_get_llm):
        """LLM 输出非 JSON：降级为原始 Query"""
        mock_get_llm.return_value = _mock_llm('这不是 json')
        r = rewrite_query('原始问题')
        assert r['ok'] is False
        assert r['rewritten_query'] == '原始问题'

    @patch('apps.retrieval.query_transform.get_llm')
    def test_rewrite_when_empty_content_then_degrade_to_original(self, mock_get_llm):
        """LLM 返回空 content：降级为原始 Query"""
        mock_get_llm.return_value = _mock_llm('')
        r = rewrite_query('原始问题')
        assert r['ok'] is False
        assert r['rewritten_query'] == '原始问题'


# ============================================================================
# decompose_query
# ============================================================================
@pytest.mark.unit
class TestDecomposeQuery:
    """LLM 拆分为 N 个子查询，失败降级为不分解"""

    @patch('apps.retrieval.query_transform.get_llm')
    def test_decompose_success_when_complex_query_then_returns_sub_queries(self, mock_get_llm):
        """复杂查询：返回多个子查询"""
        mock_get_llm.return_value = _mock_llm(
            '{"need_decompose": true, "sub_queries": ["产品价格", "售后政策"]}')
        r = decompose_query('某产品的价格和售后政策')
        assert r['ok'] is True
        assert r['need_decompose'] is True
        assert r['sub_queries'] == ['产品价格', '售后政策']

    @patch('apps.retrieval.query_transform.get_llm')
    def test_decompose_when_simple_query_then_no_split(self, mock_get_llm):
        """单一问题：need_decompose=False"""
        mock_get_llm.return_value = _mock_llm('{"need_decompose": false, "sub_queries": []}')
        r = decompose_query('报销流程')
        assert r['need_decompose'] is False
        assert r['sub_queries'] == []

    @patch('apps.retrieval.query_transform.get_llm')
    def test_decompose_when_llm_exception_then_degrade_no_split(self, mock_get_llm):
        """LLM 异常：降级为不分解"""
        mock_get_llm.return_value.chat.side_effect = RuntimeError('llm down')
        r = decompose_query('复杂问题')
        assert r['ok'] is False
        assert r['need_decompose'] is False
        assert r['sub_queries'] == []

    @patch('apps.retrieval.query_transform.get_llm')
    def test_decompose_when_max_sub_exceeded_then_truncated(self, mock_get_llm):
        """子查询数超过 QUERY_DECOMPOSE_MAX_SUB 时截断（默认 3）"""
        mock_get_llm.return_value = _mock_llm(
            '{"need_decompose": true, "sub_queries": ["a", "b", "c", "d", "e"]}')
        r = decompose_query('多意图问题')
        assert len(r['sub_queries']) <= 3

    @patch('apps.retrieval.query_transform.get_llm')
    def test_decompose_when_need_flag_but_empty_then_no_split(self, mock_get_llm):
        """LLM 误报 need_decompose=True 但无子查询：视为不分解"""
        mock_get_llm.return_value = _mock_llm('{"need_decompose": true, "sub_queries": []}')
        r = decompose_query('复杂问题')
        assert r['need_decompose'] is False


# ============================================================================
# search_with_transform
# ============================================================================
@pytest.mark.unit
class TestSearchWithTransform:
    """改写 → 检索 → 置信度不足 → 分解 → 合并去重 → Rerank"""

    @patch('apps.retrieval.hybrid._search_core')
    @patch('apps.retrieval.query_transform.get_llm')
    def test_high_confidence_then_no_decompose(self, mock_get_llm, mock_core):
        """改写后置信度足够：不触发分解，直接用改写 query 检索"""
        mock_get_llm.return_value = _mock_llm(
            '{"rewritten_query": "公司年假申请流程", "expansions": ["年假审批"], "changed": true}')
        mock_core.return_value = {
            'chunks': [_chunk(1, rerank_score=0.8), _chunk(2, rerank_score=0.6)],
            'stats': {'total_ms': 1},
            'raw': {'vector': [], 'bm25': [], 'rrf': []},
        }
        r = search_with_transform('年假咋申请', MagicMock())
        # 只用改写后的 query 检索一次
        mock_core.assert_called_once()
        assert mock_core.call_args.args[0] == '公司年假申请流程'
        assert r['transform']['enabled'] is True
        assert r['transform']['rewrite']['changed'] is True
        assert r['transform']['confidence'] == 0.8
        assert 'decompose' not in r['transform']

    @patch('apps.retrieval.rerank.rerank_docs')
    @patch('apps.retrieval.query_transform.rrf_fuse')
    @patch('apps.retrieval.hybrid._search_core')
    @patch('apps.retrieval.query_transform.get_llm')
    def test_low_confidence_then_decompose_and_merge(self, mock_get_llm, mock_core,
                                                     mock_rrf, mock_rerank):
        """改写后置信度不足：拆分子查询逐路召回，RRF 合并去重后重排"""
        # 改写调用 + 分解调用各返回一次
        mock_get_llm.return_value.chat.side_effect = [
            {'content': '{"rewritten_query": "公司年假申请流程", "expansions": [], "changed": true}'},
            {'content': '{"need_decompose": true, "sub_queries": ["年假规则", "请假天数"]}'},
        ]
        # 改写后检索：低置信度（改写路无命中片段 → confidence=0.0）
        def _fake_core(query, *args, **kwargs):
            if query == '公司年假申请流程':
                return {'chunks': [], 'stats': {}, 'raw': {}}
            if query == '年假规则':
                return {'chunks': [_chunk(2), _chunk(3)], 'stats': {}, 'raw': {}}
            return {'chunks': [_chunk(4)], 'stats': {}, 'raw': {}}
        mock_core.side_effect = _fake_core
        # 合并：RRF 融合 + 以原始 query 精排
        mock_rrf.return_value = [_chunk(2), _chunk(1), _chunk(4)]
        mock_rerank.return_value = [_chunk(2), _chunk(1)]

        r = search_with_transform('年假怎么申请', MagicMock())

        # 子查询逐路召回：改写 query + 2 个子查询共 3 次 _search_core
        assert mock_core.call_count == 3
        # 子查询路召回不重复精排（do_rerank=False）
        sub_calls = mock_core.call_args_list[1:]
        assert all(call.kwargs.get('do_rerank') is False for call in sub_calls)
        # 合并后替换 chunks，且以原始 query 重排
        assert [c['chunk_id'] for c in r['chunks']] == [2, 1]
        mock_rerank.assert_called_once()
        assert mock_rerank.call_args.args[0] == '年假怎么申请'
        assert r['transform']['decomposed'] is True
        assert r['transform']['decompose']['sub_queries'] == ['年假规则', '请假天数']
        # route_trace 审计条目可生成
        from apps.retrieval.query_transform import build_route_trace
        trace = build_route_trace(r['transform'])
        layers = [t['layer'] for t in trace]
        assert layers == ['query_rewrite', 'query_decompose']

    @patch('apps.retrieval.hybrid._search_core')
    @patch('apps.retrieval.query_transform.get_llm')
    def test_rewrite_failure_then_search_with_original(self, mock_get_llm, mock_core):
        """LLM 改写失败：降级用原始 query 检索，不触发分解"""
        mock_get_llm.return_value.chat.side_effect = RuntimeError('llm down')
        mock_core.return_value = {'chunks': [_chunk(1, rerank_score=0.8)],
                                  'stats': {}, 'raw': {}}
        r = search_with_transform('原始问题', MagicMock())
        assert mock_core.call_args.args[0] == '原始问题'
        assert r['transform']['rewrite']['ok'] is False
        # 置信度足够 → 不分解
        assert 'decompose' not in r['transform']

    @patch('apps.retrieval.rerank.rerank_docs')
    @patch('apps.retrieval.hybrid._search_core')
    @patch('apps.retrieval.query_transform.get_llm')
    def test_sub_query_search_failure_then_keeps_main_result(self, mock_get_llm, mock_core,
                                                             mock_rerank):
        """子查询检索异常：单路失败不影响整体，跳过该路继续合并"""
        mock_get_llm.return_value.chat.side_effect = [
            {'content': '{"rewritten_query": "改", "expansions": [], "changed": true}'},
            {'content': '{"need_decompose": true, "sub_queries": ["a", "b"]}'},
        ]

        def _fake_core(query, *args, **kwargs):
            if query == '改':
                # 改写路低置信度（命中但分数低 → 触发分解）
                return {'chunks': [_chunk(1, rerank_score=0.1)], 'stats': {}, 'raw': {}}
            if query == 'a':
                raise RuntimeError('vector db down')
            return {'chunks': [_chunk(2)], 'stats': {}, 'raw': {}}
        mock_core.side_effect = _fake_core
        mock_rerank.return_value = [_chunk(2), _chunk(1)]

        # 不抛异常，正常返回（仍可合并 b 的结果）
        r = search_with_transform('复杂问题', MagicMock())
        assert r['transform']['decomposed'] is True
        assert mock_core.call_count == 3

    @patch('apps.retrieval.hybrid._search_core')
    @patch('apps.retrieval.query_transform.get_llm')
    def test_no_rerank_mode_then_merge_without_rerank(self, mock_get_llm, mock_core):
        """do_rerank=False：合并后不重排，直接返回融合结果"""
        mock_get_llm.return_value.chat.side_effect = [
            {'content': '{"rewritten_query": "改", "expansions": [], "changed": true}'},
            {'content': '{"need_decompose": true, "sub_queries": ["a"]}'},
        ]

        def _fake_core(query, *args, **kwargs):
            if query == 'a':
                return {'chunks': [_chunk(7)], 'stats': {}, 'raw': {}}
            return {'chunks': [], 'stats': {}, 'raw': {}}
        mock_core.side_effect = _fake_core
        with patch('apps.retrieval.query_transform.rrf_fuse') as mock_rrf:
            mock_rrf.return_value = [_chunk(5), _chunk(6)]
            with patch('apps.retrieval.rerank.rerank_docs') as mock_rerank:
                r = search_with_transform('问题', MagicMock(), do_rerank=False)
        mock_rerank.assert_not_called()
        assert [c['chunk_id'] for c in r['chunks']] == [5, 6]


# ============================================================================
# hybrid_search 包装（开关）
# ============================================================================
@pytest.mark.unit
class TestHybridSearchWrapper:
    """开关关闭行为与现状一致；开启走 search_with_transform"""

    @patch('apps.retrieval.query_transform.transform_enabled', return_value=False)
    @patch('apps.retrieval.hybrid._search_core')
    def test_switch_off_then_calls_core_directly(self, mock_core, mock_enabled):
        """开关关闭：直接走原混合检索，返回结果不含 transform 键"""
        expected = {'chunks': [], 'stats': {'total_ms': 1}, 'raw': {}}
        mock_core.return_value = expected
        from apps.retrieval.hybrid import hybrid_search
        r = hybrid_search('测试查询', MagicMock(), do_rerank=True)
        assert r is expected
        assert 'transform' not in r
        assert mock_core.call_args.args[0] == '测试查询'

    @patch('apps.retrieval.query_transform.transform_enabled', return_value=True)
    @patch('apps.retrieval.query_transform.search_with_transform')
    def test_switch_on_then_calls_search_with_transform(self, mock_transform, mock_enabled):
        """开关开启：走改写/分解包装链路，返回结构不变并带 transform"""
        mock_transform.return_value = {'chunks': [], 'stats': {}, 'raw': {},
                                       'transform': {'enabled': True}}
        from apps.retrieval.hybrid import hybrid_search
        r = hybrid_search('测试查询', MagicMock())
        mock_transform.assert_called_once()
        assert r['transform']['enabled'] is True


# ============================================================================
# build_route_trace
# ============================================================================
@pytest.mark.unit
class TestBuildRouteTrace:
    """改写/分解追踪信息转 QaRecord.route_trace 审计条目"""

    def test_empty_when_disabled(self):
        from apps.retrieval.query_transform import build_route_trace
        assert build_route_trace(None) == []
        assert build_route_trace({'enabled': False}) == []

    def test_rewrite_entry_fields(self):
        from apps.retrieval.query_transform import build_route_trace
        transform = {
            'enabled': True,
            'rewrite': {'original': '年假咋申请', 'rewritten_query': '公司年假申请流程',
                        'expansions': ['年假审批'], 'changed': True, 'ok': True,
                        'error': '', 'latency_ms': 120},
        }
        trace = build_route_trace(transform)
        assert len(trace) == 1
        entry = trace[0]
        assert entry['layer'] == 'query_rewrite'
        assert entry['query'] == '年假咋申请'
        assert entry['rewritten_query'] == '公司年假申请流程'
        assert entry['changed'] is True
        assert entry['latency_ms'] == 120

    def test_decompose_entry_only_when_triggered(self):
        from apps.retrieval.query_transform import build_route_trace
        transform = {
            'enabled': True,
            'rewrite': {'original': 'q', 'rewritten_query': 'q', 'expansions': [],
                        'changed': False, 'ok': True, 'error': '', 'latency_ms': 10},
            'decompose': {'original': 'q', 'need_decompose': True,
                          'sub_queries': ['a', 'b'], 'ok': True, 'error': '',
                          'latency_ms': 20},
            'decomposed': True,
        }
        trace = build_route_trace(transform)
        assert [t['layer'] for t in trace] == ['query_rewrite', 'query_decompose']
        assert trace[1]['sub_queries'] == ['a', 'b']
        assert trace[1]['decomposed'] is True
