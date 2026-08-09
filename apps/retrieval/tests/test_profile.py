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
