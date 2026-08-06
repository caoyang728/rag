"""
apps.graph.retriever 测试 —— GraphRAG 局部/全局检索与统一入口

覆盖范围：
- local_search：正常检索（实体+关系扩展）、全零向量降级、低相似度降级
- global_search：正常检索（社区摘要）、全零向量降级、实体低分降级
- graphrag_search：auto 择优 / local / global 三种模式

测试分层：
- local_search / global_search：DB 集成测试，mock embedding client，真实 pgvector 向量检索
- graphrag_search：纯 mock，验证模式分发与择优逻辑
"""
import pytest
from unittest.mock import patch, MagicMock


from apps.graph.models import GraphEntity, GraphRelation, GraphCommunity


def _vec(fill: float, size: int = 1024):
    """构造全值相同的向量（与同值向量余弦距离为 0）"""
    return [fill] * size


def _alt_vec(size: int = 1024):
    """交替正负号向量：与全正值向量的余弦相似度≈0，用于构造低分场景"""
    return [0.5 if i % 2 == 0 else -0.5 for i in range(size)]


def _mock_client(qvec):
    client = MagicMock()
    client.embed_one.return_value = qvec
    return client


@pytest.mark.django_db
class TestLocalSearch:
    """local_search 局部图谱检索（DB 集成）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入实体与关系"""
        self.e1 = GraphEntity.objects.create(
            name='张三', type='PERSON', description='研发部员工', embedding=_vec(0.1))
        self.e2 = GraphEntity.objects.create(
            name='李四', type='PERSON', description='产品经理', embedding=_vec(0.1))
        GraphRelation.objects.create(
            source_entity=self.e1, target_entity=self.e2,
            relation_type='同事', weight=1.0)

    @patch('apps.graph.retriever.get_embedding_client')
    def test_local_search_success(self, mock_get):
        """正常检索应返回实体+关系上下文与置信度"""
        mock_get.return_value = _mock_client(_vec(0.1))
        from apps.graph.retriever import local_search
        result = local_search('张三负责什么', MagicMock())

        assert result['source'] == 'graphrag_local'
        assert result['confidence'] > 0
        names = [e['name'] for e in result['entities']]
        assert '张三' in names
        assert '李四' in names  # 关系扩展命中邻居
        assert len(result['relations']) == 1
        assert '同事' in result['relations'][0]['type']

    @patch('apps.graph.retriever.get_embedding_client')
    def test_local_search_zero_vector_degrades(self, mock_get):
        """全零向量（embedding 服务异常）应返回空结果"""
        mock_get.return_value = _mock_client([0.0] * 1024)
        from apps.graph.retriever import local_search
        result = local_search('测试', MagicMock())
        assert result['context'] == ''
        assert result['confidence'] == 0.0

    @patch('apps.graph.retriever.get_embedding_client')
    def test_local_search_low_score_degrades(self, mock_get):
        """实体相似度低于 ENTITY_MATCH_THRESHOLD 应判定无关返回空结果"""
        mock_get.return_value = _mock_client(_alt_vec())
        from apps.graph.retriever import local_search
        result = local_search('完全无关的话题', MagicMock())
        assert result['context'] == ''
        assert result['confidence'] == 0.0


@pytest.mark.django_db
class TestGlobalSearch:
    """global_search 全局图谱检索（DB 集成）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入实体与社区"""
        self.e1 = GraphEntity.objects.create(
            name='张三', type='PERSON', description='研发部员工', embedding=_vec(0.1))
        self.community = GraphCommunity.objects.create(
            community_id=0, level=0, entity_ids=[self.e1.id],
            summary='研发团队社区摘要', keywords=['研发', '团队'],
            metadata={'topic': '研发团队'})

    @patch('apps.graph.retriever.get_embedding_client')
    def test_global_search_success(self, mock_get):
        """正常检索应返回社区摘要与关键词"""
        mock_get.return_value = _mock_client(_vec(0.1))
        from apps.graph.retriever import global_search
        result = global_search('研发团队', MagicMock())

        assert result['source'] == 'graphrag_global'
        assert result['confidence'] > 0
        assert len(result['communities']) == 1
        assert result['communities'][0]['summary'] == '研发团队社区摘要'
        assert result['communities'][0]['topic'] == '研发团队'

    @patch('apps.graph.retriever.get_embedding_client')
    def test_global_search_zero_vector_degrades(self, mock_get):
        """全零向量应返回空结果"""
        mock_get.return_value = _mock_client([0.0] * 1024)
        from apps.graph.retriever import global_search
        result = global_search('测试', MagicMock())
        assert result['context'] == ''
        assert result['confidence'] == 0.0

    @patch('apps.graph.retriever.get_embedding_client')
    def test_global_search_low_score_degrades(self, mock_get):
        """实体相似度低于 GLOBAL_ENTITY_GATE(0.4) 应判定无关返回空结果"""
        mock_get.return_value = _mock_client(_alt_vec())
        from apps.graph.retriever import global_search
        result = global_search('无关话题', MagicMock())
        assert result['context'] == ''
        assert result['confidence'] == 0.0


class TestGraphragSearch:
    """graphrag_search 统一入口模式分发与择优测试（纯 mock）"""

    @pytest.mark.unit
    @patch('apps.graph.retriever.local_search')
    def test_mode_local(self, mock_local):
        """mode='local' 时只调用 local_search"""
        mock_local.return_value = {'source': 'graphrag_local', 'confidence': 0.5}
        from apps.graph.retriever import graphrag_search
        result = graphrag_search('q', MagicMock(), mode='local')
        assert result['source'] == 'graphrag_local'
        mock_local.assert_called_once()

    @pytest.mark.unit
    @patch('apps.graph.retriever.global_search')
    def test_mode_global(self, mock_global):
        """mode='global' 时只调用 global_search"""
        mock_global.return_value = {'source': 'graphrag_global', 'confidence': 0.6}
        from apps.graph.retriever import graphrag_search
        result = graphrag_search('q', MagicMock(), mode='global')
        assert result['source'] == 'graphrag_global'
        mock_global.assert_called_once()

    @pytest.mark.unit
    @patch('apps.graph.retriever.global_search')
    @patch('apps.graph.retriever.local_search')
    def test_mode_auto_keeps_local_when_high(self, mock_local, mock_global):
        """auto：local 置信度足够（>=0.3）时不调用 global"""
        mock_local.return_value = {'source': 'graphrag_local', 'confidence': 0.6}
        from apps.graph.retriever import graphrag_search
        result = graphrag_search('q', MagicMock())
        assert result['source'] == 'graphrag_local'
        mock_global.assert_not_called()

    @pytest.mark.unit
    @patch('apps.graph.retriever.global_search')
    @patch('apps.graph.retriever.local_search')
    def test_mode_auto_switches_when_global_better(self, mock_local, mock_global):
        """auto：local 置信度低且 global 更高时应切换为 global"""
        mock_local.return_value = {'source': 'graphrag_local', 'confidence': 0.2}
        mock_global.return_value = {'source': 'graphrag_global', 'confidence': 0.5}
        from apps.graph.retriever import graphrag_search
        result = graphrag_search('q', MagicMock())
        assert result['source'] == 'graphrag_global'

    @pytest.mark.unit
    @patch('apps.graph.retriever.global_search')
    @patch('apps.graph.retriever.local_search')
    def test_mode_auto_keeps_local_when_global_worse(self, mock_local, mock_global):
        """auto：global 置信度不高于 local 时保留 local"""
        mock_local.return_value = {'source': 'graphrag_local', 'confidence': 0.2}
        mock_global.return_value = {'source': 'graphrag_global', 'confidence': 0.1}
        from apps.graph.retriever import graphrag_search
        result = graphrag_search('q', MagicMock())
        assert result['source'] == 'graphrag_local'
