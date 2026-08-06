"""
apps.graph.vector_search 测试 —— 实体向量检索

覆盖范围：
- search_entities_by_name：精确/模糊匹配、空名过滤
- search_entities：按向量检索语义相似实体（含类型过滤、空向量跳过）
"""

import pytest

from apps.graph.models import GraphEntity


def _vec(fill: float):
    return [fill] * 1024


def _alt_vec():
    """交替正负号向量，与全正值向量的余弦相似度≈0"""
    return [0.5 if i % 2 == 0 else -0.5 for i in range(1024)]


@pytest.mark.django_db
class TestSearchEntitiesByName:
    """search_entities_by_name 名称检索测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入实体"""
        self.e1 = GraphEntity.objects.create(
            name='张三', type='PERSON', description='员工')

    def test_exact_match_case_insensitive(self):
        """精确匹配（大小写不敏感）应命中"""
        from apps.graph.vector_search import search_entities_by_name
        assert [e.id for e in search_entities_by_name('张三', exact=True)] == [self.e1.id]

    def test_exact_match_no_hit(self):
        """精确匹配无结果时返回空列表"""
        from apps.graph.vector_search import search_entities_by_name
        assert search_entities_by_name('不存在的名字', exact=True) == []

    def test_fuzzy_match_icontains(self):
        """模糊匹配按名称包含检索"""
        from apps.graph.vector_search import search_entities_by_name
        hits = search_entities_by_name('张')
        assert [e.id for e in hits] == [self.e1.id]

    def test_empty_name_returns_empty(self):
        """空名/纯空白名应返回空列表"""
        from apps.graph.vector_search import search_entities_by_name
        assert search_entities_by_name('') == []
        assert search_entities_by_name('   ') == []


@pytest.mark.django_db
class TestSearchEntities:
    """search_entities 向量检索（依赖 pgvector）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入带向量实体"""
        self.e1 = GraphEntity.objects.create(
            name='张三', type='PERSON', description='研发员工', embedding=_vec(0.1))
        self.e2 = GraphEntity.objects.create(
            name='HR', type='ORG', description='人事部门', embedding=_alt_vec())

    def test_search_returns_scored_sorted_results(self):
        """检索结果按相似度降序返回，score = 1 - cosine_distance"""
        from apps.graph.vector_search import search_entities
        results = search_entities(_vec(0.1), top_k=10)
        assert len(results) == 2
        # 与 query 相同的 e1 相似度最高（distance=0 -> score=1.0），排在最前
        assert results[0]['name'] == '张三'
        assert results[0]['score'] == 1.0
        # 分数应单调递减
        scores = [r['score'] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_search_filters_by_type(self):
        """entity_types 过滤应只返回指定类型的实体"""
        from apps.graph.vector_search import search_entities
        results = search_entities(_vec(0.1), top_k=10, entity_types=['ORG'])
        assert len(results) == 1
        assert results[0]['name'] == 'HR'

    def test_search_skips_null_embedding(self):
        """embedding 为空的实体不应出现在结果中"""
        from apps.graph.vector_search import search_entities
        GraphEntity.objects.create(name='无向量', type='TERM', description='x')
        results = search_entities(_vec(0.1), top_k=10)
        assert all(r['name'] != '无向量' for r in results)
