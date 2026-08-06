"""
apps.graph.sync 单元/集成测试 —— 图谱增量同步与数据一致性

覆盖范围：
- _clean_graph_data：关系清理 + 实体引用移除（保留多来源实体 / 删除无来源实体）
- on_document_done：文档完成触发抽取任务
- on_document_updated：文档更新触发重新抽取
- on_document_deleted：文档删除清理图谱数据 + 标记社区待刷新

测试分层：
- on_document_done / on_document_updated：mock graph_extract_task.delay，验证任务派发
- on_document_deleted：mock _clean_graph_data + DB 集成验证社区时间戳更新
- _clean_graph_data：DB 集成测试，验证关系/实体的清理与保留逻辑
"""
import pytest
from unittest.mock import patch, MagicMock

from django.utils import timezone
from datetime import timedelta

from apps.graph.models import GraphEntity, GraphRelation, GraphCommunity


# ============================================================================
# _clean_graph_data：DB 集成测试
# ============================================================================
@pytest.mark.django_db
class TestCleanGraphData:
    """_clean_graph_data DB 集成测试

    验证文档删除/重新抽取前的图谱清理逻辑：
    - 关系按 source_doc_ids 精确清理
    - 实体从 source_doc_ids 移除该文档：有其他来源则保留并置空 embedding，
      无其他来源则删除
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入双来源/单来源实体与关系"""
        # entity_a 来自文档 1 和 2（清理文档 1 后应保留，source_doc_ids=[2]）
        self.entity_a = GraphEntity.objects.create(
            name='实体A', type='PERSON', description='描述A',
            source_doc_ids=[1, 2])
        # entity_b 仅来自文档 1（清理文档 1 后应删除）
        self.entity_b = GraphEntity.objects.create(
            name='实体B', type='ORG', description='描述B',
            source_doc_ids=[1])
        # 关系来自文档 1（清理文档 1 后应删除）
        self.relation = GraphRelation.objects.create(
            source_entity=self.entity_a,
            target_entity=self.entity_b,
            relation_type='认识',
            weight=1.0,
            source_doc_ids=[1],
        )

    def test_deletes_relations_for_document(self):
        """文档 1 的关系应被清理"""
        from apps.graph.sync import _clean_graph_data

        stats = _clean_graph_data(1)

        # 关系被删除
        assert not GraphRelation.objects.filter(id=self.relation.id).exists()
        assert stats['relations'] >= 1

    def test_deletes_entities_without_other_sources(self):
        """仅来自文档 1 的实体应被删除"""
        from apps.graph.sync import _clean_graph_data

        _clean_graph_data(1)

        # entity_b（source_doc_ids=[1]）应被删除
        assert not GraphEntity.objects.filter(id=self.entity_b.id).exists()

    def test_keeps_entities_with_other_sources(self):
        """有其他来源的实体应保留，且从 source_doc_ids 移除该文档"""
        from apps.graph.sync import _clean_graph_data

        stats = _clean_graph_data(1)

        # entity_a（source_doc_ids=[1, 2]）应保留
        entity_a = GraphEntity.objects.get(id=self.entity_a.id)
        assert entity_a.source_doc_ids == [2]
        # embedding 置空，等待下次抽取重新同步
        assert entity_a.embedding is None

    def test_stats_structure(self):
        """返回的 stats 应包含完整的清理统计"""
        from apps.graph.sync import _clean_graph_data

        stats = _clean_graph_data(1)

        assert 'relations' in stats
        assert 'entities_deleted' in stats
        assert 'entities_kept' in stats
        assert 'entities_to_refresh' in stats
        # entity_b 删除（1 个），entity_a 保留（1 个）
        assert stats['entities_deleted'] == 1
        assert stats['entities_kept'] == 1
        assert stats['entities_to_refresh'] == 1

    def test_clean_nonexistent_document(self):
        """清理不存在的文档 ID 应正常返回（无副作用）"""
        from apps.graph.sync import _clean_graph_data

        stats = _clean_graph_data(999)

        assert stats['relations'] == 0
        assert stats['entities_deleted'] == 0
        assert stats['entities_kept'] == 0
        assert stats['entities_to_refresh'] == 0
        # 原有数据不受影响
        assert GraphEntity.objects.count() == 2
        assert GraphRelation.objects.count() == 1

    def test_clean_preserves_unrelated_entities(self):
        """清理文档 1 不应影响来自其他文档的实体/关系"""
        from apps.graph.sync import _clean_graph_data

        # 创建来自文档 3 的独立关系
        entity_c = GraphEntity.objects.create(
            name='实体C', type='CONCEPT', description='描述C',
            source_doc_ids=[3])
        entity_d = GraphEntity.objects.create(
            name='实体D', type='PRODUCT', description='描述D',
            source_doc_ids=[3])
        relation2 = GraphRelation.objects.create(
            source_entity=entity_c,
            target_entity=entity_d,
            relation_type='属于',
            source_doc_ids=[3],
        )

        _clean_graph_data(1)

        # 文档 3 的实体和关系不受影响
        assert GraphEntity.objects.filter(id=entity_c.id).exists()
        assert GraphEntity.objects.filter(id=entity_d.id).exists()
        assert GraphRelation.objects.filter(id=relation2.id).exists()
        assert GraphEntity.objects.get(id=entity_c.id).source_doc_ids == [3]


# ============================================================================
# on_document_done / on_document_updated：mock graph_extract_task
# ============================================================================
@pytest.mark.unit
@patch('apps.graph.tasks.graph_extract_task')
def test_on_document_done_calls_delay(mock_task):
    """文档完成时应触发 graph_extract_task.delay(document_id)"""
    from apps.graph.sync import on_document_done

    on_document_done(123)

    mock_task.delay.assert_called_once_with(123)


@pytest.mark.unit
@patch('apps.graph.tasks.graph_extract_task')
def test_on_document_updated_calls_delay(mock_task):
    """文档更新时应触发 graph_extract_task.delay(document_id)"""
    from apps.graph.sync import on_document_updated

    on_document_updated(456)

    mock_task.delay.assert_called_once_with(456)


@pytest.mark.unit
@patch('apps.graph.tasks.graph_extract_task')
def test_on_document_done_different_ids(mock_task):
    """不同 document_id 应正确传递给 delay"""
    from apps.graph.sync import on_document_done

    on_document_done(1)
    on_document_done(2)

    assert mock_task.delay.call_count == 2
    # 验证两次调用的参数
    first_call = mock_task.delay.call_args_list[0]
    second_call = mock_task.delay.call_args_list[1]
    assert first_call.args == (1,)
    assert second_call.args == (2,)


# ============================================================================
# on_document_deleted：mock _clean_graph_data + 验证社区更新
# ============================================================================
@pytest.mark.unit
@patch('apps.graph.models.GraphCommunity')
@patch('apps.graph.sync._clean_graph_data')
def test_on_document_deleted_calls_clean(mock_clean, mock_gc):
    """文档删除时应调用 _clean_graph_data"""
    from apps.graph.sync import on_document_deleted

    mock_clean.return_value = {
        'relations': 1, 'entities_deleted': 1,
        'entities_kept': 0, 'entities_to_refresh': 0,
    }

    on_document_deleted(123)

    mock_clean.assert_called_once_with(123)


@pytest.mark.unit
@patch('apps.graph.models.GraphCommunity')
@patch('apps.graph.sync._clean_graph_data')
def test_on_document_deleted_updates_community_timestamp(mock_clean, mock_gc):
    """文档删除时应更新所有社区的 updated_at（标记社区待刷新）"""
    from apps.graph.sync import on_document_deleted

    mock_clean.return_value = {
        'relations': 0, 'entities_deleted': 0,
        'entities_kept': 0, 'entities_to_refresh': 0,
    }

    on_document_deleted(999)

    # GraphCommunity.objects.all().update(updated_at=...) 应被调用
    mock_gc.objects.all.return_value.update.assert_called_once()
    # 验证 update 参数包含 updated_at
    call_kwargs = mock_gc.objects.all.return_value.update.call_args[1]
    assert 'updated_at' in call_kwargs


@pytest.mark.unit
@patch('apps.graph.models.GraphCommunity')
@patch('apps.graph.sync._clean_graph_data')
def test_on_document_deleted_does_not_trigger_extract(mock_clean, mock_gc):
    """文档删除仅清理数据，不触发重新抽取（与 on_document_done 区分）"""
    from apps.graph.sync import on_document_deleted

    mock_clean.return_value = {
        'relations': 0, 'entities_deleted': 0,
        'entities_kept': 0, 'entities_to_refresh': 0,
    }

    with patch('apps.graph.tasks.graph_extract_task') as mock_task:
        on_document_deleted(999)
        # 不应调用 graph_extract_task.delay
        mock_task.delay.assert_not_called()


# ============================================================================
# on_document_deleted：DB 集成测试（验证社区时间戳真实更新）
# ============================================================================
@pytest.mark.django_db
class TestOnDocumentDeletedDB:
    """on_document_deleted DB 集成测试

    验证 GraphCommunity.updated_at 在文档删除后确实被更新到当前时间。
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入旧时间戳社区"""
        self.community = GraphCommunity.objects.create(
            community_id=0, level=0, entity_ids=[1, 2])
        # 将 updated_at 设为过去时间，便于验证更新
        self.old_time = timezone.now() - timedelta(hours=1)
        GraphCommunity.objects.filter(id=self.community.id).update(updated_at=self.old_time)

    @patch('apps.graph.sync._clean_graph_data')
    def test_community_updated_at_refreshed(self, mock_clean):
        """文档删除后社区 updated_at 应被刷新为当前时间"""
        from apps.graph.sync import on_document_deleted

        mock_clean.return_value = {
            'relations': 0, 'entities_deleted': 0,
            'entities_kept': 0, 'entities_to_refresh': 0,
        }

        on_document_deleted(999)

        self.community.refresh_from_db()
        # updated_at 应大于旧时间（被 update() 刷新为当前时间）
        assert self.community.updated_at > self.old_time

    @patch('apps.graph.sync._clean_graph_data')
    def test_all_communities_updated(self, mock_clean):
        """文档删除后所有社区的 updated_at 都应被更新"""
        from apps.graph.sync import on_document_deleted

        # 创建第二个社区
        community2 = GraphCommunity.objects.create(
            community_id=1, level=0, entity_ids=[3])
        GraphCommunity.objects.filter(id=community2.id).update(updated_at=self.old_time)

        mock_clean.return_value = {
            'relations': 0, 'entities_deleted': 0,
            'entities_kept': 0, 'entities_to_refresh': 0,
        }

        on_document_deleted(999)

        self.community.refresh_from_db()
        community2.refresh_from_db()
        assert self.community.updated_at > self.old_time
        assert community2.updated_at > self.old_time


# ============================================================================
# 集成场景：_clean_graph_data + on_document_deleted 联动
# ============================================================================
@pytest.mark.django_db
class TestOnDocumentDeletedIntegration:
    """on_document_deleted + _clean_graph_data 联动测试

    不 mock _clean_graph_data，验证真实清理 + 社区刷新的完整流程。
    """

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入待清理实体与社区"""
        self.entity = GraphEntity.objects.create(
            name='待清理实体', type='PERSON', description='描述',
            source_doc_ids=[100])
        self.community = GraphCommunity.objects.create(
            community_id=0, level=0, entity_ids=[self.entity.id])
        self.old_time = timezone.now() - timedelta(hours=1)
        GraphCommunity.objects.filter(id=self.community.id).update(updated_at=self.old_time)

    def test_full_cleanup_and_community_refresh(self):
        """文档删除应清理图谱数据并刷新社区时间戳"""
        from apps.graph.sync import on_document_deleted

        on_document_deleted(100)

        # 实体应被删除（source_doc_ids 仅含 100）
        assert not GraphEntity.objects.filter(id=self.entity.id).exists()
        # 社区时间戳应被刷新
        self.community.refresh_from_db()
        assert self.community.updated_at > self.old_time
