"""
agent.executor 单元测试（工具函数 + 缓存 + ask_stream mode 分流）

覆盖：
- _detect_error_type 错误分类
- _check_full_text 内容审查
- _normalize / _hash 工具函数
- _cache_scope 缓存作用域
- _make_filtered_event 事件构造
- _build_citations 引用组装
- ask_stream() mode 分流（流式完整链路见 test_executor_stream.py）
- _persist_qa / _try_cache / _update_cache 缓存三件套

Mock 说明：
- executor 顶层 `from apps.llm.factory import get_llm` 等 import 会把名称绑定进
  executor 命名空间，因此必须 patch 使用点 apps.agent.executor.*。
- 函数内 import（如 analytics.realtime.* 等）按定义处 patch。
"""
import pytest
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.utils import timezone

from apps.users.models import User
from apps.memory.models import Session
from apps.chat.models import HotQaCache

pytestmark = pytest.mark.unit


def _chunk(cid, doc_id, title='文档A', section='s1', page=1, rrf=0.5, rerank=0.6):
    """构造一个符合 executor 处理的 chunk 结构"""
    return {
        'chunk_id': cid, 'document_id': doc_id, 'doc_title': title,
        'section_path': section, 'page_number': page,
        'rrf_score': rrf, 'rerank_score': rerank,
    }


# ---------------------------------------------------------------------------
# _detect_error_type
# ---------------------------------------------------------------------------

class TestDetectErrorType:
    """_detect_error_type 错误类型分类测试"""

    def test_detect_when_empty_message_then_returns_unknown(self):
        from apps.agent.executor import _detect_error_type
        assert _detect_error_type({}) == ''
        assert _detect_error_type({'error': None}) == ''
        assert _detect_error_type({'error': ''}) == ''

    def test_detect_when_timeout_then_returns_timeout(self):
        from apps.agent.executor import _detect_error_type
        assert _detect_error_type({'error': 'Request timeout'}) == 'timeout'
        assert _detect_error_type({'error': 'timed out after 30s'}) == 'timeout'

    def test_detect_when_rate_limit_then_returns_rate_limit(self):
        from apps.agent.executor import _detect_error_type
        assert _detect_error_type({'error': 'Rate limit exceeded'}) == 'rate_limit'
        assert _detect_error_type({'error': 'HTTP 429 Too Many Requests'}) == 'rate_limit'
        assert _detect_error_type({'error': 'too many requests'}) == 'rate_limit'

    def test_detect_when_network_error_then_returns_network(self):
        from apps.agent.executor import _detect_error_type
        assert _detect_error_type({'error': 'Connection reset by peer'}) == 'network'
        assert _detect_error_type({'error': 'Network connection lost'}) == 'network'
        assert _detect_error_type({'error': 'connection refused'}) == 'network'

    def test_detect_when_content_filter_then_returns_content_filter(self):
        from apps.agent.executor import _detect_error_type
        assert _detect_error_type({'error': 'content filter triggered policy violation'}) == 'content_filter'

    def test_detect_when_embedding_error_then_returns_embedding(self):
        from apps.agent.executor import _detect_error_type
        assert _detect_error_type({'error': 'embedding service down'}) == 'embedding_error'

    def test_detect_when_server_error_then_returns_server_error(self):
        from apps.agent.executor import _detect_error_type
        assert _detect_error_type({'error': 'Internal Server Error 500'}) == 'server_error'
        assert _detect_error_type({'error': 'Bad Gateway 502'}) == 'server_error'

    def test_detect_when_unknown_error_then_returns_unknown(self):
        from apps.agent.executor import _detect_error_type
        assert _detect_error_type({'error': 'Some weird error'}) == 'unknown'


# ---------------------------------------------------------------------------
# _check_full_text
# ---------------------------------------------------------------------------

class TestCheckFullText:
    """_check_full_text 内容审查测试"""

    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_check_full_text_when_empty_then_returns_text(self, mock_get_sf):
        from apps.agent.executor import _check_full_text
        text, hit = _check_full_text('')
        assert text == ''
        assert hit is None
        mock_get_sf.assert_not_called()

    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_check_full_text_when_no_hits_then_returns_text(self, mock_get_sf):
        mock_sf = MagicMock()
        mock_sf.check.return_value = []
        mock_get_sf.return_value = mock_sf

        from apps.agent.executor import _check_full_text
        text, hit = _check_full_text('This is a clean text with no sensitive content.')
        assert text == 'This is a clean text with no sensitive content.'
        assert hit is None

    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_check_full_text_when_block_hit_then_returns_blocked(self, mock_get_sf):
        mock_sf = MagicMock()
        block_hit = MagicMock()
        block_hit.action = 'block'
        mock_sf.check.return_value = [block_hit]
        mock_get_sf.return_value = mock_sf

        from apps.agent.executor import _check_full_text
        text, hit = _check_full_text('This text has blocked content.')
        assert text == 'This text has blocked content.'
        assert hit is block_hit

    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_check_full_text_when_mask_hit_then_returns_masked(self, mock_get_sf):
        mock_sf = MagicMock()
        mask_hit = MagicMock()
        mask_hit.action = 'mask'
        mask_hit.start = 5
        mask_hit.end = 10
        mock_sf.MASK_STR = '***'
        mock_sf.check.return_value = [mask_hit]
        mock_get_sf.return_value = mock_sf

        from apps.agent.executor import _check_full_text
        text, hit = _check_full_text('Hello World Test')
        assert hit is None
        assert '***' in text

    @patch('apps.security.sensitive_filter.get_sensitive_filter')
    def test_check_full_text_when_filter_raises_then_returns_text(self, mock_get_sf):
        mock_get_sf.side_effect = Exception('Sensitive filter broken')
        from apps.agent.executor import _check_full_text
        text, hit = _check_full_text('Some text')
        assert text == 'Some text'
        assert hit is None


# ---------------------------------------------------------------------------
# _normalize / _hash
# ---------------------------------------------------------------------------

class TestNormalizeAndHash:
    """_normalize / _hash 工具函数测试"""

    def test_normalize_when_whitespace_then_collapsed(self):
        from apps.agent.executor import _normalize
        assert _normalize('  Hello  World  ') == 'helloworld'
        assert _normalize('HelloWorld') == 'helloworld'

    def test_normalize_when_mixed_case_then_lowercased(self):
        from apps.agent.executor import _normalize
        assert _normalize('HELLO') == 'hello'

    def test_normalize_when_empty_then_returns_empty(self):
        from apps.agent.executor import _normalize
        assert _normalize('') == ''

    def test_hash_when_same_input_then_deterministic(self):
        from apps.agent.executor import _hash
        h1 = _hash('test question')
        h2 = _hash('test question')
        assert h1 == h2

    def test_hash_when_different_inputs_then_different_hash(self):
        from apps.agent.executor import _hash
        h1 = _hash('question A')
        h2 = _hash('question B')
        assert h1 != h2

    def test_hash_then_returns_fixed_length(self):
        from apps.agent.executor import _hash
        h = _hash('test')
        assert len(h) == 64  # SHA256 hex digest


# ---------------------------------------------------------------------------
# _cache_scope
# ---------------------------------------------------------------------------

class TestCacheScope:
    """_cache_scope 缓存作用域判定"""

    def test_cache_scope_when_anonymous_then_returns_global_key(self):
        from apps.agent.executor import _cache_scope
        assert _cache_scope(None) == 'anonymous'

    def test_cache_scope_when_not_authenticated_then_returns_global_key(self):
        from apps.agent.executor import _cache_scope
        user = MagicMock()
        user.is_authenticated = False
        assert _cache_scope(user) == 'anonymous'

    def test_cache_scope_when_super_admin_then_returns_global_key(self):
        from apps.agent.executor import _cache_scope
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = True
        assert _cache_scope(user) == 'super'

    def test_cache_scope_when_normal_user_then_returns_user_specific_key(self):
        from apps.agent.executor import _cache_scope
        user = MagicMock()
        user.is_authenticated = True
        user.is_super_admin = False
        user.id = 42
        assert _cache_scope(user) == 'user_42'


# ---------------------------------------------------------------------------
# _make_filtered_event
# ---------------------------------------------------------------------------

class TestMakeFilteredEvent:
    """_make_filtered_event：content_filtered 事件构造"""

    def test_make_filtered_event_when_has_category_then_uses_category(self):
        from apps.agent.executor import _make_filtered_event
        hit = MagicMock(category='porn')
        ev = _make_filtered_event(hit)
        assert ev['type'] == 'content_filtered'
        assert ev['reason'] == '检测到违规内容，已拦截'
        assert ev['category'] == 'porn'

    def test_make_filtered_event_when_no_category_then_falls_back(self):
        from apps.agent.executor import _make_filtered_event
        class _Hit:  # 无 category 属性的普通对象
            pass
        assert _make_filtered_event(_Hit())['category'] == 'other'


# ---------------------------------------------------------------------------
# _build_citations
# ---------------------------------------------------------------------------

class TestBuildCitations:
    """_build_citations：按文档合并 chunks 组装引用"""

    def test_build_citations_when_multiple_chunks_then_merges_by_doc(self):
        from apps.agent.executor import _build_citations
        chunks = [
            _chunk(1, 101, title='文档A', section='s1', page=1),
            _chunk(2, 101, title='文档A', section='s2', page=2),
            _chunk(3, 102, title='文档B', section=None, page=None),
        ]
        citations = _build_citations(chunks)
        assert len(citations) == 2
        doc_a = citations[0]
        assert doc_a['index'] == 1
        assert sorted(doc_a['section'].split(', ')) == ['s1', 's2']
        assert doc_a['page'] == [1, 2]
        assert doc_a['chunk_ids'] == [1, 2]
        assert citations[1]['doc_title'] == '文档B'
        assert citations[1]['section'] == ''
        assert citations[1]['page'] == []

    def test_build_citations_when_sections_over_3_then_appends_ellipsis(self):
        """同一文档命中超过 3 个章节时，引用 section 需以 '...' 结尾"""
        from apps.agent.executor import _build_citations
        chunks = [_chunk(i, 101, title='文档A', section=f's{i}', page=1) for i in range(5)]
        citations = _build_citations(chunks)
        assert citations[0]['section'].endswith('...')

    def test_build_citations_when_missing_doc_title_then_uses_default(self):
        """缺少 doc_title 时兜底为 '未知文档'"""
        from apps.agent.executor import _build_citations
        chunks = [{'chunk_id': 1, 'document_id': 101, 'section_path': 's', 'page_number': 1}]
        citations = _build_citations(chunks)
        assert citations[0]['doc_title'] == '未知文档'

    def test_build_citations_when_empty_chunks_then_returns_empty(self):
        from apps.agent.executor import _build_citations
        assert _build_citations([]) == []


# ---------------------------------------------------------------------------
# ask_stream() mode 分流（流式完整链路见 test_executor_stream.py）
# ---------------------------------------------------------------------------

class TestAskStreamModeBranching:
    """ask_stream() 的 mode 分流逻辑测试"""

    @patch('apps.agent.executor._try_cache')
    @patch('apps.agent.executor._ask_stream_via_agent')
    def test_ask_stream_when_mode_auto_then_calls_agent_stream(self, mock_agent_stream, mock_cache):
        """mode='auto' 应 yield from _ask_stream_via_agent"""
        mock_cache.return_value = None
        mock_agent_stream.return_value = iter([
            {'type': 'start'},
            {'type': 'delta', 'delta': 'hello'},
            {'type': 'done'},
        ])

        from apps.agent.executor import ask_stream
        user = MagicMock()
        session = MagicMock()
        session.id = 1
        session.turn_count = 0

        events = list(ask_stream(user, 'q', session, mode='auto'))
        types = [e['type'] for e in events]
        assert 'start' in types
        assert 'delta' in types

    @patch('apps.agent.executor._try_cache')
    @patch('apps.agent.executor._ask_stream_via_route')
    def test_ask_stream_when_mode_rag_then_calls_route_stream(self, mock_route_stream, mock_cache):
        """mode='rag' 应 yield from _ask_stream_via_route"""
        mock_cache.return_value = None
        mock_route_stream.return_value = iter([
            {'type': 'start', 'route_source': 'rag'},
            {'type': 'delta', 'delta': 'answer'},
            {'type': 'done'},
        ])

        from apps.agent.executor import ask_stream
        user = MagicMock()
        session = MagicMock()
        session.id = 1
        session.turn_count = 0

        events = list(ask_stream(user, 'q', session, mode='rag'))
        assert events[0]['route_source'] == 'rag'


# ---------------------------------------------------------------------------
# _persist_qa（真实 DB）
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestPersistQa:
    """_persist_qa：QaRecord 落库 + tokens_per_second + 实时指标/评估容错"""

    @pytest.fixture(autouse=True)
    def _qa_env(self):
        """pytest fixture：注入用户与会话"""
        self.user = User.objects.create_user(
            username='persist_user', email='persist@example.com', password='x')
        self.session = Session.objects.create(user=self.user, title='测试会话')

    def _persist(self, **overrides):
        """以默认参数调用 _persist_qa，允许用例覆盖任意键"""
        from apps.agent.executor import _persist_qa
        kwargs = dict(
            user=self.user, session=self.session, question='测试问题', answer='测试答案',
            citations=[{'doc_title': 'A'}], retrieval_hits=[1, 2],
            retrieval_scores=[{'chunk_id': 1, 'rrf': 0.5, 'rerank': 0.6}],
            stats={'latency_total_ms': 100, 'latency_retrieval_ms': 10},
            llm_stats={'latency_llm_ms': 50, 'tokens_prompt': 5, 'tokens_completion': 10,
                       'cost': 0.01, 'llm_provider': 'deepseek', 'llm_model': 'deepseek-chat'},
            root_type='company_doc', turn_index=3, answer_type='rag',
        )
        kwargs.update(overrides)
        return _persist_qa(**kwargs)

    @patch('apps.analytics.realtime.increment_realtime_metrics')
    @patch('apps.analytics.production_eval.maybe_dispatch_eval')
    def test_persist_qa_when_full_fields_then_created(self, mock_eval, mock_metrics):
        """全字段落库 + 实时指标与评估派发各调用一次"""
        qa = self._persist(
            is_filtered=True, filter_reason='output:xxx',
            error_type='timeout', is_success=False,
            route_source='wiki', route_trace=[{'layer': 'wiki'}],
        )
        qa.refresh_from_db()
        assert qa.question == '测试问题'
        assert qa.answer == '测试答案'
        assert qa.turn_index == 3
        assert qa.retrieval_hits == [1, 2]
        assert qa.retrieval_scores == [{'chunk_id': 1, 'rrf': 0.5, 'rerank': 0.6}]
        assert qa.citations == [{'doc_title': 'A'}]
        assert qa.latency_total_ms == 100
        assert qa.latency_retrieval_ms == 10
        assert qa.latency_llm_ms == 50
        assert qa.tokens_prompt == 5
        assert qa.tokens_completion == 10
        assert qa.cost_estimate == Decimal('0.01')
        assert qa.llm_provider == 'deepseek'
        assert qa.error_type == 'timeout'
        assert qa.is_success is False
        assert qa.is_filtered is True
        assert qa.filter_reason == 'output:xxx'
        assert qa.route_source == 'wiki'
        assert qa.route_trace == [{'layer': 'wiki'}]
        mock_metrics.assert_called_once_with(qa)
        mock_eval.assert_called_once_with(qa)

    @patch('apps.analytics.realtime.increment_realtime_metrics')
    @patch('apps.analytics.production_eval.maybe_dispatch_eval')
    def test_persist_qa_when_success_then_tokens_per_second_computed(self, mock_eval, mock_metrics):
        """成功且非缓存的请求按 completion_tokens / llm_耗时 计算速率"""
        qa = self._persist(llm_stats={'latency_llm_ms': 1000, 'tokens_completion': 100,
                                      'tokens_prompt': 1, 'cost': 0})
        assert qa.tokens_per_second == 100.0

    @patch('apps.analytics.realtime.increment_realtime_metrics')
    @patch('apps.analytics.production_eval.maybe_dispatch_eval')
    def test_persist_qa_when_cache_hit_then_no_tps(self, mock_eval, mock_metrics):
        """缓存命中不计算生成速率（tokens 属于历史内容）"""
        qa = self._persist(is_hit_cache=True, llm_stats={'tokens_completion': 100})
        assert qa.tokens_per_second == 0.0

    @patch('apps.analytics.realtime.increment_realtime_metrics',
           side_effect=RuntimeError('redis down'))
    @patch('apps.analytics.production_eval.maybe_dispatch_eval',
           side_effect=RuntimeError('celery down'))
    def test_persist_qa_when_metrics_errors_then_non_fatal(self, mock_eval, mock_metrics):
        """实时指标/评估派发失败不阻断 QaRecord 保存"""
        qa = self._persist()
        assert qa.id is not None

    @patch('apps.analytics.realtime.increment_realtime_metrics')
    @patch('apps.analytics.production_eval.maybe_dispatch_eval')
    def test_persist_qa_when_llm_stats_has_error_then_writes_error_message(self, mock_eval, mock_metrics):
        """llm_stats.error 写入 error_message 字段（audit 用）"""
        qa = self._persist(llm_stats={'error': 'Request timeout'}, error_type='timeout',
                           is_success=False)
        assert qa.error_message == 'Request timeout'


# ---------------------------------------------------------------------------
# _try_cache / _update_cache（真实 DB）
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestTryCache:
    """_try_cache：热点缓存命中逻辑（含权限二次校验）"""

    @pytest.fixture(autouse=True)
    def _cache_env(self):
        """pytest fixture：注入用户与会话"""
        self.user = User.objects.create_user(
            username='cache_user', email='cache@example.com', password='x')
        self.session = Session.objects.create(user=self.user, title='测试会话')

    def _mk_cache(self, **kwargs):
        from apps.agent.executor import _hash
        defaults = dict(
            question_hash=_hash('测试问题'), root_type='company_doc',
            visibility_scope='anonymous', question='测试问题', answer='缓存答案',
            citations=[], hit_count=0,
        )
        defaults.update(kwargs)
        return HotQaCache.objects.create(**defaults)

    def test_try_cache_when_no_cache_then_returns_none(self):
        from apps.agent.executor import _try_cache
        assert _try_cache('测试问题', 'company_doc', None) is None

    def test_try_cache_when_hit_then_increments_and_returns(self):
        """匿名命中：返回 answer/citations 且 hit_count 自增"""
        from apps.agent.executor import _try_cache
        obj = self._mk_cache()
        result = _try_cache('测试问题', 'company_doc', None)
        assert result['answer'] == '缓存答案'
        obj.refresh_from_db()
        assert obj.hit_count == 1

    def test_try_cache_when_expired_then_returns_none(self):
        from apps.agent.executor import _try_cache
        self._mk_cache(expires_at=timezone.now() - timedelta(hours=1))
        assert _try_cache('测试问题', 'company_doc', None) is None

    def test_try_cache_when_auth_user_then_can_hit_super_scope(self):
        """已登录普通用户可命中 super 作用域缓存（scopes 追加 'super'）"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope='super')
        result = _try_cache('测试问题', 'company_doc', self.user)
        assert result is not None

    def test_try_cache_when_permission_revoked_then_skips(self):
        """缓存引用文档的权限已被回收时跳过缓存"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope=f'user_{self.user.id}',
                       citations=[{'chunk_ids': [999]}])
        mock_chunk = MagicMock(document_id=123)
        with patch('apps.knowledge.models.DocumentChunk.objects.filter') as mock_qs, \
                patch('apps.agent.executor.filter_accessible_doc_ids', return_value=[]) as mock_filter:
            mock_qs.return_value.first.return_value = mock_chunk
            assert _try_cache('测试问题', 'company_doc', self.user) is None
            mock_filter.assert_called_once_with(self.user, [123])

    def test_try_cache_when_permission_allowed_then_returns(self):
        """权限仍有效时正常命中缓存"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope=f'user_{self.user.id}',
                       citations=[{'chunk_ids': [999]}])
        mock_chunk = MagicMock(document_id=123)
        with patch('apps.knowledge.models.DocumentChunk.objects.filter') as mock_qs, \
                patch('apps.agent.executor.filter_accessible_doc_ids', return_value=[123]):
            mock_qs.return_value.first.return_value = mock_chunk
            result = _try_cache('测试问题', 'company_doc', self.user)
            assert result['answer'] == '缓存答案'

    def test_try_cache_when_citation_chunk_missing_then_skips_check(self):
        """引用对应的 chunk 已删除时跳过权限校验直接命中"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope=f'user_{self.user.id}',
                       citations=[{'chunk_ids': [999]}])
        with patch('apps.knowledge.models.DocumentChunk.objects.filter') as mock_qs, \
                patch('apps.agent.executor.filter_accessible_doc_ids') as mock_filter:
            mock_qs.return_value.first.return_value = None
            result = _try_cache('测试问题', 'company_doc', self.user)
            assert result['answer'] == '缓存答案'
            mock_filter.assert_not_called()


@pytest.mark.django_db
class TestUpdateCache:
    """_update_cache：写/更新热点缓存（失败静默）"""

    @pytest.fixture(autouse=True)
    def _upd_env(self):
        """pytest fixture：注入用户"""
        self.user = User.objects.create_user(
            username='upd_user', email='upd@example.com', password='x')

    def test_update_cache_when_new_then_creates(self):
        from apps.agent.executor import _update_cache, _hash
        _update_cache('问题', 'company_doc', None, '答案', [{'doc_title': 'A'}])
        obj = HotQaCache.objects.get(question_hash=_hash('问题'))
        assert obj.answer == '答案'
        assert obj.citations == [{'doc_title': 'A'}]
        assert obj.question == '问题'

    def test_update_cache_when_existing_then_updates(self):
        from apps.agent.executor import _update_cache, _hash
        HotQaCache.objects.create(question_hash=_hash('问题'), root_type='company_doc',
                                  visibility_scope='anonymous', question='问题',
                                  answer='旧答案', citations=[])
        _update_cache('问题', 'company_doc', None, '新答案', [])
        obj = HotQaCache.objects.get(question_hash=_hash('问题'))
        assert obj.answer == '新答案'

    def test_update_cache_when_exception_then_logged_not_raised(self):
        """缓存写入失败仅记录日志，不向上抛异常"""
        from apps.agent.executor import _update_cache
        with patch('apps.agent.executor.HotQaCache') as mock_cache_cls:
            mock_cache_cls.objects.update_or_create.side_effect = Exception('db down')
            _update_cache('问题', 'company_doc', None, '答案', [])  # 不应抛异常
