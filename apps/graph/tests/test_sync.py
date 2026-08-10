"""
apps.graph.sync 单元/集成测试 —— 图谱增量同步与数据一致性

覆盖范围：
- _clean_graph_data：关系清理 + 实体引用移除（保留多来源实体 / 删除无来源实体）
- on_document_done：文档完成防抖派发节点级抽取（置 pending + check-and-set + 标记 skipped 分支）
- on_document_updated：文档更新防抖派发节点级重新抽取
- on_document_deleted：文档删除清理图谱数据 + 标记社区待刷新

测试分层：
- on_document_done / on_document_updated：mock Document/KnowledgeNode/_graph_enabled +
  graph_extract_task.delay，验证防抖派发与 skipped 分支
- on_document_deleted：mock _clean_graph_data + DB 集成验证社区时间戳更新
- _clean_graph_data：DB 集成测试，验证关系/实体的清理与保留逻辑
"""
import pytest
from contextlib import ExitStack, contextmanager
from unittest.mock import patch, MagicMock

from django.utils import timezone
from datetime import timedelta

from apps.graph.models import GraphEntity, GraphRelation, GraphCommunity


# ============================================================================
# _clean_graph_data：DB 集成测试
# ============================================================================
@pytest.mark.django_db
@pytest.mark.integration
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
# on_document_done / on_document_updated：节点级防抖派发
# ============================================================================
def _fake_doc(document_id, node_id, chunk_count=10):
    """构造最小 Document 实例替身（含 id / node_id / chunk_count）"""
    doc = MagicMock()
    doc.id = document_id
    doc.node_id = node_id
    doc.chunk_count = chunk_count
    return doc


@contextmanager
def _patch_dispatch_env(doc, node_update_returns=1):
    """构造防抖派发共享 mock 环境（with 语句管理 patch 生命周期）

    patch Document/KnowledgeNode 查询链 + _graph_enabled(开启) + graph_extract_task，
    doc 同时供多个 document_id 查询复用（用于模拟同节点多文档场景）。
    """
    with ExitStack() as stack:
        mock_doc = stack.enter_context(patch('apps.knowledge.models.Document'))
        mock_node = stack.enter_context(patch('apps.knowledge.models.KnowledgeNode'))
        stack.enter_context(patch('apps.graph.sync._graph_enabled', return_value=True))
        mock_task = stack.enter_context(patch('apps.graph.tasks.graph_extract_task'))
        mock_doc.objects.filter.return_value.only.return_value.first.return_value = doc
        mock_node.objects.filter.return_value.update.return_value = node_update_returns
        yield mock_doc, mock_node, mock_task


@pytest.mark.unit
class TestOnDocumentDoneDispatch:
    """on_document_done 防抖派发测试（mock Document/KnowledgeNode/task）"""

    def test_document_not_found_no_dispatch(self):
        """文档不存在（filter().first() 返回 None）时不派发"""
        from apps.graph.sync import on_document_done
        with patch('apps.knowledge.models.Document') as mock_doc, \
                patch('apps.graph.tasks.graph_extract_task') as mock_task:
            mock_doc.objects.filter.return_value.only.return_value.first.return_value = None

            on_document_done(999)

        mock_task.delay.assert_not_called()

    def test_done_marks_pending_and_dispatches_node(self):
        """文档完成应置 graph_status=pending，并派发节点级抽取任务"""
        from apps.graph.sync import on_document_done
        doc = _fake_doc(document_id=1, node_id=42)

        with _patch_dispatch_env(doc, node_update_returns=1) as (mock_doc, mock_node, mock_task):
            on_document_done(1)

            # 文档置 pending
            mock_doc.objects.filter.assert_any_call(id=1)
            mock_doc.objects.filter.return_value.update.assert_called_once_with(
                graph_status='pending')
            # 节点原子 check-and-set：id + 未删除 + 当前无待构建标记
            mock_node.objects.filter.assert_called_once_with(
                id=42, is_deleted=False, graph_pending=False)
            mock_node.objects.filter.return_value.update.assert_called_once_with(
                graph_pending=True)
            # 派发参数为节点 ID
            mock_task.delay.assert_called_once_with(42)

    def test_same_node_dispatches_once(self):
        """同一节点多个文档连续完成时，仅首个完成者派发节点任务（防抖合并）"""
        from apps.graph.sync import on_document_done
        # 同一 node_id 的文档 1、2 复用同一 mock 查询
        doc = _fake_doc(document_id=1, node_id=42)

        with _patch_dispatch_env(doc) as (mock_doc, mock_node, mock_task):
            # 第一次节点 update 返回 1（首个文档完成 → 派发），第二次返回 0（已有标记 → 不派发）
            mock_node.objects.filter.return_value.update.side_effect = [1, 0]

            on_document_done(1)
            on_document_done(2)

            mock_task.delay.assert_called_once_with(42)
            # 两个文档都应置 pending（合并到同一次节点任务）
            assert mock_doc.objects.filter.return_value.update.call_count == 2

    def test_disabled_marks_skipped_without_dispatch(self):
        """配置关闭（GRAPH_ENABLED=false）时标记 skipped，不派发"""
        from apps.graph.sync import on_document_done
        doc = _fake_doc(document_id=1, node_id=42)
        with patch('apps.knowledge.models.Document') as mock_doc, \
                patch('apps.graph.sync._graph_enabled', return_value=False), \
                patch('apps.graph.tasks.graph_extract_task') as mock_task:
            mock_doc.objects.filter.return_value.only.return_value.first.return_value = doc

            on_document_done(1)

            mock_doc.objects.filter.return_value.update.assert_called_once_with(
                graph_status='skipped')
            mock_task.delay.assert_not_called()

    def test_no_chunks_marks_skipped_without_dispatch(self):
        """无切片数据（chunk_count=0）时标记 skipped，避免无意义空抽取"""
        from apps.graph.sync import on_document_done
        doc = _fake_doc(document_id=1, node_id=42, chunk_count=0)
        with patch('apps.knowledge.models.Document') as mock_doc, \
                patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks.graph_extract_task') as mock_task:
            mock_doc.objects.filter.return_value.only.return_value.first.return_value = doc

            on_document_done(1)

            mock_doc.objects.filter.return_value.update.assert_called_once_with(
                graph_status='skipped')
            mock_task.delay.assert_not_called()


@pytest.mark.unit
class TestOnDocumentUpdatedDispatch:
    """on_document_updated 防抖派发测试（与 on_document_done 共用 _dispatch_node_graph_task）"""

    def test_updated_marks_pending_and_dispatches_node(self):
        """文档更新应置 pending 并派发节点级重新抽取任务"""
        from apps.graph.sync import on_document_updated
        doc = _fake_doc(document_id=456, node_id=42)

        with _patch_dispatch_env(doc, node_update_returns=1) as (mock_doc, mock_node, mock_task):
            on_document_updated(456)

            mock_doc.objects.filter.assert_any_call(id=456)
            mock_task.delay.assert_called_once_with(42)

    def test_updated_debounce_same_node(self):
        """同节点已有待构建标记时，更新不重复派发"""
        from apps.graph.sync import on_document_updated
        doc = _fake_doc(document_id=456, node_id=42)

        with _patch_dispatch_env(doc, node_update_returns=0) as (mock_doc, mock_node, mock_task):
            on_document_updated(456)

            mock_task.delay.assert_not_called()


# ============================================================================
# on_document_deleted：mock _clean_graph_data + 验证社区更新
# ============================================================================
@pytest.mark.unit
@patch('apps.graph.models.GraphCommunity')
@patch('apps.graph.sync._clean_graph_data')
@patch('apps.knowledge.models.KnowledgeNode')
@patch('apps.knowledge.models.Document')
def test_on_document_deleted_calls_clean(mock_doc, mock_node, mock_clean, mock_gc):
    """文档删除时应调用 _clean_graph_data"""
    from apps.graph.sync import on_document_deleted

    # 文档已从库中删除（软删后 node_id 查询为空），节点自愈逻辑跳过
    mock_doc.objects.filter.return_value.values_list.return_value.first.return_value = None
    mock_clean.return_value = {
        'relations': 1, 'entities_deleted': 1,
        'entities_kept': 0, 'entities_to_refresh': 0,
    }

    on_document_deleted(123)

    mock_clean.assert_called_once_with(123)


@pytest.mark.unit
@patch('apps.graph.models.GraphCommunity')
@patch('apps.graph.sync._clean_graph_data')
@patch('apps.knowledge.models.KnowledgeNode')
@patch('apps.knowledge.models.Document')
def test_on_document_deleted_updates_community_timestamp(mock_doc, mock_node, mock_clean, mock_gc):
    """文档删除时应更新所有社区的 updated_at（标记社区待刷新）"""
    from apps.graph.sync import on_document_deleted

    mock_doc.objects.filter.return_value.values_list.return_value.first.return_value = None
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
@patch('apps.knowledge.models.KnowledgeNode')
@patch('apps.knowledge.models.Document')
def test_on_document_deleted_does_not_trigger_extract(mock_doc, mock_node, mock_clean, mock_gc):
    """文档删除仅清理数据，不触发重新抽取（与 on_document_done 区分）"""
    from apps.graph.sync import on_document_deleted

    mock_doc.objects.filter.return_value.values_list.return_value.first.return_value = None
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
@pytest.mark.integration
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
@pytest.mark.integration
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
