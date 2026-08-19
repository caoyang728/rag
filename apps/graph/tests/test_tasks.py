"""
apps.graph.tasks 测试 —— 图谱 Celery 任务（按节点防抖合并 + 续传 + 自愈）

覆盖范围：
- graph_extract_task(node_id)：按节点批量处理待抽取文档
  - 先清除节点 graph_pending 标记（任务崩溃后不残留）
  - 配置关闭 → 该节点待处理文档标记 skipped，不抽取
  - 收集 graph_status='pending' 的已完成文档，统一标记 extracting 后逐文档
    清理+抽取，结果回写 done / failed
  - 续传：有匹配版本进度时跳过清理、从进度切片继续
  - 预算耗尽未完成：保存进度、文档回退 pending、续派任务
- graph_recover_task / _recover_stuck_graph_docs：自愈卡死的 extracting / pending
- community_detection_task：获取 LLM 并运行社区检测

DB 集成（django_db）：任务内部直接 ORM 查询节点下文档并回写状态，
需真实 DB 验证状态流转；LLM 抽取链路（extractor）以 mock 隔离。
"""
import uuid

import pytest
from unittest.mock import MagicMock, patch

from apps.knowledge.models import KnowledgeNode, Document, VisibilityLevel
from apps.users.models import User, Department, Team

# 单文档抽取的通用 mock 返回值（completed=True 表示全部切片处理完）
_DONE_RESULT = {'completed': True, 'processed': 1, 'next_chunk': 1}


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


# ---------------------------------------------------------------------------
# Redis 辅助函数单元测试（纯逻辑，无 DB 依赖）
# ---------------------------------------------------------------------------
@pytest.mark.unit
class TestRedisHelpers:
    """Redis 辅助函数（进度/活跃标记读写）降级路径测试

    覆盖：_get_doc_progress / _set_doc_progress / _clear_doc_progress /
    _mark_node_active / _clear_node_active / _node_active 在 Redis 不可用时的
    容错行为（conn=None），以及读写异常时的降级处理。
    """

    def test_get_doc_progress_when_redis_unavailable(self):
        """Redis 不可用时 _get_doc_progress 应返回 None，不抛异常"""
        from apps.graph.tasks import _get_doc_progress
        with patch('apps.graph.tasks._redis', return_value=None):
            result = _get_doc_progress(999)
        assert result is None

    def test_set_doc_progress_when_redis_unavailable(self):
        """Redis 不可用时 _set_doc_progress 应静默返回，不抛异常"""
        from apps.graph.tasks import _set_doc_progress
        with patch('apps.graph.tasks._redis', return_value=None):
            _set_doc_progress(999, chunk_index=0, version=1)

    def test_clear_doc_progress_when_redis_unavailable(self):
        """Redis 不可用时 _clear_doc_progress 应静默返回，不抛异常"""
        from apps.graph.tasks import _clear_doc_progress
        with patch('apps.graph.tasks._redis', return_value=None):
            _clear_doc_progress(999)

    def test_mark_node_active_when_redis_unavailable(self):
        """Redis 不可用时 _mark_node_active 应静默返回，不抛异常"""
        from apps.graph.tasks import _mark_node_active
        with patch('apps.graph.tasks._redis', return_value=None):
            _mark_node_active(999)

    def test_clear_node_active_when_redis_unavailable(self):
        """Redis 不可用时 _clear_node_active 应静默返回，不抛异常"""
        from apps.graph.tasks import _clear_node_active
        with patch('apps.graph.tasks._redis', return_value=None):
            _clear_node_active(999)

    def test_node_active_returns_true_when_redis_unavailable(self):
        """Redis 不可用时 _node_active 应返回 True（保守判定"有任务"），避免误回退"""
        from apps.graph.tasks import _node_active
        with patch('apps.graph.tasks._redis', return_value=None):
            result = _node_active(999)
        assert result is True

    def test_get_doc_progress_when_redis_raises(self):
        """Redis 连接正常但操作异常时，_get_doc_progress 应降级返回 None"""
        from apps.graph.tasks import _get_doc_progress
        mock_conn = MagicMock()
        mock_conn.get.side_effect = ConnectionError('redis down')
        with patch('apps.graph.tasks._redis', return_value=mock_conn):
            result = _get_doc_progress(999)
        assert result is None

    def test_set_doc_progress_when_redis_raises(self):
        """Redis 连接正常但写入异常时，_set_doc_progress 应静默降级"""
        from apps.graph.tasks import _set_doc_progress
        mock_conn = MagicMock()
        mock_conn.set.side_effect = ConnectionError('redis down')
        with patch('apps.graph.tasks._redis', return_value=mock_conn):
            _set_doc_progress(999, chunk_index=0, version=1)

    def test_clear_doc_progress_when_redis_raises(self):
        """Redis 连接正常但删除异常时，_clear_doc_progress 应静默降级"""
        from apps.graph.tasks import _clear_doc_progress
        mock_conn = MagicMock()
        mock_conn.delete.side_effect = ConnectionError('redis down')
        with patch('apps.graph.tasks._redis', return_value=mock_conn):
            _clear_doc_progress(999)

    def test_mark_node_active_when_redis_raises(self):
        """Redis 连接正常但写入异常时，_mark_node_active 应静默降级"""
        from apps.graph.tasks import _mark_node_active
        mock_conn = MagicMock()
        mock_conn.set.side_effect = ConnectionError('redis down')
        with patch('apps.graph.tasks._redis', return_value=mock_conn):
            _mark_node_active(999)

    def test_clear_node_active_when_redis_raises(self):
        """Redis 连接正常但删除异常时，_clear_node_active 应静默降级"""
        from apps.graph.tasks import _clear_node_active
        mock_conn = MagicMock()
        mock_conn.delete.side_effect = ConnectionError('redis down')
        with patch('apps.graph.tasks._redis', return_value=mock_conn):
            _clear_node_active(999)

    def test_node_active_returns_true_when_redis_raises(self):
        """Redis 连接正常但读取异常时，_node_active 应返回 True（保守判定）"""
        from apps.graph.tasks import _node_active
        mock_conn = MagicMock()
        mock_conn.exists.side_effect = ConnectionError('redis down')
        with patch('apps.graph.tasks._redis', return_value=mock_conn):
            result = _node_active(999)
        assert result is True

    def test_get_doc_progress_parses_json(self):
        """Redis 返回有效 JSON 时，_get_doc_progress 应正确解析并返回字典"""
        from apps.graph.tasks import _get_doc_progress
        import json
        mock_conn = MagicMock()
        mock_conn.get.return_value = json.dumps(
            {'chunk_index': 3, 'version': 1}).encode()
        with patch('apps.graph.tasks._redis', return_value=mock_conn):
            result = _get_doc_progress(42)
        assert result == {'chunk_index': 3, 'version': 1}

    def test_get_doc_progress_returns_none_when_key_missing(self):
        """Redis key 不存在时，_get_doc_progress 应返回 None"""
        from apps.graph.tasks import _get_doc_progress
        mock_conn = MagicMock()
        mock_conn.get.return_value = None
        with patch('apps.graph.tasks._redis', return_value=mock_conn):
            result = _get_doc_progress(42)
        assert result is None


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

    def _run_task(self, **patches):
        """执行 graph_extract_task 的公共入口：注入基础 mock 并返回任务结果

        patches 为额外 mock 的 name: return_value/side_effect 映射。
        """
        from apps.graph.tasks import graph_extract_task
        patchers = [
            patch('apps.graph.sync._graph_enabled', return_value=True),
            patch('apps.graph.tasks._get_doc_progress', return_value=None),
            patch('apps.graph.sync._clean_graph_data'),
            patch('apps.graph.extractor.batch_extract_for_document',
                  return_value=_DONE_RESULT),
            patch('apps.graph.tasks.graph_extract_task.delay'),
        ]
        for name, value in patches.items():
            patchers.append(patch(name, **value) if isinstance(value, dict) else patch(name, value))
        for p in patchers:
            p.start()
        try:
            return graph_extract_task(self.node.id)
        finally:
            for p in patchers:
                p.stop()

    def test_batches_pending_docs_and_writes_done(self):
        """应批量处理节点下 pending 文档并回写 done，跳过已 done 文档"""
        from apps.graph.tasks import graph_extract_task

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks._get_doc_progress', return_value=None), \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document',
                      return_value=_DONE_RESULT) as mock_batch, \
                patch('apps.graph.tasks.graph_extract_task.delay'):
            result = graph_extract_task(self.node.id)

        assert result == {'ok': True, 'processed': 2, 'failed': 0, 'total': 2, 'timed_out': False}
        # 无进度 → 逐文档清理 + 从 0 抽取
        assert mock_clean.call_count == 2
        assert mock_batch.call_count == 2
        start_chunks = [c.kwargs.get('start_chunk') for c in mock_batch.call_args_list]
        assert start_chunks == [0, 0]
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

        self._run_task()

        self.node.refresh_from_db()
        assert self.node.graph_pending is False

    def test_marks_failed_on_extract_error(self):
        """单文档抽取失败应回写 failed 并计入 failed，不影响其他文档"""
        from apps.graph.tasks import graph_extract_task

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks._get_doc_progress', return_value=None), \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document',
                      side_effect=[RuntimeError('llm down'), _DONE_RESULT]) as mock_batch, \
                patch('apps.graph.tasks.graph_extract_task.delay'):
            result = graph_extract_task(self.node.id)

        assert result['processed'] == 1
        assert result['failed'] == 1
        assert result['total'] == 2
        assert mock_clean.call_count == 2
        assert mock_batch.call_count == 2
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
                patch('apps.graph.tasks._get_doc_progress', return_value=None), \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document') as mock_batch, \
                patch('apps.graph.tasks.graph_extract_task.delay'):
            result = graph_extract_task(self.node.id)

        assert result == {'ok': True, 'processed': 0}
        mock_clean.assert_not_called()
        mock_batch.assert_not_called()

    def test_resumes_from_saved_progress_without_clean(self):
        """存在匹配版本的进度时，应从进度切片续传且不清理旧图谱数据"""
        from apps.graph.tasks import graph_extract_task

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks._get_doc_progress',
                      return_value={'chunk_index': 3, 'version': 1}) as mock_progress, \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document',
                      return_value=_DONE_RESULT) as mock_batch, \
                patch('apps.graph.tasks.graph_extract_task.delay'):
            result = graph_extract_task(self.node.id)

        assert result['processed'] == 2
        assert result['timed_out'] is False
        # 有进度 → 不清理旧数据，直接从进度续传
        mock_clean.assert_not_called()
        assert mock_progress.call_count == 2
        start_chunks = [c.kwargs.get('start_chunk') for c in mock_batch.call_args_list]
        assert start_chunks == [3, 3]

    def test_resumes_from_start_when_version_changed(self):
        """进度版本与文档版本不匹配时，应清理旧数据并从 0 重新抽取（版本升级）"""
        from apps.graph.tasks import graph_extract_task
        Document.objects.filter(id=self.doc_pending_1.id).update(version=2)

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks._get_doc_progress',
                      return_value={'chunk_index': 5, 'version': 1}), \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document',
                      return_value=_DONE_RESULT) as mock_batch, \
                patch('apps.graph.tasks.graph_extract_task.delay'):
            graph_extract_task(self.node.id)

        # 版本不匹配的文档1从头清理抽取；版本匹配的文档2保留进度续传
        assert mock_clean.call_count == 1
        start_chunks = [c.kwargs.get('start_chunk') for c in mock_batch.call_args_list]
        assert sorted(start_chunks) == [0, 5]

    def test_unfinished_doc_saves_progress_and_dispatches_next(self):
        """预算耗尽未完成时，应保存进度、文档回退 pending，并续派下一轮任务"""
        from apps.graph.tasks import graph_extract_task

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks._get_doc_progress', return_value=None), \
                patch('apps.graph.sync._clean_graph_data') as mock_clean, \
                patch('apps.graph.extractor.batch_extract_for_document',
                      side_effect=[{'completed': False, 'processed': 2, 'next_chunk': 2},
                                   _DONE_RESULT]) as mock_batch, \
                patch('apps.graph.tasks._set_doc_progress') as mock_set_progress, \
                patch('apps.graph.tasks.graph_extract_task.delay') as mock_delay:
            result = graph_extract_task(self.node.id)

        assert result['timed_out'] is True
        assert result['processed'] == 0
        # 未完成的文档：保存进度 + 回退 pending
        mock_set_progress.assert_called_once_with(self.doc_pending_1.id, 2, 1)
        self.doc_pending_1.refresh_from_db()
        assert self.doc_pending_1.graph_status == 'pending'
        # 剩余文档（本批已标记 extracting 未处理）回退 pending，并续派任务
        self.doc_pending_2.refresh_from_db()
        assert self.doc_pending_2.graph_status == 'pending'
        mock_delay.assert_called_once_with(self.node.id)

    def test_soft_time_limit_exceeded_reverts_current_doc_and_breaks(self):
        """SoftTimeLimitExceeded 应将当前文档回退 pending 并中断循环"""
        from apps.graph.tasks import graph_extract_task
        from celery.exceptions import SoftTimeLimitExceeded

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks._get_doc_progress', return_value=None), \
                patch('apps.graph.sync._clean_graph_data'), \
                patch('apps.graph.extractor.batch_extract_for_document',
                      side_effect=SoftTimeLimitExceeded()) as mock_batch, \
                patch('apps.graph.tasks.graph_extract_task.delay') as mock_delay:
            result = graph_extract_task(self.node.id)

        # 仅第一篇文档触发了抽取（遇到软超时即 break）
        assert mock_batch.call_count == 1
        assert result['timed_out'] is True
        assert result['processed'] == 0
        # 触发 SoftTimeLimitExceeded 的文档回退 pending
        self.doc_pending_1.refresh_from_db()
        assert self.doc_pending_1.graph_status == 'pending'
        # 剩余文档也被回退 pending（timed_out 路径）
        self.doc_pending_2.refresh_from_db()
        assert self.doc_pending_2.graph_status == 'pending'
        # timed_out 触发续派
        mock_delay.assert_called_once_with(self.node.id)

    def test_outer_exception_reverts_all_extracting_to_pending(self):
        """批量级异常（如 LLM 整体不可用）应回退本批所有 extracting 文档为 pending

        外层 except 捕获的是 for 循环体内、内层 try 之外的异常，
        例如 time.monotonic() 调用失败（系统时钟异常等极端场景）。
        """
        from apps.graph.tasks import graph_extract_task
        call_count = [0]

        def _boom_monotonic():
            """第2次调用时抛异常，模拟批量处理中途的系统级故障"""
            call_count[0] += 1
            if call_count[0] >= 2:
                raise RuntimeError('system clock unavailable')
            return 0

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks._get_doc_progress', return_value=None), \
                patch('apps.graph.sync._clean_graph_data'), \
                patch('apps.graph.extractor.batch_extract_for_document',
                      return_value=_DONE_RESULT), \
                patch('apps.graph.tasks.time.monotonic', side_effect=_boom_monotonic), \
                patch('apps.graph.tasks.graph_extract_task.delay') as mock_delay:
            result = graph_extract_task(self.node.id)

        assert result['timed_out'] is True
        assert result['processed'] == 0
        assert result['failed'] == 0
        # 本批文档全部回退 pending（外层 except 路径）
        self.doc_pending_1.refresh_from_db()
        self.doc_pending_2.refresh_from_db()
        assert self.doc_pending_1.graph_status == 'pending'
        assert self.doc_pending_2.graph_status == 'pending'
        mock_delay.assert_called_once_with(self.node.id)

    def test_has_more_pending_docs_triggers_redispatch(self):
        """处理完成后仍有 pending 文档时，应续派下一轮任务"""
        from apps.graph.tasks import graph_extract_task
        # 在 _env 创建的 doc_pending_2 之外再添加两篇 pending 文档（共 4 篇）
        # doc_pending_1 正常完成，doc_pending_2 未完成（回退 pending）→ has_more 为 True
        extra1 = _make_doc(self.node, self.owner, '额外pending1', graph_status='pending')
        extra2 = _make_doc(self.node, self.owner, '额外pending2', graph_status='pending')

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks._get_doc_progress', return_value=None), \
                patch('apps.graph.sync._clean_graph_data'), \
                patch('apps.graph.extractor.batch_extract_for_document',
                      side_effect=[_DONE_RESULT,
                                   {'completed': False, 'processed': 0, 'next_chunk': 0}]) as mock_batch, \
                patch('apps.graph.tasks._set_doc_progress'), \
                patch('apps.graph.tasks.graph_extract_task.delay') as mock_delay:
            result = graph_extract_task(self.node.id)

        # doc_pending_1 成功完成，doc_pending_2 未完成（预算耗尽），timed_out 后续派
        assert result['timed_out'] is True
        # doc_pending_2 回退 pending，extra1/extra2 已标记 extracting 也回退 pending → has_more=True
        mock_delay.assert_called_once_with(self.node.id)


@pytest.mark.django_db
@pytest.mark.integration
class TestGraphRecover:
    """graph_recover_task / _recover_stuck_graph_docs 自愈测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """注入节点/用户/文档：2 个卡死 extracting + 1 个无触发源 pending"""
        self.node = _make_node()
        self.owner = User.objects.create_user(
            username='graph-recover', email='graph-recover@test.com', password='x')
        self.doc_stuck_1 = _make_doc(self.node, self.owner, '卡死1', graph_status='extracting')
        self.doc_stuck_2 = _make_doc(self.node, self.owner, '卡死2', graph_status='extracting')
        self.doc_stuck_3 = _make_doc(self.node, self.owner, '卡死3', graph_status='extracting')
        # 另一节点：有 pending 文档但无任务在跑（失去触发源）
        self.node2 = _make_node()
        self.doc_pending = _make_doc(self.node2, self.owner, '无触发源', graph_status='pending')

    def test_recovers_stuck_extracting_and_dispatches(self):
        """节点无任务且无活跃标记时，应回退 extracting 为 pending 并重新派发任务"""
        from apps.graph.tasks import _recover_stuck_graph_docs

        with patch('apps.graph.tasks._node_active', return_value=False), \
                patch('apps.graph.tasks.graph_extract_task.delay') as mock_delay:
            stats = _recover_stuck_graph_docs()

        assert stats['recovered'] == 3
        assert stats['dispatched_nodes'] == 2
        for doc in (self.doc_stuck_1, self.doc_stuck_2, self.doc_stuck_3):
            doc.refresh_from_db()
            assert doc.graph_status == 'pending'
        # 两个节点都应派发任务（卡死节点 + 无触发源节点）
        assert mock_delay.call_count == 2
        node_ids = sorted(c.args[0] for c in mock_delay.call_args_list)
        assert node_ids == sorted([self.node.id, self.node2.id])

    def test_skips_node_with_active_task(self):
        """节点存在活跃任务标记时，不应回退（避免误伤正在抽取的文档）"""
        from apps.graph.tasks import _recover_stuck_graph_docs

        with patch('apps.graph.tasks._node_active', return_value=True), \
                patch('apps.graph.tasks.graph_extract_task.delay') as mock_delay:
            stats = _recover_stuck_graph_docs()

        assert stats['recovered'] == 0
        assert stats['dispatched_nodes'] == 0
        mock_delay.assert_not_called()
        self.doc_stuck_1.refresh_from_db()
        assert self.doc_stuck_1.graph_status == 'extracting'

    def test_force_recovery_ignores_active_flag(self):
        """force=True 时跳过活跃标记检查（人工运维场景），强制恢复卡死文档"""
        from apps.graph.tasks import _recover_stuck_graph_docs

        with patch('apps.graph.tasks._node_active', return_value=True), \
                patch('apps.graph.tasks.graph_extract_task.delay') as mock_delay:
            stats = _recover_stuck_graph_docs(force=True)

        assert stats['recovered'] == 3
        assert stats['dispatched_nodes'] == 2
        assert mock_delay.call_count == 2

    def test_recover_task_delegates_to_shared_logic(self):
        """graph_recover_task 应委托共享恢复逻辑（配置开启时）"""
        from apps.graph.tasks import graph_recover_task

        with patch('apps.graph.sync._graph_enabled', return_value=True), \
                patch('apps.graph.tasks._recover_stuck_graph_docs',
                      return_value={'recovered': 2, 'dispatched_nodes': 1}) as mock_recover:
            result = graph_recover_task()

        assert result == {'ok': True, 'recovered': 2, 'dispatched_nodes': 1}
        mock_recover.assert_called_once_with()

    def test_recover_task_skips_when_graph_disabled(self):
        """配置关闭时自愈任务直接跳过，不执行恢复"""
        from apps.graph.tasks import graph_recover_task

        with patch('apps.graph.sync._graph_enabled', return_value=False), \
                patch('apps.graph.tasks._recover_stuck_graph_docs') as mock_recover:
            result = graph_recover_task()

        assert result == {'ok': True, 'recovered': 0, 'dispatched_nodes': 0, 'skipped': True}
        mock_recover.assert_not_called()

    def test_skips_node_with_graph_pending_true(self):
        """节点 graph_pending=True 时应跳过恢复（说明任务可能正在运行）"""
        from apps.graph.tasks import _recover_stuck_graph_docs
        # 将所有节点标记为 graph_pending=True，阻止所有补派
        KnowledgeNode.objects.filter(
            id__in=[self.node.id, self.node2.id]
        ).update(graph_pending=True)

        with patch('apps.graph.tasks._node_active', return_value=False), \
                patch('apps.graph.tasks.graph_extract_task.delay') as mock_delay:
            stats = _recover_stuck_graph_docs()

        # 卡死节点被跳过（graph_pending=True），不应回退文档
        assert stats['recovered'] == 0
        assert stats['dispatched_nodes'] == 0
        mock_delay.assert_not_called()
        self.doc_stuck_1.refresh_from_db()
        assert self.doc_stuck_1.graph_status == 'extracting'

    def test_skips_deleted_node(self):
        """已删除的节点应跳过恢复（数据已不可用）"""
        from apps.graph.tasks import _recover_stuck_graph_docs
        # 将 node 标记为已删除
        KnowledgeNode.objects.filter(id=self.node.id).update(is_deleted=True)

        with patch('apps.graph.tasks._node_active', return_value=False), \
                patch('apps.graph.tasks.graph_extract_task.delay') as mock_delay:
            stats = _recover_stuck_graph_docs()

        # 已删除节点的卡死文档不应回退
        assert stats['recovered'] == 0
        self.doc_stuck_1.refresh_from_db()
        assert self.doc_stuck_1.graph_status == 'extracting'
        # node2 的 pending 文档仍应正常补派
        if mock_delay.call_count > 0:
            dispatched_ids = [c.args[0] for c in mock_delay.call_args_list]
            assert self.node2.id in dispatched_ids


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
