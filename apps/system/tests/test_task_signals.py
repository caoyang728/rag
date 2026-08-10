"""
apps.system.task_signals 单元测试 —— Celery 信号统一写入 CeleryTaskLog

直接调用信号处理函数（不走真实 Celery 信号派发），覆盖：
- 记录过滤：_SKIP_TASKS 中的高频维护任务不落库
- 幂等写入：同 task_id 重复 prerun 不产生重复行
- 状态流转：prerun → started，postrun → success + 耗时，failure → error_message
- 异常隔离：信号内写库失败不冒泡（不阻塞任务主流程，这是验收硬约束）
"""
from unittest.mock import patch

import pytest

from apps.system.models import CeleryTaskLog
from apps.system.task_signals import (
    _record_finished,
    _record_started,
    _is_recordable,
    on_task_failure,
    on_task_postrun,
    on_task_prerun,
    on_task_revoked,
)


class _FakeRequest:
    """模拟信号 sender.request：携带队列路由与重试次数

    revoked 处理器从 request 读取 id/task 属性，故需一并提供。
    """

    def __init__(self, routing_key='default', retries=0,
                 task_id='fake-task-id', task='apps.qa.tasks.run'):
        self.delivery_info = {'routing_key': routing_key}
        self.retries = retries
        self.id = task_id
        self.task = task


class _FakeSender:
    """模拟信号 sender（Celery Task 对象）"""

    def __init__(self, routing_key='default', retries=0):
        self.request = _FakeRequest(routing_key, retries)
        self.id = 'fake-task-id'
        self.task = 'apps.qa.tasks.run'


@pytest.mark.django_db
class TestTaskLogRecord:
    """信号处理器落库行为测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        # 每个用例独立的 sender，保证 task_id 不冲突
        self.sender = _FakeSender(routing_key='analytics', retries=1)

    @pytest.mark.integration
    def test_prerun_creates_started_row(self):
        """prerun 落库：status=started，队列/重试次数取自上送信息"""
        on_task_prerun(self.sender, 'tid-1', 'apps.qa.tasks.run', ['a'], {'k': 1})
        row = CeleryTaskLog.objects.get(task_id='tid-1')
        assert row.status == 'started'
        assert row.queue == 'analytics'
        assert row.retry_count == 1
        assert row.args == ['a']
        assert row.kwargs == {'k': 1}
        assert row.started_at is not None

    @pytest.mark.integration
    def test_prerun_skips_high_frequency_task(self):
        """_SKIP_TASKS 高频维护任务不落库，避免淹没看板"""
        on_task_prerun(self.sender, 'tid-2', 'analytics.update_queue_depth', [], {})
        assert not CeleryTaskLog.objects.filter(task_id='tid-2').exists()

    @pytest.mark.integration
    def test_prerun_idempotent_when_same_task_id(self):
        """同 task_id 重复 prerun 只保留一行（update_or_create 幂等）"""
        on_task_prerun(self.sender, 'tid-3', 'apps.qa.tasks.run', [], {})
        on_task_prerun(self.sender, 'tid-3', 'apps.qa.tasks.run', [], {})
        assert CeleryTaskLog.objects.filter(task_id='tid-3').count() == 1

    @pytest.mark.integration
    def test_postrun_marks_success_and_duration(self):
        """postrun 更新：status=success，finished_at 落库，耗时 > 0"""
        on_task_prerun(self.sender, 'tid-4', 'apps.qa.tasks.run', [], {})
        on_task_postrun(self.sender, 'tid-4', 'apps.qa.tasks.run', {'ok': True})
        row = CeleryTaskLog.objects.get(task_id='tid-4')
        assert row.status == 'success'
        assert row.finished_at is not None
        assert row.duration_ms > 0
        assert 'ok' in row.result

    @pytest.mark.integration
    def test_failure_records_error_message(self):
        """failure 更新：status=failure，error_message 含异常与堆栈"""
        on_task_prerun(self.sender, 'tid-5', 'apps.qa.tasks.run', [], {})
        on_task_failure(self.sender, 'tid-5', 'apps.qa.tasks.run',
                        ValueError('boom'), 'Traceback: boom')
        row = CeleryTaskLog.objects.get(task_id='tid-5')
        assert row.status == 'failure'
        assert 'boom' in row.error_message

    @pytest.mark.integration
    def test_revoked_marks_revoked(self):
        """revoked 更新：status=revoked"""
        on_task_prerun(self.sender, 'tid-6', 'apps.qa.tasks.run', [], {})
        fake_req = _FakeRequest('default', task_id='tid-6', task='apps.qa.tasks.run')
        on_task_revoked(None, fake_req, terminated=False, signum=None, expired=False)
        row = CeleryTaskLog.objects.get(task_id='tid-6')
        assert row.status == 'revoked'

    @pytest.mark.integration
    def test_signal_write_error_not_raised(self):
        """异常隔离：信号内写库抛错被吞掉，不向上冒泡（不阻塞任务主流程）"""
        with patch('apps.system.task_signals.CeleryTaskLog') as mock_cls:
            mock_cls.objects.update_or_create.side_effect = Exception('db down')
            # 不应抛出任何异常
            on_task_prerun(self.sender, 'tid-7', 'apps.qa.tasks.run', [], {})

    @pytest.mark.integration
    def test_postrun_without_started_row_is_noop(self):
        """postrun 但无对应 started 行时静默跳过（不抛错不建行）"""
        on_task_postrun(self.sender, 'tid-8', 'apps.qa.tasks.run', {})
        assert not CeleryTaskLog.objects.filter(task_id='tid-8').exists()

    @pytest.mark.integration
    def test_prerun_when_sender_request_errors_then_defaults(self):
        """sender.request 访问异常：队列回退 default、重试次数回退 0，仍正常落库"""
        class _BadRequest:
            @property
            def delivery_info(self):
                raise RuntimeError('boom')

        class _BadSender:
            request = _BadRequest()

        on_task_prerun(_BadSender(), 'tid-11', 'apps.qa.tasks.run', [], {})
        row = CeleryTaskLog.objects.get(task_id='tid-11')
        assert row.status == 'started'
        assert row.queue == 'default'
        assert row.retry_count == 0

    @pytest.mark.integration
    def test_prerun_when_args_kwargs_unsafe_then_emptied(self):
        """args/kwargs 不可序列化（循环引用）：回退空值，不阻断落库"""
        loop = []
        loop.append(loop)
        on_task_prerun(self.sender, 'tid-12', 'apps.qa.tasks.run', loop, {'k': loop})
        row = CeleryTaskLog.objects.get(task_id='tid-12')
        assert row.args == []
        assert row.kwargs == {}

    @pytest.mark.integration
    def test_postrun_when_skip_task_then_noop(self):
        """postrun 收到高频维护任务：直接跳过，不写库"""
        on_task_postrun(self.sender, 'tid-13', 'analytics.update_queue_depth', {})
        assert not CeleryTaskLog.objects.filter(task_id='tid-13').exists()

    @pytest.mark.integration
    def test_postrun_when_retval_unsafe_then_empty_result(self):
        """retval 不可序列化（循环引用）：result 落空串，状态仍为 success"""
        loop = []
        loop.append(loop)
        on_task_prerun(self.sender, 'tid-14', 'apps.qa.tasks.run', [], {})
        on_task_postrun(self.sender, 'tid-14', 'apps.qa.tasks.run', loop)
        row = CeleryTaskLog.objects.get(task_id='tid-14')
        assert row.status == 'success'
        assert row.result == ''

    @pytest.mark.integration
    def test_postrun_when_db_error_then_not_raised(self):
        """postrun 落库失败被吞掉，不向上冒泡（不阻塞任务主流程）"""
        with patch('apps.system.task_signals._record_finished',
                   side_effect=Exception('db down')):
            on_task_postrun(self.sender, 'tid-15', 'apps.qa.tasks.run', {})

    @pytest.mark.integration
    def test_failure_when_skip_task_then_noop(self):
        """failure 收到高频维护任务：直接跳过，不写库"""
        on_task_failure(self.sender, 'tid-16', 'analytics.update_queue_depth',
                        ValueError('x'), None)
        assert not CeleryTaskLog.objects.filter(task_id='tid-16').exists()

    @pytest.mark.integration
    def test_failure_when_db_error_then_not_raised(self):
        """failure 落库失败被吞掉，不向上冒泡（不阻塞任务主流程）"""
        with patch('apps.system.task_signals._record_finished',
                   side_effect=Exception('db down')):
            on_task_failure(self.sender, 'tid-17', 'apps.qa.tasks.run',
                            ValueError('x'), 'tb')

    @pytest.mark.integration
    def test_revoked_when_skip_task_then_noop(self):
        """revoked 收到高频维护任务：直接跳过，不写库"""
        fake_req = _FakeRequest('default', task_id='tid-18',
                                task='analytics.update_queue_depth')
        on_task_revoked(None, fake_req, terminated=False, signum=None, expired=False)
        assert not CeleryTaskLog.objects.filter(task_id='tid-18').exists()

    @pytest.mark.integration
    def test_revoked_when_no_task_id_then_noop(self):
        """revoked 请求缺 task_id：直接跳过，不写库"""
        fake_req = _FakeRequest('default', task_id=None, task='apps.qa.tasks.run')
        on_task_revoked(None, fake_req, terminated=False, signum=None, expired=False)
        assert not CeleryTaskLog.objects.filter(task_id='tid-19').exists()

    @pytest.mark.integration
    def test_revoked_when_db_error_then_not_raised(self):
        """revoked 落库失败被吞掉，不向上冒泡（不阻塞任务主流程）"""
        fake_req = _FakeRequest('default', task_id='tid-20', task='apps.qa.tasks.run')
        with patch('apps.system.task_signals._record_finished',
                   side_effect=Exception('db down')):
            on_task_revoked(None, fake_req, terminated=False, signum=None, expired=False)


class TestTaskLogRecordLogic:
    """纯逻辑测试：过滤判定与底层写入辅助函数"""

    @pytest.mark.unit
    def test_is_recordable_filters_skip_list(self):
        assert _is_recordable('apps.qa.tasks.run')
        assert not _is_recordable('analytics.update_queue_depth')
        assert not _is_recordable('security.expire_ip_blacklist')
        assert not _is_recordable('')

    @pytest.mark.django_db
    @pytest.mark.integration
    def test_record_started_update_or_create(self):
        """_record_started 重复调用幂等：同一 task_id 只存在一行"""
        _record_started('tid-9', 't', 'default', [], {}, 0)
        _record_started('tid-9', 't', 'default', [], {}, 0)
        assert CeleryTaskLog.objects.filter(task_id='tid-9').count() == 1

    @pytest.mark.django_db
    @pytest.mark.integration
    def test_record_finished_updates_existing_row(self):
        """_record_finished 仅更新已存在行，不新建行"""
        _record_started('tid-10', 't', 'default', [], {}, 0)
        _record_finished('tid-10', 'failure', error_message='err')
        row = CeleryTaskLog.objects.get(task_id='tid-10')
        assert row.status == 'failure'
        assert row.error_message == 'err'
        assert row.finished_at is not None
