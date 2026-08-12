"""
apps.graph.tasks 测试 —— 图谱 Celery 任务（按节点防抖合并）

覆盖范围：
- graph_extract_task(node_id)：按节点批量处理待抽取文档
  - 先清除节点 graph_pending 标记（任务崩溃后不残留）
  - 配置关闭 → 该节点待处理文档标记 skipped，不抽取
  - 收集 graph_status='pending' 的已完成文档，统一标记 extracting 后逐文档
    清理+抽取，结果回写 done / failed
- community_detection_task：获取 LLM 并运行社区检测

DB 集成（django_db）：任务内部直接 ORM 查询节点下文档并回写状态，
需真实 DB 验证状态流转；LLM 抽取链路（extractor）以 mock 隔离。
"""
import uuid

import pytest
from unittest.mock import patch

from apps.knowledge.models import KnowledgeNode, Document, VisibilityLevel
from apps.users.models import User, Department, Team


def _make_node():
    """创建知识节点（level 4 业务分类）"""
    node = KnowledgeNode.objects.create(
        root_type='company_doc', node_type='folder', node_level=4, name='图谱节点')
    node.path = f'/{node.id}/'
    node.depth = 1
    node.save(update_fields=['path', 'depth'])
    return node


def _make_doc(node, owner, title, **extra):
    """创建已完成文档（直接 ORM 写入，绕过上传管线）

    extra 可覆盖默认字段（如 graph_status='pending' 构造待抽取文档）。
    满足 doc_owner_scope_required 约束：team_id / dept_id 至少一个非空。
    """
    dept = Department.objects.create(name=f'图谱部-{title}', code=f'g-{uuid.uuid4().hex[:8]}')
    team = Team.objects.create(
        name=f'图谱组-{title}', code=f't-{uuid.uuid4().hex[:8]}', department=dept)
    fields = {
        'node': node,
        'title': title,
        'file_name': f'{title}.txt',
        'file_type': 'txt',
        'file_size': 100,
        'file_hash': uuid.uuid4().hex,
        'file_path': '/tmp/fake.txt',
        'mime_type': 'text/plain',
        'owner': owner,
        'dept_id': dept.id,
        'team_id': team.id,
        'visibility_level': VisibilityLevel.TEAM_ONLY,
        'root_type': node.root_type,
        'status': 'done',
    }
    fields.update(extra)
    return Document.objects.create(**fields)


@pytest.mark.django_db
@pytest.mark.integration
class TestGraphExtractTask:
    """graph_extract_task 节点批量抽取测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入节点/用户/文档"""
        self.node = _make_node()
        self.owner = User.objects.create_user(
            username='graph-owner', email='graph-owner@test.com', password='x')
        # 2 篇待抽取 + 1 篇已完成（应跳过），验证仅处理 pending 文档
        self.doc_pending_1 = _make_doc(self.node, self.owner, '待抽取1', graph_status='pending')
        self.doc_pending_2 = _make_doc(self.node, self.owner, '待抽取2', graph_status='pending')
        self.doc_done = _make_doc(self.node, self.owner, '已完成', graph_status='done')

    def test_batches_pending_docs_and_writes_done(self):
        """应批量处理节点下 pending 文档并回写 done，跳过已 done 文档"""
        from apps.graph.tasks import graph_extract_task

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document') as mock_batch:
            result = graph_extract_task(self.node.id)

        assert result == {'ok': True, 'processed': 2, 'failed': 0, 'total': 2, 'timed_out': False}
        # 逐文档清理 + 抽取
        assert mock_clean.call_count == 2
        assert mock_batch.call_count == 2
        mock_batch.assert_any_call(self.doc_pending_1.id)
        mock_batch.assert_any_call(self.doc_pending_2.id)
        # 状态回写：pending → done，已 done 的文档不受影响
        self.doc_pending_1.refresh_from_db()
        self.doc_pending_2.refresh_from_db()
        self.doc_done.refresh_from_db()
        assert self.doc_pending_1.graph_status == 'done'
        assert self.doc_pending_2.graph_status == 'done'
        assert self.doc_done.graph_status == 'done'

    def test_clears_node_pending_flag(self):
        """任务执行后应清除节点 graph_pending 标记（自愈，后续文档可重新触发）"""
        from apps.graph.tasks import graph_extract_task
        KnowledgeNode.objects.filter(id=self.node.id).update(graph_pending=True)

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.sync._clean_graph_data'), \
                patch('apps.graph.extractor.batch_extract_for_document'):
            graph_extract_task(self.node.id)

        self.node.refresh_from_db()
        assert self.node.graph_pending is False

    def test_marks_failed_on_extract_error(self):
        """单文档抽取失败应回写 failed 并计入 failed，不影响其他文档"""
        from apps.graph.tasks import graph_extract_task

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document',
                      side_effect=[RuntimeError('llm down'), None]) as mock_batch:
            result = graph_extract_task(self.node.id)

        assert result['processed'] == 1
        assert result['failed'] == 1
        assert result['total'] == 2
        self.doc_pending_1.refresh_from_db()
        self.doc_pending_2.refresh_from_db()
        assert self.doc_pending_1.graph_status == 'failed'
        assert self.doc_pending_2.graph_status == 'done'

    def test_disabled_marks_skipped_without_extract(self):
        """配置关闭时该节点 pending 文档标记 skipped，不执行任何抽取"""
        from apps.graph.tasks import graph_extract_task

        with patch('apps.graph.sync._graph_enabled', return_value=False), \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document') as mock_batch:
            result = graph_extract_task(self.node.id)

        assert result == {'ok': True, 'processed': 0, 'skipped': True}
        mock_clean.assert_not_called()
        mock_batch.assert_not_called()
        self.doc_pending_1.refresh_from_db()
        assert self.doc_pending_1.graph_status == 'skipped'
        self.doc_pending_2.refresh_from_db()
        assert self.doc_pending_2.graph_status == 'skipped'
        # 已 done 文档不受影响
        self.doc_done.refresh_from_db()
        assert self.doc_done.graph_status == 'done'

    def test_no_pending_docs_returns_zero(self):
        """节点下无待抽取文档时直接返回 processed=0，不调用抽取"""
        from apps.graph.tasks import graph_extract_task
        Document.objects.filter(id__in=[self.doc_pending_1.id, self.doc_pending_2.id]).update(
            graph_status='done')

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document') as mock_batch:
            result = graph_extract_task(self.node.id)

        assert result == {'ok': True, 'processed': 0}
        mock_clean.assert_not_called()
        mock_batch.assert_not_called()


@pytest.mark.unit
@patch('apps.graph.community.run_community_detection')
@patch('apps.llm.factory.get_llm')
def test_community_detection_task(mock_get_llm, mock_run):
    """community_detection_task 应获取 LLM 并运行社区检测，返回社区数量"""
    from apps.graph.tasks import community_detection_task
    mock_run.return_value = 5
    count = community_detection_task()
    assert count == 5
    mock_get_llm.assert_called_once()
    mock_run.assert_called_once()
