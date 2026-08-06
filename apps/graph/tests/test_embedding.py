"""
apps.graph.embedding 测试 —— 实体 Embedding 文本构建与批量同步

覆盖范围：
- build_entity_embedding_text：名称+类型标签+描述拼接、未知类型降级
- sync_entity_embeddings：只处理缺失向量的实体、跳过全零向量、embedding 失败不阻断
"""
from unittest.mock import patch, MagicMock

import pytest

from apps.graph.models import GraphEntity


def _vec(fill: float):
    return [fill] * 1024


@pytest.mark.django_db
class TestBuildEntityEmbeddingText:
    """build_entity_embedding_text 文本构建测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入实体"""
        self.entity = GraphEntity.objects.create(
            name='张三', type='PERSON', description='研发部员工')

    def test_build_text_with_known_type(self):
        """已知类型应使用中文标签拼接"""
        from apps.graph.embedding import build_entity_embedding_text
        assert build_entity_embedding_text(self.entity) == '张三 是人物。研发部员工'

    def test_build_text_with_unknown_type(self):
        """未知类型应降级为原始 type 值"""
        from apps.graph.embedding import build_entity_embedding_text
        e = GraphEntity.objects.create(name='X', type='CUSTOM', description='desc')
        assert build_entity_embedding_text(e) == 'X 是CUSTOM。desc'


@pytest.mark.django_db
class TestSyncEntityEmbeddings:
    """sync_entity_embeddings 批量同步测试"""

    @patch('apps.graph.embedding.get_embedding_client')
    def test_syncs_only_missing_embeddings(self, mock_get):
        """只处理 embedding 为空的实体，已有向量的实体跳过"""
        from apps.graph.embedding import sync_entity_embeddings
        e1 = GraphEntity.objects.create(name='张三', type='PERSON', description='a')
        e2 = GraphEntity.objects.create(
            name='李四', type='PERSON', description='b', embedding=_vec(0.1))

        client = MagicMock()
        client.embed.return_value = [_vec(0.2)]
        mock_get.return_value = client

        count = sync_entity_embeddings()
        assert count == 1  # 只有 e1 被处理
        e1.refresh_from_db()
        assert e1.embedding is not None

    @patch('apps.graph.embedding.get_embedding_client')
    def test_skips_zero_vectors(self, mock_get):
        """全零向量（embedding 服务空返回）不应写入"""
        from apps.graph.embedding import sync_entity_embeddings
        e1 = GraphEntity.objects.create(name='张三', type='PERSON', description='a')

        client = MagicMock()
        client.embed.return_value = [[0.0] * 1024]
        mock_get.return_value = client

        count = sync_entity_embeddings(entity_ids=[e1.id])
        assert count == 0
        e1.refresh_from_db()
        assert e1.embedding is None

    @patch('apps.graph.embedding.get_embedding_client')
    def test_embed_error_non_blocking(self, mock_get):
        """embedding 服务异常不应阻断整体流程"""
        from apps.graph.embedding import sync_entity_embeddings
        GraphEntity.objects.create(name='张三', type='PERSON', description='a')
        GraphEntity.objects.create(name='李四', type='PERSON', description='b')

        client = MagicMock()
        client.embed.side_effect = Exception('embed failed')
        mock_get.return_value = client

        count = sync_entity_embeddings()
        assert count == 0
