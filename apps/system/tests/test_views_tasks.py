"""
apps.system.views 后台任务看板接口集成测试

覆盖范围：
- TaskLogView：列表 / status 过滤 / task_name 模糊 / task_id 精确 / 分页上限 / 权限（401/403/200）
- TaskStatsView：状态分布统计 / 平均与最慢耗时 / 队列深度（mock 快照）
- TaskRetryView：非法 task_id 400 / 不存在 404 / 非失败 409 / 成功 202 派发参数校验 / 派发异常 500
- cleanup_task_logs：超期日志清理 / 保留期配置生效

复用 SystemAPITestBase（JWT + 超管/普通用户环境）。
"""
import uuid
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.utils import timezone

from apps.system.models import CeleryTaskLog, SystemConfig
from apps.system.tasks import cleanup_task_logs
from apps.system.tests.test_views import SystemAPITestBase

TASKS_URL = '/api/v1/system/tasks/'
STATS_URL = '/api/v1/system/tasks/stats/'


def _make_task_log(task_id, task_name='apps.qa.tasks.run', status='success',
                   queue='default', **extra):
    """构造任务日志记录（默认参数可被 extra 覆盖）

    created_at 是 auto_now_add 字段，create 时显式传值会被覆盖为当前时间，
    需先创建再用 update 落历史时间，供清理任务测试构造超期数据。
    """
    created_at = extra.pop('created_at', None)
    defaults = {
        'task_name': task_name, 'status': status, 'queue': queue,
        'args': [], 'kwargs': {},
    }
    defaults.update(extra)
    row = CeleryTaskLog.objects.create(task_id=task_id, **defaults)
    if created_at is not None:
        CeleryTaskLog.objects.filter(pk=row.pk).update(created_at=created_at)
    return row


def _retry_url(task_id):
    return f'/api/v1/system/tasks/{task_id}/retry/'


class TestTaskLogViewAPI(SystemAPITestBase):
    """任务日志列表接口测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_list_when_anonymous_then_401(self):
        resp = self.client.get(TASKS_URL)
        assert resp.status_code == 401

    @pytest.mark.integration
    def test_list_when_normal_user_then_403(self):
        resp = self.client.get(TASKS_URL, **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_list_when_admin_then_200_with_items(self):
        _make_task_log('t-1', status='success')
        _make_task_log('t-2', status='failure')
        resp = self.client.get(TASKS_URL, **self.admin_a_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == 2
        assert data['page'] == 1
        assert data['page_size'] == 50
        # 倒序：后创建的在前
        assert data['items'][0]['task_id'] == 't-2'

    @pytest.mark.integration
    def test_list_filter_by_status(self):
        _make_task_log('t-1', status='success')
        _make_task_log('t-2', status='failure')
        resp = self.client.get(TASKS_URL, {'status': 'failure'}, **self.admin_a_headers)
        data = resp.json()
        assert data['total'] == 1
        assert data['items'][0]['task_id'] == 't-2'

    @pytest.mark.integration
    def test_list_filter_by_task_name_fuzzy(self):
        _make_task_log('t-1', task_name='apps.qa.tasks.run')
        _make_task_log('t-2', task_name='apps.analytics.tasks.compute')
        resp = self.client.get(TASKS_URL, {'task_name': 'analytics'}, **self.admin_a_headers)
        data = resp.json()
        assert data['total'] == 1
        assert data['items'][0]['task_name'] == 'apps.analytics.tasks.compute'

    @pytest.mark.integration
    def test_list_filter_by_task_id_exact(self):
        _make_task_log('t-1')
        _make_task_log('t-2')
        resp = self.client.get(TASKS_URL, {'task_id': 't-1'}, **self.admin_a_headers)
        data = resp.json()
        assert data['total'] == 1
        assert data['items'][0]['task_id'] == 't-1'

    @pytest.mark.integration
    def test_list_page_size_capped_at_200(self):
        for i in range(5):
            _make_task_log(f't-{i}')
        resp = self.client.get(TASKS_URL, {'page_size': 9999}, **self.admin_a_headers)
        assert resp.json()['page_size'] == 200

    @pytest.mark.integration
    def test_list_serializes_nullable_time(self):
        """started_at 为空时返回 None，不抛异常"""
        _make_task_log('t-1', started_at=None, finished_at=None)
        resp = self.client.get(TASKS_URL, **self.admin_a_headers)
        item = resp.json()['items'][0]
        assert item['started_at'] is None
        assert item['finished_at'] is None


class TestTaskStatsViewAPI(SystemAPITestBase):
    """任务状态统计接口测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_stats_when_normal_user_then_403(self):
        resp = self.client.get(STATS_URL, **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_stats_counts_distribution(self):
        _make_task_log('t-1', status='success', duration_ms=100)
        _make_task_log('t-2', status='success', duration_ms=300)
        _make_task_log('t-3', status='failure')
        _make_task_log('t-4', status='started')
        _make_task_log('t-5', status='pending')
        resp = self.client.get(STATS_URL, **self.admin_a_headers)
        assert resp.status_code == 200
        counts = resp.json()['counts']
        assert counts['success'] == 2
        assert counts['failure'] == 1
        assert counts['started'] == 1
        assert counts['pending'] == 1
        # 已结束任务耗时统计：成功两条均计耗时，失败默认 0 不计入
        assert resp.json()['avg_duration_ms'] == 200
        assert resp.json()['max_duration_ms'] == 300

    @pytest.mark.integration
    def test_stats_queues_from_snapshot(self):
        """队列深度复用 analytics 快照，mock 掉 Redis 依赖"""
        fake_snapshot = {'default': {'size': 5}, 'parse': {'size': 0}}
        with patch('apps.analytics.services.realtime_service.get_queue_depth_snapshot',
                   return_value=fake_snapshot):
            resp = self.client.get(STATS_URL, **self.admin_a_headers)
        assert resp.json()['queues'] == fake_snapshot

    @pytest.mark.integration
    def test_stats_when_snapshot_fails_then_queues_empty(self):
        """Redis 不可用时 queues 降级为空 dict，接口仍返回 200"""
        with patch('apps.analytics.services.realtime_service.get_queue_depth_snapshot',
                   side_effect=Exception('redis down')):
            resp = self.client.get(STATS_URL, **self.admin_a_headers)
        assert resp.status_code == 200
        assert resp.json()['queues'] == {}


class TestTaskRetryViewAPI(SystemAPITestBase):
    """失败任务重试接口测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_retry_when_invalid_task_id_then_400(self):
        resp = self.client.post(_retry_url('not-a-uuid'), data={},
                                content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_retry_when_task_not_found_then_404(self):
        tid = str(uuid.uuid4())
        resp = self.client.post(_retry_url(tid), data={},
                                content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_retry_when_not_failure_then_409(self):
        row = _make_task_log(str(uuid.uuid4()), status='success')
        resp = self.client.post(_retry_url(row.task_id), data={},
                                content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_retry_when_failure_then_202_with_send_args(self):
        """失败任务重试：以原 task_name/args/kwargs/queue 重新派发"""
        row = _make_task_log(str(uuid.uuid4()), status='failure', queue='analytics',
                             args=['x'], kwargs={'k': 1})
        fake_result = Mock(id='new-task-id')
        with patch('apps.system.views.current_app.send_task', return_value=fake_result) as mock_send:
            resp = self.client.post(_retry_url(row.task_id), data={},
                                    content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 202
        body = resp.json()
        assert body['new_task_id'] == 'new-task-id'
        assert body['old_task_id'] == row.task_id
        mock_send.assert_called_once_with(
            'apps.qa.tasks.run', args=['x'], kwargs={'k': 1}, queue='analytics')

    @pytest.mark.integration
    def test_retry_when_send_fails_then_500(self):
        """派发异常返回 500，且错误被记录"""
        row = _make_task_log(str(uuid.uuid4()), status='failure')
        with patch('apps.system.views.current_app.send_task',
                   side_effect=Exception('broker down')):
            resp = self.client.post(_retry_url(row.task_id), data={},
                                    content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 500


class TestCleanupTaskLogs(SystemAPITestBase):
    """每日清理任务测试：日志量受控约束的落地点"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_cleanup_deletes_only_expired(self):
        """仅删除超过保留期的日志，近期日志保留"""
        now = timezone.now()
        _make_task_log('old-1', created_at=now - timedelta(days=40))
        _make_task_log('old-2', created_at=now - timedelta(days=35))
        _make_task_log('new-1', created_at=now - timedelta(days=1))
        result = cleanup_task_logs()
        assert result['deleted'] == 2
        assert not CeleryTaskLog.objects.filter(task_id='old-1').exists()
        assert CeleryTaskLog.objects.filter(task_id='new-1').exists()

    @pytest.mark.integration
    def test_cleanup_uses_configurable_retention(self):
        """保留天数可配：SystemConfig.TASK_LOG_RETENTION_DAYS 生效"""
        SystemConfig.objects.create(
            key='TASK_LOG_RETENTION_DAYS', value='7', value_type='int',
            label='任务日志保留天数', category='analytics')
        now = timezone.now()
        _make_task_log('old-1', created_at=now - timedelta(days=10))
        _make_task_log('new-1', created_at=now - timedelta(days=3))
        cleanup_task_logs()
        assert not CeleryTaskLog.objects.filter(task_id='old-1').exists()
        assert CeleryTaskLog.objects.filter(task_id='new-1').exists()
