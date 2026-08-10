"""
retrieval.profile 个性化检索 单元/集成测试
覆盖：
- 开关与加权系数（默认关闭 / 默认 0.1 / 钳制上限 0.2）
- 用户画像构建（冷启动 / UserMemory+QaRecord 聚合 / 偏好文档类型）
- apply_personalization（关闭原样返回 / 冷启动无副作用 / 开启后重排 + 审计）
- route_trace 审计条目（build_route_trace 并入 personalization 层）
"""
from unittest.mock import patch

import pytest

from apps.memory.models import Session, UserMemory
from apps.chat.models import QaRecord


def _make_session(user):
    """创建会话（QaRecord 外键必需）"""
    return Session.objects.create(user=user, root_type='company_doc', title='test_session')


def _make_qa(user, session, question, root_type='company_doc'):
    """创建一条问答记录（默认字段已覆盖，仅需必填项）"""
    return QaRecord.objects.create(
        session=session, user=user, question=question,
        answer='测试回答', answer_type='rag', root_type=root_type,
    )


def _chunk(chunk_id, rerank_score, content, root_type='company_doc'):
    """构造一个检索结果 chunk（含 rerank 分数与画像匹配所需字段）"""
    return {
        'chunk_id': chunk_id,
        'document_id': chunk_id,
        'content': content,
        'root_type': root_type,
        'rerank_score': rerank_score,
    }


class TestConfig:
    """开关与加权系数"""

    @patch('apps.retrieval.profile.get_config_value', return_value=False)
    def test_enabled_default_false(self, m):
        from apps.retrieval.profile import personalization_enabled
        assert personalization_enabled() is False

    @patch('apps.retrieval.profile.get_config_value', return_value=True)
    def test_enabled_true(self, m):
        from apps.retrieval.profile import personalization_enabled
        assert personalization_enabled() is True

    @patch('apps.retrieval.profile.get_config_value', return_value=0.1)
    def test_weight_default(self, m):
        from apps.retrieval.profile import personalization_weight
        assert personalization_weight() == 0.1

    @patch('apps.retrieval.profile.get_config_value', return_value=0.5)
    def test_weight_clamped_to_max(self, m):
        """加权系数钳制上限 0.2，防止画像污染全局结果"""
        from apps.retrieval.profile import personalization_weight
        assert personalization_weight() == 0.2

    @patch('apps.retrieval.profile.get_config_value', return_value=-0.3)
    def test_weight_clamped_to_zero(self, m):
        from apps.retrieval.profile import personalization_weight
        assert personalization_weight() == 0.0


@pytest.mark.django_db
class TestBuildUserProfile:
    """用户画像特征提取"""

    def test_cold_start_returns_none(self, test_user):
        """无 UserMemory 且无历史问答 → 冷启动返回 None（不参与加权）"""
        from apps.retrieval.profile import build_user_profile
        assert build_user_profile(test_user) is None

    def test_from_memory_and_qa(self, test_user):
        """UserMemory 领域词 + QaRecord 聚合（偏好文档类型 ≥3 次、jieba 高频词）"""
        from apps.retrieval.profile import build_user_profile

        # 用户创建信号已自动初始化 UserMemory，这里只需更新画像字段
        UserMemory.objects.update_or_create(
            user=test_user,
            defaults={
                'domain_tags': ['hr', '招聘'],
                'frequent_topics': ['入职流程'],
            },
        )
        session = _make_session(test_user)
        # 偏好类型：hr_doc 出现 3 次 → 记为偏好；company_doc 仅 2 次 → 不记
        for _ in range(3):
            _make_qa(test_user, session, '新员工入职需要哪些材料', root_type='hr_doc')
        for _ in range(2):
            _make_qa(test_user, session, '公司报销制度', root_type='company_doc')

        profile = build_user_profile(test_user)
        assert profile is not None
        assert profile.has_profile is True
        assert 'hr' in profile.terms
        assert '入职流程' in profile.terms
        assert profile.preferred_root_types == ['hr_doc']

    def test_memory_only_still_has_profile(self, test_user):
        """仅有 UserMemory（无历史问答）也算有画像，不视为冷启动"""
        from apps.retrieval.profile import build_user_profile
        # 用户创建信号已自动初始化 UserMemory，这里只需更新画像字段
        UserMemory.objects.update_or_create(
            user=test_user, defaults={'domain_tags': ['hr']})
        profile = build_user_profile(test_user)
        assert profile is not None
        assert profile.preferred_root_types == []

    @pytest.mark.parametrize('user', [None, 'anon'])
    def test_anonymous_or_none_returns_none(self, user):
        """匿名/None 用户不参与个性化"""
        from apps.retrieval.profile import get_user_profile
        assert get_user_profile(user) is None


@pytest.mark.django_db
class TestApplyPersonalization:
    """排序加权主逻辑"""

    @patch('apps.retrieval.profile.personalization_enabled', return_value=False)
    def test_when_disabled_then_unchanged(self, m, test_user):
        """总开关关闭 → 结果原样返回，无 personalization 审计键（与现状完全一致）"""
        from apps.retrieval.profile import apply_personalization
        result = {'chunks': [_chunk(1, 0.9, '内容a'), _chunk(2, 0.8, '内容b')]}
        out = apply_personalization(result, test_user, '查询')
        assert out is result
        assert 'personalization' not in result
        assert [c['chunk_id'] for c in result['chunks']] == [1, 2]

    @patch('apps.retrieval.profile.personalization_enabled', return_value=True)
    @patch('apps.retrieval.profile.get_user_profile', return_value=None)
    def test_when_cold_start_then_no_side_effect(self, m_profile, m_enabled, test_user):
        """冷启动用户：顺序不变，仅附加 applied=False 审计（无副作用）"""
        from apps.retrieval.profile import apply_personalization
        result = {'chunks': [_chunk(1, 0.9, '内容a'), _chunk(2, 0.8, '内容b')]}
        out = apply_personalization(result, test_user, '查询')
        assert [c['chunk_id'] for c in out['chunks']] == [1, 2]
        pz = out['personalization']
        assert pz['enabled'] is True
        assert pz['applied'] is False
        assert pz['cold_start'] is True
        # 审计挂到 transform 下，供 build_route_trace 统一收集
        assert out['transform']['enabled'] is False
        assert out['transform']['personalization'] is pz

    @patch('apps.retrieval.profile.personalization_enabled', return_value=True)
    def test_when_no_chunks_then_audit_no_chunks(self, m, test_user):
        """无召回结果：不抛异常，记录 applied=False"""
        from apps.retrieval.profile import apply_personalization
        from apps.retrieval.profile import UserProfile
        with patch('apps.retrieval.profile.get_user_profile',
                   return_value=UserProfile(terms=['hr'])):
            result = {'chunks': []}
            out = apply_personalization(result, test_user, '查询')
        assert out['chunks'] == []
        assert out['personalization']['applied'] is False
        assert out['personalization']['reason'] == 'no_chunks'

    @patch('apps.retrieval.profile.personalization_enabled', return_value=True)
    @patch('apps.retrieval.profile.personalization_weight', return_value=0.2)
    def test_when_enabled_then_reorders_and_audits(self, m_w, m_e, test_user):
        """有画像且基础分数相同时，按画像相关度重排并输出完整审计"""
        from apps.retrieval.profile import apply_personalization
        from apps.retrieval.profile import UserProfile

        profile = UserProfile(
            terms=['入职', '报销'],
            preferred_root_types=['company_doc'],
        )
        chunks = [
            _chunk(1, 0.90, '新员工入职培训材料', root_type='company_doc'),
            # 非偏好类型且无领域词命中 → sim=0
            _chunk(2, 0.90, '销售策略分析', root_type='sales_doc'),
            _chunk(3, 0.90, '公司报销制度说明', root_type='company_doc'),
        ]
        with patch('apps.retrieval.profile.get_user_profile', return_value=profile):
            result = apply_personalization({'chunks': chunks}, test_user, '查询')

        # 基础分数全部相同（span=0 → norm=0.5），由画像相关度决出顺序：
        # chunk1 sim=0.65、chunk3 sim=0.65 优先，chunk2 sim=0 殿后
        assert [c['chunk_id'] for c in result['chunks']] == [1, 3, 2]

        pz = result['personalization']
        assert pz['enabled'] is True
        assert pz['applied'] is True
        assert pz['weight'] == 0.2
        # 等分时 chunk2/chunk3 互换位置，位置变化数为 2
        assert pz['adjusted_count'] == 2
        assert pz['reordered'] is True
        assert pz['top_personalized'] is True
        assert pz['personalized_hits'] == 2
        assert pz['profile']['terms'] == ['入职', '报销']
        assert pz['profile']['preferred_root_types'] == ['company_doc']
        # 审计并入 transform 供 build_route_trace 收集
        assert result['transform']['personalization'] is pz

    @patch('apps.retrieval.profile.personalization_enabled', return_value=True)
    @patch('apps.retrieval.profile.personalization_weight', return_value=0.0)
    def test_when_weight_zero_then_keep_base_order(self, m_w, m_e, test_user):
        """系数为 0 → 完全按基础分数排序，个性化不产生任何影响"""
        from apps.retrieval.profile import apply_personalization
        from apps.retrieval.profile import UserProfile
        profile = UserProfile(terms=['入职'], preferred_root_types=['company_doc'])
        chunks = [
            _chunk(1, 0.9, '无关内容'),
            _chunk(2, 0.8, '新员工入职材料'),
            _chunk(3, 0.7, '其他内容'),
        ]
        with patch('apps.retrieval.profile.get_user_profile', return_value=profile):
            result = apply_personalization({'chunks': chunks}, test_user, '查询')
        assert [c['chunk_id'] for c in result['chunks']] == [1, 2, 3]
        assert result['personalization']['adjusted_count'] == 0
        assert result['personalization']['reordered'] is False


class TestRouteTrace:
    """route_trace 审计条目"""

    def test_build_personalization_route_trace_when_disabled_empty(self):
        from apps.retrieval.profile import build_personalization_route_trace
        assert build_personalization_route_trace(None) == []
        assert build_personalization_route_trace({'enabled': False}) == []

    def test_build_personalization_route_trace(self):
        from apps.retrieval.profile import build_personalization_route_trace
        pz = {
            'enabled': True, 'applied': True, 'cold_start': False, 'weight': 0.1,
            'adjusted_count': 1, 'reordered': True, 'top_personalized': True,
            'personalized_hits': 2, 'latency_ms': 3,
            'profile': {'terms': ['hr'], 'preferred_root_types': ['company_doc']},
        }
        trace = build_personalization_route_trace(pz)
        assert len(trace) == 1
        entry = trace[0]
        assert entry['layer'] == 'personalization'
        assert entry['applied'] is True
        assert entry['reordered'] is True
        assert entry['personalized_hits'] == 2
        assert entry['profile_domains'] == ['hr']
        assert entry['preferred_root_types'] == ['company_doc']

    def test_build_route_trace_without_transform_includes_personalization(self):
        """改写/分解关闭、仅个性化生效时，trace 只含 personalization 层"""
        from apps.retrieval.query_transform import build_route_trace
        pz = {'enabled': True, 'applied': True, 'cold_start': False,
              'weight': 0.1, 'adjusted_count': 1, 'reordered': True,
              'top_personalized': True, 'personalized_hits': 1, 'latency_ms': 2,
              'profile': {'terms': ['hr'], 'preferred_root_types': []}}
        trace = build_route_trace({'enabled': False, 'personalization': pz})
        assert [t['layer'] for t in trace] == ['personalization']

    def test_build_route_trace_with_transform_and_personalization(self):
        """改写链路开启 + 个性化生效时，trace 同时包含两层"""
        from apps.retrieval.query_transform import build_route_trace
        transform = {
            'enabled': True,
            'rewrite': {'original': 'q', 'rewritten_query': 'q2', 'expansions': [],
                        'changed': True, 'ok': True, 'error': '', 'latency_ms': 5},
            'personalization': {'enabled': True, 'applied': True, 'cold_start': False,
                                'weight': 0.1, 'adjusted_count': 0, 'reordered': False,
                                'top_personalized': False, 'personalized_hits': 0,
                                'latency_ms': 1,
                                'profile': {'terms': ['hr'], 'preferred_root_types': []}},
        }
        trace = build_route_trace(transform)
        assert [t['layer'] for t in trace] == ['query_rewrite', 'personalization']

    def test_build_route_trace_empty(self):
        from apps.retrieval.query_transform import build_route_trace
        assert build_route_trace(None) == []
        assert build_route_trace({'enabled': False}) == []


class TestUserProfileDTO:
    """UserProfile 序列化/反序列化（无 DB 依赖）"""

    def test_to_dict_returns_terms_and_types(self):
        from apps.retrieval.profile import UserProfile
        p = UserProfile(terms=['hr'], preferred_root_types=['company_doc'])
        assert p.to_dict() == {'terms': ['hr'], 'preferred_root_types': ['company_doc']}

    def test_from_dict_when_none_fields_then_empty_lists(self):
        from apps.retrieval.profile import UserProfile
        p = UserProfile.from_dict({'terms': None, 'preferred_root_types': None})
        assert p.terms == []
        assert p.preferred_root_types == []
        assert p.has_profile is False

    def test_from_dict_roundtrip(self):
        from apps.retrieval.profile import UserProfile
        p = UserProfile.from_dict({'terms': ['a', 'b'], 'preferred_root_types': ['c']})
        assert p.has_profile is True
        assert p.to_dict() == {'terms': ['a', 'b'], 'preferred_root_types': ['c']}


class TestConfigExceptions:
    """配置读取异常降级（不阻断主检索链路）"""

    @patch('apps.retrieval.profile.get_config_value', side_effect=Exception('cfg down'))
    def test_enabled_when_config_raises_then_false(self, m):
        from apps.retrieval.profile import personalization_enabled
        assert personalization_enabled() is False

    @patch('apps.retrieval.profile.get_config_value', side_effect=TypeError('bad type'))
    def test_weight_when_config_raises_then_default(self, m):
        from apps.retrieval.profile import personalization_weight
        assert personalization_weight() == 0.1


@pytest.mark.django_db
class TestGetUserProfile:
    """get_user_profile：缓存读/写与异常降级"""

    def test_when_cache_hit_then_returns_profile(self, test_user):
        """缓存命中直接反序列化返回，不触发画像构建"""
        with patch('apps.retrieval.profile.cache') as mock_cache:
            mock_cache.get.return_value = {'terms': ['hr'], 'preferred_root_types': []}
            from apps.retrieval.profile import get_user_profile
            p = get_user_profile(test_user)
        assert p is not None
        assert p.terms == ['hr']
        mock_cache.get.assert_called_once()
        mock_cache.set.assert_not_called()

    def test_when_no_cache_then_builds_and_sets(self, test_user):
        """无缓存 → 构建画像并写缓存"""
        from apps.memory.models import UserMemory
        UserMemory.objects.update_or_create(user=test_user, defaults={'domain_tags': ['hr']})
        with patch('apps.retrieval.profile.cache') as mock_cache:
            mock_cache.get.return_value = None
            from apps.retrieval.profile import get_user_profile
            p = get_user_profile(test_user)
        assert p is not None
        assert 'hr' in p.terms
        mock_cache.set.assert_called_once()

    def test_when_build_returns_none_then_no_cache(self, test_user):
        """冷启动（构建结果为 None）不缓存空结果，让新问答能尽快生效画像"""
        with patch('apps.retrieval.profile.cache') as mock_cache, \
                patch('apps.retrieval.profile.build_user_profile', return_value=None):
            mock_cache.get.return_value = None
            from apps.retrieval.profile import get_user_profile
            assert get_user_profile(test_user) is None
        mock_cache.set.assert_not_called()

    def test_when_build_raises_then_returns_none(self, test_user):
        """画像构建异常降级为无画像，绝不阻断主检索"""
        with patch('apps.retrieval.profile.cache') as mock_cache, \
                patch('apps.retrieval.profile.build_user_profile',
                      side_effect=RuntimeError('db down')):
            mock_cache.get.return_value = None
            from apps.retrieval.profile import get_user_profile
            assert get_user_profile(test_user) is None

    def test_when_cache_get_raises_then_still_builds(self, test_user):
        """读缓存异常（Redis 抖动）→ 降级走画像构建"""
        from apps.memory.models import UserMemory
        UserMemory.objects.update_or_create(user=test_user, defaults={'domain_tags': ['hr']})
        with patch('apps.retrieval.profile.cache') as mock_cache:
            mock_cache.get.side_effect = RuntimeError('redis down')
            mock_cache.set.return_value = None
            from apps.retrieval.profile import get_user_profile
            p = get_user_profile(test_user)
        assert p is not None

    def test_when_cache_set_raises_then_still_returns(self, test_user):
        """写缓存异常（Redis 抖动）→ 仍返回画像，不阻断主流程"""
        from apps.memory.models import UserMemory
        UserMemory.objects.update_or_create(user=test_user, defaults={'domain_tags': ['hr']})
        with patch('apps.retrieval.profile.cache') as mock_cache:
            mock_cache.get.return_value = None
            mock_cache.set.side_effect = RuntimeError('redis down')
            from apps.retrieval.profile import get_user_profile
            p = get_user_profile(test_user)
        assert p is not None
        assert 'hr' in p.terms

    def test_when_force_then_skips_cache_read(self, test_user):
        """force=True 跳过缓存读，直接重新构建画像"""
        from apps.memory.models import UserMemory
        UserMemory.objects.update_or_create(user=test_user, defaults={'domain_tags': ['hr']})
        with patch('apps.retrieval.profile.cache') as mock_cache:
            mock_cache.get.return_value = {'terms': ['stale'], 'preferred_root_types': []}
            from apps.retrieval.profile import get_user_profile
            p = get_user_profile(test_user, force=True)
        assert 'hr' in p.terms
        assert 'stale' not in p.terms
        mock_cache.get.assert_not_called()


@pytest.mark.django_db
class TestBuildUserProfileExtra:
    """build_user_profile 其余分支：无 UserMemory / jieba 成功与失败"""

    def test_when_no_usermemory_then_terms_from_qa_only(self, test_user):
        """无 UserMemory 时仅由 QaRecord 聚合（jieba 高频词 + 偏好类型）"""
        from apps.memory.models import UserMemory
        UserMemory.objects.filter(user=test_user).delete()
        session = _make_session(test_user)
        # 偏好文档类型需 ≥3 次才算稳定偏好
        for _ in range(3):
            _make_qa(test_user, session, '公司报销制度查询', root_type='company_doc')
        with patch('jieba.analyse.extract_tags', return_value=['报销']):
            from apps.retrieval.profile import build_user_profile
            p = build_user_profile(test_user)
        assert p is not None
        assert '报销' in p.terms
        assert p.preferred_root_types == ['company_doc']

    def test_when_jieba_raises_then_terms_without_tags(self, test_user):
        """jieba 提取失败降级：仅保留 UserMemory 领域词，不抛异常"""
        from apps.memory.models import UserMemory
        UserMemory.objects.update_or_create(user=test_user, defaults={'domain_tags': ['hr']})
        session = _make_session(test_user)
        _make_qa(test_user, session, '一些问题', root_type='company_doc')
        with patch('jieba.analyse.extract_tags', side_effect=RuntimeError('dict load fail')):
            from apps.retrieval.profile import build_user_profile
            p = build_user_profile(test_user)
        assert p is not None
        assert 'hr' in p.terms


class TestProfileHelpers:
    """纯逻辑辅助函数（无 DB 依赖）"""

    def test_base_score_prefers_rerank(self):
        from apps.retrieval.profile import _base_score
        assert _base_score({'rerank_score': '0.9', 'rrf_score': 1, 'score': 2}) == 0.9

    def test_base_score_falls_back_rrf(self):
        from apps.retrieval.profile import _base_score
        assert _base_score({'rrf_score': 3.5}) == 3.5

    def test_base_score_falls_back_score(self):
        from apps.retrieval.profile import _base_score
        assert _base_score({'score': 0.7}) == 0.7

    def test_base_score_when_invalid_then_skips(self):
        """rerank_score 非法（非数字）→ 跳过，回退到下一个可用分数"""
        from apps.retrieval.profile import _base_score
        assert _base_score({'rerank_score': 'abc', 'score': 0.2}) == 0.2

    def test_base_score_when_all_missing_then_zero(self):
        from apps.retrieval.profile import _base_score
        assert _base_score({'chunk_id': 1}) == 0.0

    def test_chunk_similarity_when_root_type_hit(self):
        """命中偏好文档类型 → 相关度 0.5（无领域词时）"""
        from apps.retrieval.profile import _chunk_personal_similarity, UserProfile
        p = UserProfile(terms=[], preferred_root_types=['company_doc'])
        assert _chunk_personal_similarity({'root_type': 'company_doc'}, p) == 0.5

    def test_chunk_similarity_when_terms_hit_then_capped(self):
        """领域词命中累加（单词 0.15）+ 类型命中，合计封顶 1.0"""
        from apps.retrieval.profile import _chunk_personal_similarity, UserProfile
        p = UserProfile(terms=['入职', '报销', '流程', '制度'], preferred_root_types=['company_doc'])
        sim = _chunk_personal_similarity({'content': '入职报销流程制度', 'root_type': 'company_doc'}, p)
        assert sim == 1.0

    def test_chunk_similarity_when_no_content_then_zero(self):
        """chunk 无内容（无领域词可匹配）→ 相关度 0，由类型偏好单独贡献"""
        from apps.retrieval.profile import _chunk_personal_similarity, UserProfile
        p = UserProfile(terms=['hr'], preferred_root_types=[])
        assert _chunk_personal_similarity({'content': ''}, p) == 0.0

    def test_dedupe_removes_empty_and_dups(self):
        """保序去重并去掉空白串"""
        from apps.retrieval.profile import _dedupe
        assert _dedupe(['a', '', ' b ', 'a', 'c'], limit=20) == ['a', 'b', 'c']

    def test_dedupe_truncates_at_limit(self):
        from apps.retrieval.profile import _dedupe
        assert _dedupe(['a', 'b', 'c'], limit=2) == ['a', 'b']


@pytest.mark.django_db
class TestApplyPersonalizationNormalized:
    """基础分数不同（span>0）时的归一化融合与审计"""

    @patch('apps.retrieval.profile.personalization_enabled', return_value=True)
    @patch('apps.retrieval.profile.personalization_weight', return_value=0.1)
    def test_when_base_scores_differ_then_normalized_mix(self, m_w, m_e, test_user):
        """高分基础 + 画像命中者稳居第一，完整审计输出 latency"""
        from apps.retrieval.profile import apply_personalization
        from apps.retrieval.profile import UserProfile
        profile = UserProfile(terms=['入职'], preferred_root_types=[])
        chunks = [
            _chunk(1, 1.0, '新员工入职培训', root_type='company_doc'),
            _chunk(2, 0.5, '无关内容', root_type='company_doc'),
            _chunk(3, 0.0, '新员工入职材料', root_type='company_doc'),
        ]
        with patch('apps.retrieval.profile.get_user_profile', return_value=profile):
            result = apply_personalization({'chunks': chunks}, test_user, '查询')
        assert result['chunks'][0]['chunk_id'] == 1
        pz = result['personalization']
        assert pz['applied'] is True
        assert pz['latency_ms'] >= 0
        assert pz['profile']['terms'] == ['入职']
