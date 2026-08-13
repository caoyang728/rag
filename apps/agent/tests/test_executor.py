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
# _build_org_scope
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestBuildOrgScope:
    """_build_org_scope：按引用文档组织归属计算缓存权限组"""

    def _make_doc(self, node, owner, dept_id=None, team_id=None,
                  visibility_level='PUBLIC'):
        # 同一 node 下 (file_name, version_tag) 唯一，需按序递增避免重复创建冲突
        from apps.knowledge.models import Document
        self._doc_seq = getattr(self, '_doc_seq', 0) + 1
        return Document.objects.create(
            node=node, owner=owner, title='缓存测试文档',
            file_name=f't{self._doc_seq}.txt',
            file_type='txt', file_hash='h', root_type='test_root',
            dept_id=dept_id, team_id=team_id, visibility_level=visibility_level,
        )

    @pytest.fixture(autouse=True)
    def _org_env(self):
        """pytest fixture：注入文档 Owner 与根节点"""
        from apps.knowledge.models import KnowledgeNode
        self.owner = User.objects.create_user(
            username='org_owner', email='org@example.com', password='x')
        self.node = KnowledgeNode.objects.create(
            name='root', node_type='root', root_type='test_root',
            created_by=self.owner)

    def test_build_org_scope_when_no_docs_then_public(self):
        from apps.agent.executor import _build_org_scope
        assert _build_org_scope([]) == 'public'

    def test_build_org_scope_when_all_public_docs_then_public(self):
        from apps.agent.executor import _build_org_scope
        doc = self._make_doc(self.node, self.owner, visibility_level='PUBLIC')
        assert _build_org_scope([doc.id]) == 'public'

    def test_build_org_scope_when_team_doc_then_org_team(self):
        from apps.agent.executor import _build_org_scope
        doc = self._make_doc(self.node, self.owner, team_id=7,
                             visibility_level='TEAM_ONLY')
        assert _build_org_scope([doc.id]) == 'org_t7'

    def test_build_org_scope_when_dept_doc_then_org_dept(self):
        from apps.agent.executor import _build_org_scope
        doc = self._make_doc(self.node, self.owner, dept_id=3,
                             visibility_level='DEPT_ONLY')
        assert _build_org_scope([doc.id]) == 'org_d3'

    def test_build_org_scope_when_mixed_orgs_then_sorted(self):
        from apps.agent.executor import _build_org_scope
        doc1 = self._make_doc(self.node, self.owner, dept_id=9,
                              visibility_level='DEPT_ONLY')
        doc2 = self._make_doc(self.node, self.owner, team_id=3,
                              visibility_level='TEAM_ONLY')
        assert _build_org_scope([doc1.id, doc2.id]) == 'org_d9_t3'

    def test_build_org_scope_when_public_and_team_then_only_team(self):
        from apps.agent.executor import _build_org_scope
        doc1 = self._make_doc(self.node, self.owner, visibility_level='PUBLIC')
        doc2 = self._make_doc(self.node, self.owner, team_id=5,
                              visibility_level='TEAM_ONLY')
        assert _build_org_scope([doc1.id, doc2.id]) == 'org_t5'


# ---------------------------------------------------------------------------
# _user_covers_org_scope
# ---------------------------------------------------------------------------

@pytest.mark.django_db
class TestUserCoversOrgScope:
    """_user_covers_org_scope：用户可见组织范围覆盖权限组判定"""

    @pytest.fixture(autouse=True)
    def _org_env(self):
        """pytest fixture：部门 A/B + 团队 A1（用户归属 A1）"""
        from apps.users.models import Department, Team
        self.dept_a = Department.objects.create(name='缓存部门A')
        self.dept_b = Department.objects.create(name='缓存部门B')
        self.team_a1 = Team.objects.create(name='团队A1', department=self.dept_a)
        self.user = User.objects.create_user(
            username='org_user', email='orguser@example.com', password='x')
        self.user.department_id = self.dept_a.id
        self.user.team_id = self.team_a1.id
        self.user.save()

    def test_covers_when_own_dept_then_true(self):
        from apps.agent.executor import _user_covers_org_scope
        assert _user_covers_org_scope(self.user, f'org_d{self.dept_a.id}') is True

    def test_covers_when_other_dept_then_false(self):
        from apps.agent.executor import _user_covers_org_scope
        assert _user_covers_org_scope(self.user, f'org_d{self.dept_b.id}') is False

    def test_covers_when_own_team_then_true(self):
        from apps.agent.executor import _user_covers_org_scope
        assert _user_covers_org_scope(self.user, f'org_t{self.team_a1.id}') is True

    def test_covers_when_team_in_visible_dept_then_true(self):
        """同部门其他团队：部门级可见覆盖下属团队"""
        from apps.users.models import Team
        from apps.agent.executor import _user_covers_org_scope
        team_a2 = Team.objects.create(name='团队A2', department=self.dept_a)
        assert _user_covers_org_scope(self.user, f'org_t{team_a2.id}') is True

    def test_covers_when_other_dept_team_then_false(self):
        from apps.users.models import Team
        from apps.agent.executor import _user_covers_org_scope
        team_b1 = Team.objects.create(name='团队B1', department=self.dept_b)
        assert _user_covers_org_scope(self.user, f'org_t{team_b1.id}') is False


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
            visibility_scope='public', question='测试问题', answer='缓存答案',
            citations=[], hit_count=0,
        )
        defaults.update(kwargs)
        return HotQaCache.objects.create(**defaults)

    def test_try_cache_when_no_cache_then_returns_none(self):
        from apps.agent.executor import _try_cache
        assert _try_cache('测试问题', 'company_doc', None) is None

    def test_try_cache_when_hit_then_increments_and_returns(self):
        """匿名命中 public 缓存（无引用）：返回 answer/citations 且 hit_count 自增"""
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

    def test_try_cache_when_auth_user_hits_public_group(self):
        """普通用户可命中 public 组缓存（无引用，无需权限校验）"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope='public', cited_doc_ids=[], citations=[])
        result = _try_cache('测试问题', 'company_doc', self.user)
        assert result['answer'] == '缓存答案'

    def test_try_cache_when_org_not_covered_then_skips(self):
        """组织组：用户可见组织不含该部门 → 跳过缓存（重新生成）"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope='org_d3', cited_doc_ids=[123])
        with patch('apps.agent.executor.build_user_context',
                   return_value={'visible_depts': {1}, 'visible_teams': set()}):
            assert _try_cache('测试问题', 'company_doc', self.user) is None

    def test_try_cache_when_org_covered_then_hits(self):
        """组织组：可见组织覆盖 + 引用文档可访问 → 命中"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope='org_d3', cited_doc_ids=[123])
        with patch('apps.agent.executor.build_user_context',
                   return_value={'visible_depts': {3}, 'visible_teams': set()}), \
                patch('apps.agent.executor.filter_accessible_doc_ids', return_value=[123]):
            result = _try_cache('测试问题', 'company_doc', self.user)
            assert result['answer'] == '缓存答案'

    def test_try_cache_when_org_covered_but_doc_blacklisted_then_skips(self):
        """组织覆盖通过但引用文档被黑名单拦截 → 文档级兜底不命中"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope='org_d3', cited_doc_ids=[123, 456])
        with patch('apps.agent.executor.build_user_context',
                   return_value={'visible_depts': {3}, 'visible_teams': set()}), \
                patch('apps.agent.executor.filter_accessible_doc_ids', return_value=[123]):
            # 456 被黑名单过滤掉 → 引用不全可访问 → 不命中
            assert _try_cache('测试问题', 'company_doc', self.user) is None

    def test_try_cache_when_org_groups_isolated_then_hits_own_group(self):
        """不同权限组各自独立缓存互不覆盖：用户命中自己可见的组织组"""
        from apps.agent.executor import _try_cache
        self._mk_cache(question='测试问题', visibility_scope='org_d3',
                       answer='部门3答案', cited_doc_ids=[123])
        self._mk_cache(question='测试问题', visibility_scope='org_d5',
                       answer='部门5答案', cited_doc_ids=[456])
        with patch('apps.agent.executor.build_user_context',
                   return_value={'visible_depts': {3}, 'visible_teams': set()}), \
                patch('apps.agent.executor.filter_accessible_doc_ids', return_value=[123]):
            result = _try_cache('测试问题', 'company_doc', self.user)
            assert result['answer'] == '部门3答案'

    def test_try_cache_when_no_citations_then_any_user_can_hit(self):
        """缓存无引用（纯 LLM 知识答案）时任意用户可命中，无需权限校验"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope='public', cited_doc_ids=[], citations=[])
        with patch('apps.agent.executor.filter_accessible_doc_ids') as mock_filter:
            result = _try_cache('测试问题', 'company_doc', self.user)
            assert result['answer'] == '缓存答案'
            mock_filter.assert_not_called()

    def test_try_cache_when_legacy_without_permission_group_then_skips(self):
        """旧数据无权限组标记（cited_doc_ids 空但有引用）：非超管用户保守跳过"""
        from apps.agent.executor import _try_cache
        self._mk_cache(visibility_scope='public', cited_doc_ids=[],
                       citations=[{'doc_title': 'A'}])
        with patch('apps.agent.executor.filter_accessible_doc_ids') as mock_filter:
            assert _try_cache('测试问题', 'company_doc', self.user) is None
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
        # 无 chunk_ids 的引用 → 权限组为空 → public 组（任意用户可命中）
        assert obj.cited_doc_ids == []
        assert obj.visibility_scope == 'public'

    def test_update_cache_when_citations_with_chunks_then_writes_permission_group(self):
        """缓存写入时提取引用文档集合；无对应文档归属 → 权限组为 public"""
        from apps.agent.executor import _update_cache, _hash
        with patch('apps.knowledge.models.DocumentChunk.objects.filter') as mock_qs:
            mock_qs.return_value.values_list.return_value.distinct.return_value = [123]
            _update_cache('问题', 'company_doc', None, '答案',
                          [{'doc_title': 'A', 'chunk_ids': [999]}])
        obj = HotQaCache.objects.get(question_hash=_hash('问题'))
        assert obj.cited_doc_ids == [123]
        assert obj.visibility_scope == 'public'

    def test_update_cache_when_citations_ref_team_doc_then_org_group(self):
        """引用团队归属文档 → 缓存权限组为 org_t{team_id}"""
        from apps.knowledge.models import KnowledgeNode, Document
        from apps.agent.executor import _update_cache, _hash
        node = KnowledgeNode.objects.create(
            name='root', node_type='root', root_type='test_root', created_by=self.user)
        doc = Document.objects.create(node=node, owner=self.user, title='doc',
                                      file_name='d.txt', file_type='txt', file_hash='h',
                                      root_type='test_root', team_id=7,
                                      visibility_level='TEAM_ONLY')
        with patch('apps.knowledge.models.DocumentChunk.objects.filter') as mock_qs:
            # 反查的 document_id 必须指向真实文档，_build_org_scope 才能算出组织归属
            mock_qs.return_value.values_list.return_value.distinct.return_value = [doc.id]
            _update_cache('问题', 'company_doc', self.user, '答案',
                          [{'doc_title': 'doc', 'chunk_ids': [999]}])
        obj = HotQaCache.objects.get(question_hash=_hash('问题'))
        assert obj.cited_doc_ids == [doc.id]
        # 权限组取文档的组织归属（team_id=7），非文档自身 id
        assert obj.visibility_scope == 'org_t7'

    def test_update_cache_when_existing_then_updates(self):
        from apps.agent.executor import _update_cache, _hash
        HotQaCache.objects.create(question_hash=_hash('问题'), root_type='company_doc',
                                  visibility_scope='public', question='问题',
                                  answer='旧答案', citations=[])
        _update_cache('问题', 'company_doc', None, '新答案', [])
        obj = HotQaCache.objects.get(question_hash=_hash('问题'))
        assert obj.answer == '新答案'

    def test_update_cache_when_exception_then_logged_not_raised(self):
        """缓存写入失败仅记录日志，不向上抛异常"""
        from apps.agent.executor import _update_cache
        with patch('apps.agent.executor.HotQaCache') as mock_cache_cls:
            mock_cache_cls.objects.get_or_create.side_effect = Exception('db down')
            _update_cache('问题', 'company_doc', None, '答案', [])  # 不应抛异常

    def test_update_cache_when_existing_then_preserves_hit_count(self):
        """已存在缓存记录时更新答案但保留 hit_count 累计（不被重置为 1）"""
        from apps.agent.executor import _update_cache, _hash
        HotQaCache.objects.create(question_hash=_hash('问题'), root_type='company_doc',
                                  visibility_scope='public', question='问题',
                                  answer='旧答案', citations=[], hit_count=7)
        _update_cache('问题', 'company_doc', None, '新答案', [{'doc_title': 'A'}])
        obj = HotQaCache.objects.get(question_hash=_hash('问题'))
        assert obj.answer == '新答案'
        assert obj.hit_count == 7

    def test_should_update_cache_when_general_and_not_filtered_then_true(self):
        """general（未调用工具）成功回答也应写缓存，否则同问题每次都完整 LLM 生成"""
        from apps.agent.executor import _should_update_cache
        assert _should_update_cache('general', False)
        assert _should_update_cache('agent', False)
        assert _should_update_cache('rag', False)

    def test_should_update_cache_when_refused_then_false(self):
        """拒答/无资料（refused）不写缓存：内容审查词库会更新，且无引用可复用"""
        from apps.agent.executor import _should_update_cache
        assert not _should_update_cache('refused', False)

    def test_should_update_cache_when_filtered_then_false(self):
        """审查拦截命中不写缓存，避免违规内容被缓存复用"""
        from apps.agent.executor import _should_update_cache
        assert not _should_update_cache('rag', True)
        assert not _should_update_cache('general', True)


# ---------------------------------------------------------------------------
# _normalize_sources / _enabled_source_set
# ---------------------------------------------------------------------------

class TestNormalizeSources:
    """_normalize_sources 来源规范化与系统配置过滤测试
    （对应 CHAT_SOURCE_ENABLED：前端只展示开启的来源，后端兜底剔除未开启来源）"""

    def test_normalize_when_sources_none_or_empty_then_all(self):
        from apps.agent.executor import _normalize_sources
        assert _normalize_sources(None) == {'doc', 'db', 'web', 'llm'}
        assert _normalize_sources([]) == {'doc', 'db', 'web', 'llm'}

    def test_normalize_when_all_invalid_then_fallback_all(self):
        """全部为非法值时回退全开（默认行为），避免旧调用方/前端缺省被拒绝"""
        from apps.agent.executor import _normalize_sources
        assert _normalize_sources(['foo', 'bar']) == {'doc', 'db', 'web', 'llm'}
        # 合法值保留，非法值剔除；非空结果不回退全开
        assert _normalize_sources(['doc', 'foo']) == {'doc'}

    @patch('apps.system.config_loader.get_config_value')
    def test_normalize_when_config_only_doc_db_then_filters_disabled(self, mock_cfg):
        """系统配置只开启 doc/db 时，请求中的 web/llm 被剔除（后端兜底防御）"""
        mock_cfg.return_value = 'doc,db'
        from apps.agent.executor import _normalize_sources
        assert _normalize_sources(['doc', 'db', 'web', 'llm']) == {'doc', 'db'}

    @patch('apps.system.config_loader.get_config_value')
    def test_normalize_when_config_empty_then_fallback_all(self, mock_cfg):
        """配置为空（未初始化/被清空）时视为全开，请求的来源全部保留"""
        mock_cfg.return_value = ''
        from apps.agent.executor import _normalize_sources
        assert _normalize_sources(['doc']) == {'doc'}
        assert _normalize_sources(['doc', 'db', 'web', 'llm']) == {'doc', 'db', 'web', 'llm'}

    @patch('apps.system.config_loader.get_config_value')
    def test_normalize_when_config_read_exception_then_fallback_all(self, mock_cfg):
        """配置读取异常（如 Redis 不可用）时视为全开，不阻断主流程"""
        mock_cfg.side_effect = Exception('redis down')
        from apps.agent.executor import _normalize_sources
        assert _normalize_sources(['doc', 'db']) == {'doc', 'db'}

    @patch('apps.system.config_loader.get_config_value')
    def test_normalize_when_config_has_invalid_keys_then_ignored(self, mock_cfg):
        """配置中含非法来源 key 时忽略，不影响合法来源"""
        mock_cfg.return_value = 'doc,foo,llm'
        from apps.agent.executor import _normalize_sources
        assert _normalize_sources(['doc', 'db', 'llm']) == {'doc', 'llm'}

    @patch('apps.system.config_loader.get_config_value')
    def test_enabled_source_set_when_config_unavailable_then_all(self, mock_cfg):
        """_enabled_source_set 配置缺失时返回全开集合"""
        mock_cfg.return_value = ''
        from apps.agent.executor import _enabled_source_set
        assert _enabled_source_set() == {'doc', 'db', 'web', 'llm'}
