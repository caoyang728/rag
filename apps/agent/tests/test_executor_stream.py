"""
agent.executor 流式问答 ask_stream() 相关单元测试

覆盖：
- ask_stream() 流式主流程（legacy RAG 兜底路径）：输入侧敏感词拦截、缓存命中审查、
  任务拆分、Embedding 异常、流式输出审查（block/mask）、客户端主动断开（GeneratorExit）、
  LLM 流异常
- _ask_stream_via_route：三层路由流式问答
- _ask_stream_via_agent：Agent 模式流式事件转发与统一落库

Mock 说明：
- executor 顶层 `from apps.llm.factory import get_llm` 等 import 会把名称绑定进
  executor 命名空间，因此必须 patch 使用点 apps.agent.executor.*。
- 函数内 import（如 security.sensitive_filter.get_sensitive_filter、
  analytics.realtime.* 等）按定义处 patch。
"""
import pytest
from contextlib import ExitStack
from unittest.mock import patch, MagicMock

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 公共辅助：构造 ask_stream 全链路 mock 环境
# ---------------------------------------------------------------------------

def _stream_mocks():
    """返回 ask_stream 所需的默认 mock 集合（尚未进入 context）

    - sf: 默认放行的敏感词过滤器（check 无命中、feed/flush 原样透传）
    - llm: 默认流式 LLM
    - mm: 默认记忆管理器（load_context 返回空记忆块）
    """
    m = {}
    sf = MagicMock()
    sf.check.return_value = []
    sf.new_state.return_value = {'buffer': ''}
    sf.feed.side_effect = lambda state, delta: ([delta], None)
    sf.flush.return_value = ([], None)
    m['sf'] = sf
    m['llm'] = MagicMock(name='deepseek', model='deepseek-chat')
    m['mm'] = MagicMock()
    m['mm'].load_context.return_value = {'memory_block': ''}
    return m


def _enter_stream_env(m, chunks, llm_stream=None):
    """进入 ask_stream 公共 mock 上下文，返回 (ExitStack, 补丁后的引用)

    chunks: hybrid_search 返回的 chunks
    llm_stream: llm.stream 返回的迭代器；None 时不设置（由用例自行配置）
    """
    stack = ExitStack()
    stack.enter_context(
        patch('apps.security.sensitive_filter.get_sensitive_filter', return_value=m['sf']))
    stack.enter_context(patch('apps.agent.executor._try_cache', return_value=None))
    stack.enter_context(patch('apps.agent.executor.hybrid_search', return_value={
        'chunks': chunks, 'stats': {'vector_ms': 1, 'bm25_ms': 2, 'rrf_ms': 3, 'rerank_ms': 4},
    }))
    stack.enter_context(patch('apps.agent.executor.MemoryManager', return_value=m['mm']))
    stack.enter_context(patch('apps.agent.executor.build_qa_messages',
                              return_value=[{'role': 'user', 'content': 'q'}]))
    stack.enter_context(patch('apps.agent.executor.get_llm', return_value=m['llm']))
    stack.enter_context(patch('apps.agent.executor.LlmCallLog'))
    stack.enter_context(patch('apps.agent.executor._persist_qa',
                              return_value=MagicMock(id=1)))
    stack.enter_context(patch('apps.agent.executor._update_cache'))
    if llm_stream is not None:
        m['llm'].stream.return_value = llm_stream
    return stack


def _chunk(cid, doc_id, title='文档A', section='s1', page=1, rrf=0.5, rerank=0.6):
    """构造一个符合 executor 处理的 chunk 结构"""
    return {
        'chunk_id': cid, 'document_id': doc_id, 'doc_title': title,
        'section_path': section, 'page_number': page,
        'rrf_score': rrf, 'rerank_score': rerank,
    }


# ---------------------------------------------------------------------------
# ask_stream 流式全链路
# ---------------------------------------------------------------------------

class TestAskStream:
    """ask_stream() 流式主流程（legacy RAG 兜底路径）"""

    @staticmethod
    def _session():
        return MagicMock(id=1, turn_count=0)

    def test_ask_stream_when_input_filter_block_then_filtered(self):
        """输入侧命中 block：start + first_token + content_filtered + done，不跑检索/LLM"""
        from apps.agent.executor import ask_stream
        block_hit = MagicMock(action='block', word='违禁', category='porn')
        sf = MagicMock()
        sf.check.return_value = [block_hit]
        with patch('apps.security.sensitive_filter.get_sensitive_filter', return_value=sf), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=9)) as mock_persist, \
                patch('apps.agent.executor.hybrid_search') as mock_hybrid:
            events = list(ask_stream(None, '包含违禁的问题', self._session(),
                                     use_cache=True, mode='legacy'))
        types = [e['type'] for e in events]
        assert types == ['start', 'first_token', 'content_filtered', 'done']
        assert events[0]['session_id'] == 1
        assert events[-1]['is_filtered'] is True
        kw = mock_persist.call_args.kwargs
        assert kw['is_filtered'] is True
        assert kw['filter_reason'] == 'input:违禁'
        assert kw['answer_type'] == 'refused'
        assert kw['is_success'] is True
        mock_hybrid.assert_not_called()

    def test_ask_stream_when_input_filter_exception_then_continues(self):
        """输入侧过滤器抛异常：跳过审查继续主流程（原始 delta 原样下发）"""
        from apps.agent.executor import ask_stream
        m = _stream_mocks()
        # 同时覆盖：输入侧 check 异常 + 流式审查器初始化异常 + 未启用审查的 else 分支
        with patch('apps.security.sensitive_filter.get_sensitive_filter',
                   side_effect=Exception('sf down')), \
                patch('apps.agent.executor._try_cache', return_value=None), \
                patch('apps.agent.executor.hybrid_search', return_value={
                    'chunks': [_chunk(1, 101)],
                    'stats': {'vector_ms': 1, 'bm25_ms': 2, 'rrf_ms': 3, 'rerank_ms': 4},
                }), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.build_qa_messages',
                      return_value=[{'role': 'user', 'content': 'q'}]), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa', return_value=MagicMock(id=1)), \
                patch('apps.agent.executor._update_cache'):
            m['llm'].stream.return_value = iter(
                [{'delta': '原始内容'}, {'finish': True, 'latency_ms': 5}])
            events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                     mode='legacy'))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['原始内容']
        assert events[-1]['type'] == 'done'

    def test_ask_stream_when_cache_hit_safe_then_returns(self):
        """缓存命中且内容安全：一次性下发完整答案"""
        from apps.agent.executor import ask_stream
        sess = self._session()
        cached = {'answer': '缓存答案', 'citations': [{'doc_title': 'A'}]}
        with patch('apps.agent.executor._try_cache', return_value=cached), \
                patch('apps.agent.executor._check_full_text',
                      return_value=('缓存答案', None)), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)) as mock_persist, \
                patch('apps.agent.executor.MemoryManager') as mock_mm_cls:
            events = list(ask_stream(None, '问题', sess, use_cache=True,
                                     mode='agent'))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['缓存答案']
        assert events[0]['is_hit_cache'] is True
        kw = mock_persist.call_args.kwargs
        assert kw['is_hit_cache'] is True
        assert kw['is_filtered'] is False
        mock_mm_cls.return_value.append_turn.assert_called_once_with(
            sess, '问题', '缓存答案')

    def test_ask_stream_when_cache_hit_block_then_filtered(self):
        """缓存命中但内容命中 block：发拦截事件，不写记忆（防止绕过审查复用）"""
        from apps.agent.executor import ask_stream
        sess = self._session()
        block_hit = MagicMock(action='block', word='违禁', category='other')
        cached = {'answer': '含违禁的答案', 'citations': []}
        with patch('apps.agent.executor._try_cache', return_value=cached), \
                patch('apps.agent.executor._check_full_text',
                      return_value=('含违禁的答案', block_hit)), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=2)) as mock_persist, \
                patch('apps.agent.executor.MemoryManager') as mock_mm_cls:
            events = list(ask_stream(None, '问题', sess, use_cache=True,
                                     mode='agent'))
        types = [e['type'] for e in events]
        assert 'content_filtered' in types
        assert 'delta' not in types  # 命中 block 不下发任何 delta
        kw = mock_persist.call_args.kwargs
        assert kw['is_filtered'] is True
        assert kw['filter_reason'] == 'cache:违禁'
        # 命中 block 时记忆写入空串（避免违规内容污染后续上下文）
        mock_mm_cls.return_value.append_turn.assert_called_once_with(
            sess, '问题', '')

    def test_ask_stream_when_task_split_then_split(self):
        """任务拆分：start + first_token + delta/done（合并答案一次性输出）"""
        from apps.agent.executor import ask_stream
        with patch('apps.agent.executor._try_cache', return_value=None), \
                patch('apps.agent.task_splitter.maybe_split',
                      return_value={'need_split': True}), \
                patch('apps.agent.task_splitter.execute_split',
                      return_value={'answer': '拆分答案', 'qa_id': 5,
                                    'citations': [{'doc_title': 'A'}]}), \
                patch('apps.agent.executor._check_full_text',
                      return_value=('拆分答案', None)):
            events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                     mode='legacy', do_task_split=True))
        types = [e['type'] for e in events]
        assert types == ['start', 'first_token', 'delta', 'done']
        assert events[-1]['message_id'] == 5
        assert events[-1]['stats']['is_task_split'] is True
        assert events[-1]['is_filtered'] is False

    def test_ask_stream_when_task_split_hit_block_then_filtered(self):
        """任务拆分合并答案命中 block：发拦截事件、不落 delta"""
        from apps.agent.executor import ask_stream
        block_hit = MagicMock(action='block', word='违禁', category='other')
        with patch('apps.agent.executor._try_cache', return_value=None), \
                patch('apps.agent.task_splitter.maybe_split',
                      return_value={'need_split': True}), \
                patch('apps.agent.task_splitter.execute_split',
                      return_value={'answer': '拆分答案', 'qa_id': 5, 'citations': []}), \
                patch('apps.agent.executor._check_full_text',
                      return_value=('拆分答案', block_hit)):
            events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                     mode='legacy', do_task_split=True))
        types = [e['type'] for e in events]
        assert 'content_filtered' in types
        assert 'delta' not in types
        assert events[-1]['is_filtered'] is True

    def test_ask_stream_when_task_split_execute_error_then_error_event(self):
        """任务拆分执行异常：发 error 事件并终止"""
        from apps.agent.executor import ask_stream
        with patch('apps.agent.executor._try_cache', return_value=None), \
                patch('apps.agent.task_splitter.maybe_split',
                      return_value={'need_split': True}), \
                patch('apps.agent.task_splitter.execute_split',
                      side_effect=RuntimeError('split failed')):
            events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                     mode='legacy', do_task_split=True))
        assert events[0]['type'] == 'start'
        assert events[-1]['type'] == 'error'
        assert '任务拆分执行失败' in events[-1]['detail']

    def test_ask_stream_when_embedding_exception_then_refused(self):
        """流式 Embedding 失败：降级提示 + error_type='embedding_error'"""
        from apps.llm.embedding import EmbeddingException
        from apps.agent.executor import ask_stream
        with patch('apps.agent.executor._try_cache', return_value=None), \
                patch('apps.agent.executor.hybrid_search',
                      side_effect=EmbeddingException('embedding down')), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)) as mock_persist:
            events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                     mode='legacy'))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['当前向量服务暂时不可用，请稍后重试。']
        kw = mock_persist.call_args.kwargs
        assert kw['error_type'] == 'embedding_error'
        assert kw['is_success'] is False
        assert events[-1]['stats']['error'] == 'embedding down'

    def test_ask_stream_when_retrieval_error_then_error_event(self):
        """检索抛非 Embedding 异常：直接发 error 事件"""
        from apps.agent.executor import ask_stream
        with patch('apps.agent.executor._try_cache', return_value=None), \
                patch('apps.agent.executor.hybrid_search',
                      side_effect=RuntimeError('db down')):
            events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                     mode='legacy'))
        assert events[0]['type'] == 'error'
        assert '检索失败' in events[0]['detail']

    def test_ask_stream_when_rag_flow_success_then_streams(self):
        """流式 RAG 主流程：start → first_token → delta* → done，落库 + 缓存/记忆"""
        from apps.agent.executor import ask_stream
        sess = self._session()
        m = _stream_mocks()
        with _enter_stream_env(m, [_chunk(1, 101, title='文档A', section='s1', page=2)],
                               llm_stream=iter([
                                   {'delta': '你好'}, {'delta': '世界'},
                                   {'finish': True, 'latency_ms': 5},
                               ])):
            with patch('apps.agent.executor._persist_qa',
                       return_value=MagicMock(id=1)) as mock_persist, \
                    patch('apps.agent.executor._update_cache') as mock_update_cache:
                events = list(ask_stream(None, '问题', sess, use_cache=True,
                                         mode='legacy'))
        types = [e['type'] for e in events]
        assert types[0] == 'start'
        assert 'first_token' in types
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['你好', '世界']
        assert events[-1]['type'] == 'done'
        assert events[-1]['is_filtered'] is False
        assert events[-1]['citations'][0]['doc_title'] == '文档A'
        kw = mock_persist.call_args.kwargs
        assert kw['answer_type'] == 'rag'
        assert kw['is_success'] is True
        assert kw['retrieval_hits'] == [1]
        mock_update_cache.assert_called_once()
        m['mm'].append_turn.assert_called_once_with(sess, '问题', '你好世界')

    def test_ask_stream_when_no_chunks_then_refused(self):
        """无相关片段：固定拒答文案，不更新缓存"""
        from apps.agent.executor import ask_stream
        m = _stream_mocks()
        with _enter_stream_env(m, []):
            with patch('apps.agent.executor._persist_qa',
                       return_value=MagicMock(id=1)) as mock_persist, \
                    patch('apps.agent.executor._update_cache') as mock_update_cache:
                events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                         mode='legacy'))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert '未找到相关资料' in deltas[0]
        kw = mock_persist.call_args.kwargs
        assert kw['answer_type'] == 'refused'
        mock_update_cache.assert_not_called()
        m['llm'].stream.assert_not_called()

    def test_ask_stream_when_block_mid_stream_then_filtered(self):
        """输出侧流式命中 block：立即中断，落库标记 is_filtered，不写缓存/记忆"""
        from apps.agent.executor import ask_stream
        sess = self._session()
        m = _stream_mocks()
        block_hit = MagicMock(action='block', word='违禁', category='other')
        m['sf'].feed.side_effect = lambda state, delta: (
            ([], block_hit) if '违禁' in delta else ([delta], None))
        with _enter_stream_env(m, [_chunk(1, 101)], llm_stream=iter([
            {'delta': '正常内容'}, {'delta': '包含违禁内容'},
            {'delta': '不应下发的内容'}, {'finish': True, 'latency_ms': 5},
        ])):
            with patch('apps.agent.executor._persist_qa',
                       return_value=MagicMock(id=1)) as mock_persist, \
                    patch('apps.agent.executor._update_cache') as mock_update_cache:
                events = list(ask_stream(None, '问题', sess, use_cache=True,
                                         mode='legacy'))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['正常内容']
        assert 'content_filtered' in [e['type'] for e in events]
        kw = mock_persist.call_args.kwargs
        assert kw['is_filtered'] is True
        assert kw['filter_reason'] == 'output:违禁'
        assert kw['answer'] == '正常内容'
        mock_update_cache.assert_not_called()
        # 命中 block 时记忆写入空串
        m['mm'].append_turn.assert_called_once_with(sess, '问题', '')

    def test_ask_stream_when_mask_output_then_masked(self):
        """输出侧 mask 命中：delta 已脱敏，正常完成"""
        from apps.agent.executor import ask_stream
        m = _stream_mocks()
        m['sf'].feed.side_effect = lambda state, delta: (['***'], None)
        with _enter_stream_env(m, [_chunk(1, 101)], llm_stream=iter([
            {'delta': '手机号 13800138000'}, {'finish': True, 'latency_ms': 5},
        ])):
            with patch('apps.agent.executor._persist_qa',
                       return_value=MagicMock(id=1)) as mock_persist:
                events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                         mode='legacy'))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['***']
        kw = mock_persist.call_args.kwargs
        assert kw['is_filtered'] is False
        assert kw['answer'] == '***'

    def test_ask_stream_when_llm_stream_exception_then_refused(self):
        """LLM 流式异常：补 first_token + '[流式中断]' delta，is_success=False

        注意（源码现状）：'[流式中断: ...]' delta 只下发前端，未追加进 full_answer，
        因此落库 answer 为 '[未生成内容]'、answer_type='refused'——前端展示内容与
        持久化内容不一致，属源码缺陷（详见报告）。
        """
        from apps.agent.executor import ask_stream
        m = _stream_mocks()
        with _enter_stream_env(m, [_chunk(1, 101)]):
            m['llm'].stream.side_effect = Exception('LLM down')
            with patch('apps.agent.executor._persist_qa',
                       return_value=MagicMock(id=1)) as mock_persist, \
                    patch('apps.agent.executor._update_cache') as mock_update_cache:
                events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                         mode='legacy'))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['\n\n[流式中断: LLM down]']
        kw = mock_persist.call_args.kwargs
        assert kw['answer'] == '[未生成内容]'
        assert kw['answer_type'] == 'refused'
        assert kw['is_success'] is False
        assert kw['error_type'] == 'unknown'
        mock_update_cache.assert_not_called()

    def test_ask_stream_when_no_delta_then_refused(self):
        """LLM 未产出任何 delta（首帧即 finish）：兜底 '[未生成内容]' + refused"""
        from apps.agent.executor import ask_stream
        m = _stream_mocks()
        with _enter_stream_env(m, [_chunk(1, 101)], llm_stream=iter([
            {'finish': True, 'latency_ms': 5},
        ])):
            with patch('apps.agent.executor._persist_qa',
                       return_value=MagicMock(id=1)) as mock_persist, \
                    patch('apps.agent.executor._update_cache') as mock_update_cache:
                events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                         mode='legacy'))
        types = [e['type'] for e in events]
        assert types == ['start', 'first_token', 'done']
        kw = mock_persist.call_args.kwargs
        assert kw['answer'] == '[未生成内容]'
        assert kw['answer_type'] == 'refused'
        mock_update_cache.assert_not_called()

    def test_ask_stream_when_client_abort_then_persists_partial(self):
        """客户端主动断开（GeneratorExit）：保存已生成的部分答案"""
        from apps.agent.executor import ask_stream
        m = _stream_mocks()
        with _enter_stream_env(m, [_chunk(1, 101)], llm_stream=iter([
            {'delta': '部分'}, {'delta': '答案'}, {'finish': True, 'latency_ms': 5},
        ])):
            with patch('apps.agent.executor._persist_qa',
                       return_value=MagicMock(id=1)) as mock_persist:
                gen = ask_stream(None, '问题', self._session(), use_cache=True,
                                 mode='legacy')
                # 消费到首个 delta 后主动关闭生成器
                while True:
                    ev = next(gen)
                    if ev['type'] == 'delta':
                        break
                gen.close()  # 触发 GeneratorExit，不向调用方抛异常
                kw = mock_persist.call_args.kwargs
                assert kw['answer'] == '部分'
                assert kw['answer_type'] == 'rag'
                assert kw['is_success'] is True

    def test_ask_stream_when_flush_block_then_filtered(self):
        """流式收尾 flush 命中 block：发拦截事件并标记 is_filtered"""
        from apps.agent.executor import ask_stream
        m = _stream_mocks()
        block_hit = MagicMock(action='block', word='尾词', category='other')
        m['sf'].flush.return_value = ([], block_hit)
        with _enter_stream_env(m, [_chunk(1, 101)], llm_stream=iter([
            {'delta': '内容'}, {'finish': True, 'latency_ms': 5},
        ])):
            with patch('apps.agent.executor._persist_qa',
                       return_value=MagicMock(id=1)) as mock_persist:
                events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                         mode='legacy'))
        types = [e['type'] for e in events]
        assert 'content_filtered' in types
        assert events[-1]['is_filtered'] is True
        kw = mock_persist.call_args.kwargs
        assert kw['is_filtered'] is True
        assert kw['filter_reason'] == 'output:尾词'

    def test_ask_stream_when_flush_exception_then_continues(self):
        """flush 审查器抛异常：仅记录日志，流程正常收尾"""
        from apps.agent.executor import ask_stream
        m = _stream_mocks()
        m['sf'].flush.side_effect = Exception('flush failed')
        with _enter_stream_env(m, [_chunk(1, 101)], llm_stream=iter([
            {'delta': '内容'}, {'finish': True, 'latency_ms': 5},
        ])):
            with patch('apps.agent.executor._persist_qa',
                       return_value=MagicMock(id=1)) as mock_persist:
                events = list(ask_stream(None, '问题', self._session(), use_cache=True,
                                         mode='legacy'))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['内容']
        assert events[-1]['type'] == 'done'
        assert events[-1]['is_filtered'] is False

    def test_ask_stream_when_permission_filter_then_filters(self):
        """流式路径二次权限验证：过滤无权文档片段"""
        from apps.agent.executor import ask_stream
        m = _stream_mocks()
        with _enter_stream_env(m, [_chunk(1, 101, title='A'), _chunk(2, 102, title='B')],
                               llm_stream=iter([{'delta': '答案'},
                                                {'finish': True, 'latency_ms': 5}])):
            with patch('apps.agent.executor.filter_accessible_doc_ids',
                       return_value=[101]) as mock_filter, \
                    patch('apps.agent.executor._persist_qa',
                          return_value=MagicMock(id=1)) as mock_persist:
                user = MagicMock(is_authenticated=True, id=1)
                events = list(ask_stream(user, '问题', self._session(), use_cache=True,
                                         mode='legacy'))
        mock_filter.assert_called_once_with(user, [101, 102])
        kw = mock_persist.call_args.kwargs
        assert kw['retrieval_hits'] == [1]
        assert events[-1]['citations'][0]['doc_title'] == 'A'


# ---------------------------------------------------------------------------
# _ask_stream_via_route
# ---------------------------------------------------------------------------

class TestAskStreamViaRoute:
    """_ask_stream_via_route：三层路由流式问答"""

    @staticmethod
    def _session():
        return MagicMock(id=1, turn_count=0)

    def test_ask_stream_via_route_when_has_context_then_streams(self):
        """有路由上下文：流式输出答案，done 携带 route_source/route_trace"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        m['llm'].stream.return_value = iter([
            {'delta': '路由流式答案'}, {'finish': True, 'latency_ms': 5},
        ])
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '路由知识片段', 'chunks': [], 'source': 'wiki',
            'route_trace': [{'layer': 'wiki'}],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)) as mock_persist, \
                patch('apps.agent.executor._update_cache') as mock_update_cache:
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc', 'db', 'web', 'llm']))
        assert events[0]['type'] == 'start'
        assert events[0]['route_source'] == 'wiki'
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['路由流式答案']
        done = events[-1]
        assert done['type'] == 'done'
        assert done['route_source'] == 'wiki'
        kw = mock_persist.call_args.kwargs
        assert kw['answer_type'] == 'rag'
        assert kw['route_source'] == 'wiki'
        assert kw['route_trace'] == [{'layer': 'wiki'}]
        mock_update_cache.assert_called_once()

    def test_ask_stream_via_route_when_no_context_then_refused(self):
        """无路由上下文：拒答文案，不调 LLM"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '', 'chunks': [], 'source': 'none', 'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)) as mock_persist, \
                patch('apps.agent.executor._update_cache') as mock_update_cache:
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc', 'db', 'web', 'llm']))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert '未找到相关资料' in deltas[0]
        kw = mock_persist.call_args.kwargs
        assert kw['answer_type'] == 'refused'
        m['llm'].stream.assert_not_called()
        mock_update_cache.assert_not_called()


# ---------------------------------------------------------------------------
# _ask_stream_via_agent
# ---------------------------------------------------------------------------

class TestAskStreamViaAgent:
    """_ask_stream_via_agent：Agent 模式流式事件转发与统一落库"""

    @staticmethod
    def _session():
        return MagicMock(id=1, turn_count=0)

    def _env(self, events):
        """构造 _ask_stream_via_agent 的 mock 上下文"""
        stack = ExitStack()
        stack.enter_context(patch('apps.agent.react.agent_ask_stream',
                                  return_value=iter(events)))
        trace = MagicMock()
        stack.enter_context(patch('apps.agent.models.AgentTrace', trace))
        stack.enter_context(patch('apps.agent.executor.LlmCallLog'))
        persist = MagicMock(return_value=MagicMock(id=5))
        stack.enter_context(patch('apps.agent.executor._persist_qa', persist))
        update_cache = MagicMock()
        stack.enter_context(patch('apps.agent.executor._update_cache', update_cache))
        mm = MagicMock()
        stack.enter_context(patch('apps.agent.executor.MemoryManager', return_value=mm))
        return stack, {'trace': trace, 'persist': persist, 'update_cache': update_cache,
                       'mm': mm}

    @staticmethod
    def _done_event(**overrides):
        ev = {
            'type': 'done', 'answer': '最终答案', 'citations': [{'doc_title': 'A'}],
            'tool_traces': [{'round': 1, 'tool_name': 'calculator'}],
            'chunks': [{'chunk_id': 1}], 'is_filtered': False,
            'stats': {'llm': {'tokens_prompt': 1}, 'tool_rounds': 1},
        }
        ev.update(overrides)
        return ev

    def test_ask_stream_via_agent_when_forward_events_then_persists(self):
        """转发 tool_call/tool_result/delta 事件，done 统一落库（answer_type='agent'）"""
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([
            {'type': 'first_token', 'ttfb_ms': 3},
            {'type': 'tool_call', 'call_id': 'c1', 'tool_name': 'calculator',
             'tool_args': {'expr': '1+1'}},
            {'type': 'tool_result', 'call_id': 'c1', 'tool_name': 'calculator',
             'ok': True},
            {'type': 'delta', 'delta': '答案'},
            self._done_event(),
        ])
        with stack:
            events = list(_ask_stream_via_agent(None, '问题', self._session(),
                                                ['company_doc'], None, 'company_doc', 1, 0,
                                                ['doc', 'db', 'web', 'llm']))
        types = [e['type'] for e in events]
        assert types[0] == 'start'
        assert 'tool_call' in types
        assert 'tool_result' in types
        assert types[-1] == 'done'
        assert events[-1]['message_id'] == 5
        assert events[-1]['stats']['is_agent'] is True
        kw = m['persist'].call_args.kwargs
        assert kw['answer_type'] == 'agent'
        assert kw['answer'] == '最终答案'  # 优先使用 done 事件的 answer
        assert kw['retrieval_hits'] == [1]
        assert kw['is_success'] is True
        m['trace'].batch_create_from_traces.assert_called_once()
        m['update_cache'].assert_called_once()
        m['mm'].append_turn.assert_called_once()

    def test_ask_stream_via_agent_when_content_filtered_then_refused(self):
        """Agent 内部命中 block：answer 存空串 + answer_type='refused'"""
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([
            {'type': 'content_filtered', 'category': 'porn'},
            self._done_event(answer='', citations=[], tool_traces=[], chunks=[],
                             is_filtered=True, filter_reason='output:词',
                             stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            events = list(_ask_stream_via_agent(None, '问题', self._session(),
                                                ['company_doc'], None, 'company_doc', 1, 0,
                                                ['doc', 'db', 'web', 'llm']))
        types = [e['type'] for e in events]
        assert 'content_filtered' in types
        assert events[-1]['is_filtered'] is True
        kw = m['persist'].call_args.kwargs
        assert kw['answer'] == ''
        assert kw['answer_type'] == 'refused'
        assert kw['is_filtered'] is True
        assert kw['filter_reason'] == 'output:词'
        m['update_cache'].assert_not_called()

    def test_ask_stream_via_agent_when_error_event_then_sets_agent_error(self):
        """Agent 内部 error 事件：转发 + error_type='agent_error' + is_success=False

        _ask_stream_via_agent 始终先发 start 事件，因此 error 事件位于 events[1]。
        """
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([
            {'type': 'error', 'detail': 'Agent 内部错误'},
            self._done_event(answer='', citations=[], tool_traces=[], chunks=[],
                             stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            events = list(_ask_stream_via_agent(None, '问题', self._session(),
                                                ['company_doc'], None, 'company_doc', 1, 0,
                                                ['doc', 'db', 'web', 'llm']))
        assert events[0]['type'] == 'start'
        assert events[1]['type'] == 'error'
        kw = m['persist'].call_args.kwargs
        assert kw['error_type'] == 'agent_error'
        assert kw['is_success'] is False
        assert kw['answer_type'] == 'refused'

    def test_ask_stream_via_agent_when_no_answer_no_traces_then_refused(self):
        """无答案且无工具调用：'[未生成内容]' + refused"""
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([
            self._done_event(answer='', citations=[], tool_traces=[], chunks=[],
                             stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            events = list(_ask_stream_via_agent(None, '问题', self._session(),
                                                ['company_doc'], None, 'company_doc', 1, 0,
                                                ['doc', 'db', 'web', 'llm']))
        kw = m['persist'].call_args.kwargs
        assert kw['answer'] == '[未生成内容]'
        assert kw['answer_type'] == 'refused'
        m['update_cache'].assert_not_called()

    def test_ask_stream_via_agent_when_llm_stats_error_then_detected(self):
        """done 事件携带的 llm_stats 含 error：按 _detect_error_type 分类"""
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([
            self._done_event(answer='答案', citations=[], tool_traces=[], chunks=[],
                             stats={'llm': {'error': 'HTTP 429 Too Many Requests'},
                                    'tool_rounds': 0}),
        ])
        with stack:
            events = list(_ask_stream_via_agent(None, '问题', self._session(),
                                                ['company_doc'], None, 'company_doc', 1, 0,
                                                ['doc', 'db', 'web', 'llm']))
        kw = m['persist'].call_args.kwargs
        assert kw['error_type'] == 'rate_limit'
        assert kw['is_success'] is False

    def test_ask_stream_via_agent_when_client_abort_then_persists_partial(self):
        """客户端断开：保存已生成的部分答案（answer_type='agent'）"""
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([
            {'type': 'delta', 'delta': '部分'},
            self._done_event(),
        ])
        with stack:
            gen = _ask_stream_via_agent(None, '问题', self._session(),
                                        ['company_doc'], None, 'company_doc', 1, 0,
                                        ['doc', 'db', 'web', 'llm'])
            ev = next(gen)  # start
            assert ev['type'] == 'start'
            ev = next(gen)  # delta
            assert ev['type'] == 'delta'
            gen.close()  # 触发 GeneratorExit
            kw = m['persist'].call_args.kwargs
            assert kw['answer'] == '部分'
            assert kw['answer_type'] == 'agent'
            assert kw['is_success'] is True

    def test_ask_stream_via_agent_when_trace_failure_then_non_fatal(self):
        """AgentTrace 批量写入失败不影响流式主流程"""
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([self._done_event()])
        with stack:
            with patch('apps.agent.models.AgentTrace.batch_create_from_traces',
                       side_effect=Exception('trace write failed')):
                events = list(_ask_stream_via_agent(None, '问题', self._session(),
                                                    ['company_doc'], None, 'company_doc',
                                                    1, 0,
                                                    ['doc', 'db', 'web', 'llm']))
        assert events[-1]['type'] == 'done'

    def test_ask_stream_via_agent_when_client_abort_filtered_then_persists_empty(self):
        """客户端断开 + is_filtered=True：保存空 answer（规避违规内容落库）"""
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([
            {'type': 'content_filtered', 'category': 'porn'},
            self._done_event(answer='', citations=[], tool_traces=[], chunks=[],
                             is_filtered=True, filter_reason='output:词',
                             stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            gen = _ask_stream_via_agent(None, '问题', self._session(),
                                        ['company_doc'], None, 'company_doc', 1, 0,
                                        ['doc', 'db', 'web', 'llm'])
            ev = next(gen)  # start
            assert ev['type'] == 'start'
            ev = next(gen)  # content_filtered
            assert ev['type'] == 'content_filtered'
            gen.close()  # 触发 GeneratorExit
            kw = m['persist'].call_args.kwargs
            assert kw['answer'] == ''
            assert kw['is_filtered'] is True

    def test_ask_stream_via_agent_when_llm_call_log_exception_then_non_fatal(self):
        """LlmCallLog 写入失败不影响流式主流程"""
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([self._done_event()])
        with stack:
            with patch('apps.agent.executor.LlmCallLog',
                       side_effect=Exception('db down')):
                events = list(_ask_stream_via_agent(None, '问题', self._session(),
                                                    ['company_doc'], None, 'company_doc',
                                                    1, 0,
                                                    ['doc', 'db', 'web', 'llm']))
        assert events[-1]['type'] == 'done'

    def test_ask_stream_via_agent_when_answer_only_no_traces_then_general(self):
        """有答案但无工具调用链：answer_type='general'"""
        from apps.agent.executor import _ask_stream_via_agent
        stack, m = self._env([
            self._done_event(answer='直接回答', citations=[], tool_traces=[],
                             chunks=[], stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            events = list(_ask_stream_via_agent(None, '问题', self._session(),
                                                ['company_doc'], None, 'company_doc',
                                                1, 0,
                                                ['doc', 'db', 'web', 'llm']))
        kw = m['persist'].call_args.kwargs
        assert kw['answer_type'] == 'general'


# ---------------------------------------------------------------------------
# _ask_stream_via_route 额外覆盖
# ---------------------------------------------------------------------------

class TestAskStreamViaRouteExtra:
    """_ask_stream_via_route 额外路径覆盖"""

    @staticmethod
    def _session():
        return MagicMock(id=1, turn_count=0)

    def test_route_when_orchestrate_exception_then_refused(self):
        """orchestrate 异常：降级为空 chunks + 拒答"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        with patch('apps.graph.router.orchestrate', side_effect=Exception('orchestrate down')), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)), \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc', 'db', 'web', 'llm']))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert '未找到相关资料' in deltas[0]
        m['llm'].stream.assert_not_called()

    def test_route_when_doc_not_in_sources_then_refused(self):
        """关闭内部文档来源：拒答文案提示用户开启"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        with patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)), \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['db', 'web', 'llm']))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert '已关闭' in deltas[0]
        assert '内部文档' in deltas[0]

    def test_route_when_llm_finish_with_error_then_detected(self):
        """LLM 流式 finish 帧携带 error：error_type 被检测"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        m['llm'].stream.return_value = iter([
            {'finish': True, 'error': 'HTTP 429'},
        ])
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '上下文', 'chunks': [], 'source': 'rag',
            'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)) as mock_persist, \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc']))
        kw = mock_persist.call_args.kwargs
        assert kw['answer'] == '[未生成内容]'
        assert kw['is_success'] is False

    def test_route_when_filter_block_mid_stream_then_filtered(self):
        """route 流式审查 block 命中：中断 + 发拦截事件"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        block_hit = MagicMock(action='block', word='违禁', category='other')
        m['sf'].feed.side_effect = lambda state, delta: (
            ([], block_hit) if '违禁' in delta else ([delta], None))
        m['llm'].stream.return_value = iter([
            {'delta': '正常'}, {'delta': '违禁内容'},
            {'finish': True, 'latency_ms': 5},
        ])
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '上下文', 'chunks': [], 'source': 'rag',
            'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)) as mock_persist, \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc']))
        assert 'content_filtered' in [e['type'] for e in events]
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert deltas == ['正常']
        kw = mock_persist.call_args.kwargs
        assert kw['is_filtered'] is True

    def test_route_when_llm_stream_exception_then_continues(self):
        """route LLM 流式异常：补 first_token + 中断 delta"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        m['llm'].stream.side_effect = Exception('LLM route down')
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '上下文', 'chunks': [], 'source': 'rag',
            'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)), \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc']))
        types = [e['type'] for e in events]
        assert 'first_token' in types
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert any('流式中断' in d for d in deltas)

    def test_route_when_flush_block_then_filtered(self):
        """route flush 审查 buffer 命中 block"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        block_hit = MagicMock(action='block', word='尾词', category='other')
        m['sf'].flush.return_value = ([], block_hit)
        m['llm'].stream.return_value = iter([
            {'delta': '内容'}, {'finish': True, 'latency_ms': 5},
        ])
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '上下文', 'chunks': [], 'source': 'rag',
            'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)) as mock_persist, \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc']))
        assert events[-1]['is_filtered'] is True

    def test_route_when_flush_exception_then_continues(self):
        """route flush 审查器抛异常：仅记录日志，正常收尾"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        m['sf'].flush.side_effect = Exception('flush err')
        m['llm'].stream.return_value = iter([
            {'delta': '内容'}, {'finish': True, 'latency_ms': 5},
        ])
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '上下文', 'chunks': [], 'source': 'rag',
            'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)), \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc']))
        assert events[-1]['type'] == 'done'
        assert events[-1]['is_filtered'] is False

    def test_route_when_empty_answer_then_refused(self):
        """LLM 未产出任何 delta：answer_type='refused'"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        m['llm'].stream.return_value = iter([
            {'finish': True, 'latency_ms': 5},
        ])
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '上下文', 'chunks': [], 'source': 'rag',
            'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)) as mock_persist, \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc']))
        kw = mock_persist.call_args.kwargs
        assert kw['answer'] == '[未生成内容]'
        assert kw['answer_type'] == 'refused'

    def test_route_when_llm_call_log_exception_then_non_fatal(self):
        """route LlmCallLog 写入失败不影响主流程"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        m['llm'].stream.return_value = iter([
            {'delta': '答案'}, {'finish': True, 'latency_ms': 5},
        ])
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '上下文', 'chunks': [], 'source': 'rag',
            'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog',
                      side_effect=Exception('db write fail')), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)), \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc']))
        assert events[-1]['type'] == 'done'

    def test_route_when_no_filter_then_llm_stream_raw(self):
        """route 审查器初始化失败：LLM 原样下发（未启用审查的 else 分支）"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        m['llm'].stream.return_value = iter([
            {'delta': '原始内容'}, {'finish': True, 'latency_ms': 5},
        ])
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '上下文', 'chunks': [], 'source': 'rag',
            'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      side_effect=Exception('sf init fail')), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)), \
                patch('apps.agent.executor._update_cache'):
            events = list(_ask_stream_via_route(None, '问题', self._session(),
                                                ['company_doc'], 'company_doc', 1, 0,
                                                ['doc']))
        deltas = [e['delta'] for e in events if e['type'] == 'delta']
        assert '原始内容' in deltas

    def test_route_when_client_abort_then_persists_partial(self):
        """route 客户端断开：保存已生成的部分答案"""
        from apps.agent.executor import _ask_stream_via_route
        m = _stream_mocks()
        m['llm'].stream.return_value = iter([
            {'delta': '部分'}, {'delta': '答案'}, {'finish': True, 'latency_ms': 5},
        ])
        with patch('apps.graph.router.orchestrate', return_value={
            'context': '上下文', 'chunks': [], 'source': 'rag',
            'route_trace': [],
        }), \
                patch('apps.security.sensitive_filter.get_sensitive_filter',
                      return_value=m['sf']), \
                patch('apps.agent.executor.MemoryManager', return_value=m['mm']), \
                patch('apps.agent.executor.get_llm', return_value=m['llm']), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)) as mock_persist, \
                patch('apps.agent.executor._update_cache'):
            gen = _ask_stream_via_route(None, '问题', self._session(),
                                        ['company_doc'], 'company_doc', 1, 0, ['doc'])
            while True:
                ev = next(gen)
                if ev['type'] == 'delta':
                    break
            gen.close()
            kw = mock_persist.call_args.kwargs
            assert kw['answer'] == '部分'


# ---------------------------------------------------------------------------
# _ask_stream_via_plan
# ---------------------------------------------------------------------------

class TestAskStreamViaPlan:
    """_ask_stream_via_plan：Plan-and-Execute 模式流式问答"""

    @staticmethod
    def _session():
        return MagicMock(id=1, turn_count=0)

    def _env(self, events):
        """构造 _ask_stream_via_plan 的 mock 上下文"""
        stack = ExitStack()
        stack.enter_context(patch('apps.agent.plan.plan_execute_stream',
                                  return_value=iter(events)))
        trace = MagicMock()
        stack.enter_context(patch('apps.agent.models.AgentTrace', trace))
        stack.enter_context(patch('apps.agent.executor.LlmCallLog'))
        persist = MagicMock(return_value=MagicMock(id=10))
        stack.enter_context(patch('apps.agent.executor._persist_qa', persist))
        update_cache = MagicMock()
        stack.enter_context(patch('apps.agent.executor._update_cache', update_cache))
        mm = MagicMock()
        stack.enter_context(patch('apps.agent.executor.MemoryManager', return_value=mm))
        return stack, {'trace': trace, 'persist': persist, 'update_cache': update_cache, 'mm': mm}

    @staticmethod
    def _done_event(**overrides):
        ev = {
            'type': 'done', 'answer': 'Plan 答案', 'citations': [{'doc_title': 'A'}],
            'tool_traces': [{'round': 1, 'tool_name': 'calculator'}],
            'chunks': [{'chunk_id': 1}], 'is_filtered': False,
            'stats': {'llm': {}, 'tool_rounds': 1},
        }
        ev.update(overrides)
        return ev

    def test_plan_when_forward_events_then_persists(self):
        """转发 tool_call/tool_result/delta 事件，done 统一落库（answer_type='plan'）"""
        from apps.agent.executor import _ask_stream_via_plan
        stack, m = self._env([
            {'type': 'first_token', 'ttfb_ms': 2},
            {'type': 'tool_call', 'call_id': 'c1', 'tool_name': 'calculator',
             'tool_args': {'expr': '2+2'}},
            {'type': 'tool_result', 'call_id': 'c1', 'tool_name': 'calculator', 'ok': True},
            {'type': 'delta', 'delta': '答案'},
            self._done_event(),
        ])
        with stack:
            events = list(_ask_stream_via_plan(None, '问题', self._session(),
                                               ['company_doc'], None, 'company_doc', 1, 0,
                                               ['doc']))
        types = [e['type'] for e in events]
        assert types[0] == 'start'
        assert 'tool_call' in types
        assert types[-1] == 'done'
        assert events[-1]['message_id'] == 10
        kw = m['persist'].call_args.kwargs
        assert kw['answer_type'] == 'plan'
        assert kw['answer'] == 'Plan 答案'
        # answer_type='plan' 不在 _should_update_cache 的可缓存集合中，不更新缓存
        m['update_cache'].assert_not_called()

    def test_plan_when_content_filtered_then_refused(self):
        """Plan 内部命中 block：answer 存空串 + answer_type='refused'"""
        from apps.agent.executor import _ask_stream_via_plan
        stack, m = self._env([
            {'type': 'content_filtered', 'category': 'porn'},
            self._done_event(answer='', citations=[], tool_traces=[], chunks=[],
                             is_filtered=True, filter_reason='output:词',
                             stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            events = list(_ask_stream_via_plan(None, '问题', self._session(),
                                               ['company_doc'], None, 'company_doc', 1, 0,
                                               ['doc']))
        assert 'content_filtered' in [e['type'] for e in events]
        kw = m['persist'].call_args.kwargs
        assert kw['answer'] == ''
        assert kw['answer_type'] == 'refused'
        m['update_cache'].assert_not_called()

    def test_plan_when_error_event_then_detected(self):
        """Plan 内部 error 事件：error_type 被检测"""
        from apps.agent.executor import _ask_stream_via_plan
        stack, m = self._env([
            {'type': 'error', 'detail': 'Plan 内部错误'},
            self._done_event(answer='', citations=[], tool_traces=[], chunks=[],
                             stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            events = list(_ask_stream_via_plan(None, '问题', self._session(),
                                               ['company_doc'], None, 'company_doc', 1, 0,
                                               ['doc']))
        assert events[1]['type'] == 'error'
        kw = m['persist'].call_args.kwargs
        assert kw['answer_type'] == 'refused'

    def test_plan_when_no_answer_no_traces_then_refused(self):
        """无答案且无工具调用链：'[未生成内容]' + refused"""
        from apps.agent.executor import _ask_stream_via_plan
        stack, m = self._env([
            self._done_event(answer='', citations=[], tool_traces=[], chunks=[],
                             stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            events = list(_ask_stream_via_plan(None, '问题', self._session(),
                                               ['company_doc'], None, 'company_doc', 1, 0,
                                               ['doc']))
        kw = m['persist'].call_args.kwargs
        assert kw['answer'] == '[未生成内容]'
        assert kw['answer_type'] == 'refused'
        m['update_cache'].assert_not_called()

    def test_plan_when_client_abort_then_persists_partial(self):
        """客户端断开：保存已生成的部分答案"""
        from apps.agent.executor import _ask_stream_via_plan
        stack, m = self._env([
            {'type': 'delta', 'delta': '部分'},
            self._done_event(),
        ])
        with stack:
            gen = _ask_stream_via_plan(None, '问题', self._session(),
                                       ['company_doc'], None, 'company_doc', 1, 0, ['doc'])
            ev = next(gen)  # start
            assert ev['type'] == 'start'
            ev = next(gen)  # delta
            assert ev['type'] == 'delta'
            gen.close()
            kw = m['persist'].call_args.kwargs
            assert kw['answer'] == '部分'
            assert kw['answer_type'] == 'plan'

    def test_plan_when_client_abort_filtered_then_persists_empty(self):
        """客户端断开 + is_filtered=True：保存空 answer"""
        from apps.agent.executor import _ask_stream_via_plan
        stack, m = self._env([
            {'type': 'content_filtered', 'category': 'porn'},
            self._done_event(answer='', is_filtered=True, filter_reason='output:词',
                             stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            gen = _ask_stream_via_plan(None, '问题', self._session(),
                                       ['company_doc'], None, 'company_doc', 1, 0, ['doc'])
            next(gen)  # start
            next(gen)  # content_filtered
            gen.close()
            kw = m['persist'].call_args.kwargs
            assert kw['answer'] == ''
            assert kw['is_filtered'] is True

    def test_plan_when_trace_failure_then_non_fatal(self):
        """AgentTrace 批量写入失败不影响主流程"""
        from apps.agent.executor import _ask_stream_via_plan
        stack, m = self._env([self._done_event()])
        with stack:
            with patch('apps.agent.models.AgentTrace.batch_create_from_traces',
                       side_effect=Exception('trace fail')):
                events = list(_ask_stream_via_plan(None, '问题', self._session(),
                                                   ['company_doc'], None, 'company_doc',
                                                   1, 0, ['doc']))
        assert events[-1]['type'] == 'done'

    def test_plan_when_llm_call_log_exception_then_non_fatal(self):
        """LlmCallLog 写入失败不影响 Plan 主流程"""
        from apps.agent.executor import _ask_stream_via_plan
        stack, m = self._env([self._done_event()])
        with stack:
            with patch('apps.agent.executor.LlmCallLog',
                       side_effect=Exception('db down')):
                events = list(_ask_stream_via_plan(None, '问题', self._session(),
                                                   ['company_doc'], None, 'company_doc',
                                                   1, 0, ['doc']))
        assert events[-1]['type'] == 'done'

    def test_plan_when_answer_only_no_traces_then_general(self):
        """有答案但无工具调用链：answer_type='general'"""
        from apps.agent.executor import _ask_stream_via_plan
        stack, m = self._env([
            self._done_event(answer='直接回答', citations=[], tool_traces=[],
                             chunks=[], stats={'llm': {}, 'tool_rounds': 0}),
        ])
        with stack:
            events = list(_ask_stream_via_plan(None, '问题', self._session(),
                                               ['company_doc'], None, 'company_doc',
                                               1, 0, ['doc']))
        kw = m['persist'].call_args.kwargs
        assert kw['answer_type'] == 'general'


# ---------------------------------------------------------------------------
# _ask_stream_via_workflow
# ---------------------------------------------------------------------------

class TestAskStreamViaWorkflow:
    """_ask_stream_via_workflow：多 Agent 工作流流式问答入口"""

    @staticmethod
    def _session():
        return MagicMock(id=1, turn_count=0)

    def test_workflow_when_planner_says_no_workflow_then_fallback_react(self):
        """编排器判断不需要工作流：降级走 ReAct 单轮链路"""
        from apps.agent.executor import _ask_stream_via_workflow
        react_events = [
            {'type': 'first_token', 'ttfb_ms': 5},
            {'type': 'delta', 'delta': 'React 答案'},
            {'type': 'done', 'answer': 'React 答案', 'citations': [],
             'tool_traces': [], 'chunks': [], 'is_filtered': False,
             'stats': {'llm': {}, 'tool_rounds': 0}},
        ]
        with patch('apps.agent.workflow.planner.maybe_plan',
                   return_value={'need_workflow': False, 'reason': '简单问题'}), \
                patch('apps.agent.react.agent_ask_stream',
                      return_value=iter(react_events)), \
                patch('apps.agent.models.AgentTrace'), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)), \
                patch('apps.agent.executor._update_cache'), \
                patch('apps.agent.executor.MemoryManager') as mock_mm_cls:
            events = list(_ask_stream_via_workflow(None, '简单问题', self._session(),
                                                   ['company_doc'], None, 'company_doc', 1, 0,
                                                   ['doc']))
        # 第一个事件是 workflow_planning 提示
        assert events[0]['type'] == 'workflow_planning'
        # 之后走 ReAct 链路（含 start → first_token → delta → done）
        types = [e['type'] for e in events[1:]]
        assert 'start' in types
        assert 'delta' in types
        assert 'done' in types

    def test_workflow_when_planner_exception_then_fallback_react(self):
        """编排器异常：降级走 ReAct 单轮链路"""
        from apps.agent.executor import _ask_stream_via_workflow
        react_events = [
            {'type': 'first_token', 'ttfb_ms': 5},
            {'type': 'delta', 'delta': '降级答案'},
            {'type': 'done', 'answer': '降级答案', 'citations': [],
             'tool_traces': [], 'chunks': [], 'is_filtered': False,
             'stats': {'llm': {}, 'tool_rounds': 0}},
        ]
        with patch('apps.agent.workflow.planner.maybe_plan',
                   side_effect=Exception('planner down')), \
                patch('apps.agent.react.agent_ask_stream',
                      return_value=iter(react_events)), \
                patch('apps.agent.models.AgentTrace'), \
                patch('apps.agent.executor.LlmCallLog'), \
                patch('apps.agent.executor._persist_qa',
                      return_value=MagicMock(id=1)), \
                patch('apps.agent.executor._update_cache'), \
                patch('apps.agent.executor.MemoryManager'):
            events = list(_ask_stream_via_workflow(None, '问题', self._session(),
                                                   ['company_doc'], None, 'company_doc', 1, 0,
                                                   ['doc']))
        assert events[0]['type'] == 'workflow_planning'
        # 降级走 ReAct
        done_events = [e for e in events if e['type'] == 'done']
        assert len(done_events) >= 1

    def test_workflow_when_need_workflow_then_runs_engine(self):
        """编排器判断需要工作流：走 run_workflow_stream"""
        from apps.agent.executor import _ask_stream_via_workflow
        wf_events = [
            {'type': 'workflow_start', 'workflow_id': 99},
            {'type': 'workflow_node_start', 'node_id': 'r1'},
            {'type': 'workflow_node_done', 'node_id': 'r1', 'status': 'succeeded'},
            {'type': 'workflow_node_start', 'node_id': 'finalize'},
            {'type': 'workflow_node_done', 'node_id': 'finalize', 'status': 'succeeded'},
            {'type': 'first_token', 'ttfb_ms': 0},
            {'type': 'delta', 'delta': '工作流答案'},
            {'type': 'done', 'workflow_id': 99, 'message_id': 1, 'status': 'succeeded',
             'is_workflow': True, 'answer_type': 'agent'},
        ]
        with patch('apps.agent.workflow.planner.maybe_plan',
                   return_value={'need_workflow': True, 'reason': '复杂问题',
                                 'nodes': [{'id': 'r1', 'type': 'research',
                                            'question': '子问题'}]}), \
                patch('apps.agent.workflow.engine.run_workflow_stream',
                      return_value=iter(wf_events)):
            events = list(_ask_stream_via_workflow(None, '复杂问题', self._session(),
                                                   ['company_doc'], None, 'company_doc', 1, 0,
                                                   ['doc']))
        assert events[0]['type'] == 'workflow_planning'
        types = [e['type'] for e in events]
        assert 'workflow_start' in types
        assert 'start' in types
        # 最后一个事件应是 run_workflow_stream 的 done
        done_events = [e for e in events if e['type'] == 'done']
        assert done_events[-1]['is_workflow'] is True
