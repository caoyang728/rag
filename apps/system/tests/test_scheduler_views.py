"""
apps.system 定时任务调度 API 集成测试 —— 任务列表 / 工单审批闭环

覆盖范围（需真实 DB，验证端到端契约）：
- SchedulerTaskView：权限（匿名 401 / 普通用户 403 / 超管 200）、返回任务清单
- PUT /configs/<SCHEDULE_*>/：非法 cron 400、合法变更创建工单（高风险）
- 工单列表按 config_key 过滤（定时任务页只展示调度类工单）
- SystemConfig 列表不暴露调度类配置（由独立定时任务页管理）
"""
import json

import pytest

from apps.system.models import SystemConfig, ConfigChangeTicket
from apps.system.scheduler_registry import (
    schedule_key, serialize_schedule,
)
from apps.system.tests.test_views import SystemAPITestBase


class TestSchedulerTaskView(SystemAPITestBase):
    """定时任务清单 API 的权限与返回结构"""

    @pytest.mark.integration
    def test_list_when_anonymous_then_401(self):
        resp = self.client.get('/api/v1/system/scheduler/tasks/')
        assert resp.status_code == 401

    @pytest.mark.integration
    def test_list_when_normal_user_then_403(self):
        resp = self.client.get(
            '/api/v1/system/scheduler/tasks/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_list_when_super_admin_then_returns_tasks(self):
        resp = self.client.get(
            '/api/v1/system/scheduler/tasks/', **self.admin_a_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['total'] == len(data['tasks'])
        assert data['total'] > 0
        first = data['tasks'][0]
        # 返回任务所需的全部渲染字段
        for field in ('name', 'key', 'task', 'label', 'description',
                      'cron', 'cron_fields', 'enabled', 'risk_level',
                      'pending_ticket_count'):
            assert field in first
        assert first['key'].startswith('SCHEDULE_')
        assert first['risk_level'] == 'high'
        # cron_fields 拆为 5 段
        assert set(first['cron_fields'].keys()) == {
            'minute', 'hour', 'day_of_month', 'month', 'day_of_week'}


class TestSchedulerConfigPUT(SystemAPITestBase):
    """调度类配置（SCHEDULE_*）PUT 创建工单的校验与审批闭环"""

    def _make_schedule_cfg(self, cron='0 2 * * *', enabled=True):
        key = schedule_key('test-put-job')
        SystemConfig.objects.create(
            key=key, value=serialize_schedule(cron, enabled), value_type='json',
            label='测试任务', description='测试', category='schedule',
            is_secret=False, is_readonly=False, risk_level='high')
        return key

    @pytest.mark.integration
    def test_put_when_invalid_cron_then_400(self):
        key = self._make_schedule_cfg()
        resp = self.client.put(
            f'/api/v1/system/configs/{key}/',
            data=json.dumps({'value': serialize_schedule('0 99 * * *', True),
                             'reason': 'test'}),
            content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'hour' in resp.json()['detail']  # 明确提示非法字段

    @pytest.mark.integration
    def test_put_when_valid_change_then_creates_high_risk_ticket(self):
        key = self._make_schedule_cfg()
        resp = self.client.put(
            f'/api/v1/system/configs/{key}/',
            data=json.dumps({'value': serialize_schedule('30 2 * * *', True),
                             'reason': '错峰执行'}),
            content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 201
        ticket = ConfigChangeTicket.objects.get(config_key=key)
        assert ticket.status == 'pending'
        assert ticket.risk_level == 'high'
        # 变更摘要含 cron 新旧值，便于审批人识别
        summary = json.loads(ticket.change_summary)
        assert summary['schedule']['cron']['old'] == '0 2 * * *'
        assert summary['schedule']['cron']['new'] == '30 2 * * *'
        # 工单未通过前配置值不变
        cfg = SystemConfig.objects.get(key=key)
        assert cfg.value == serialize_schedule('0 2 * * *', True)

    @pytest.mark.integration
    def test_approve_flow_when_high_risk_then_apply_after_super_admin_review(self):
        """调度类高风险工单：审核 → 超管复核 → 写库生效（beat 热更新读取新值）"""
        key = self._make_schedule_cfg()
        # 管理员 A 创建工单
        resp = self.client.put(
            f'/api/v1/system/configs/{key}/',
            data=json.dumps({'value': serialize_schedule('30 2 * * *', True),
                             'reason': '错峰执行'}),
            content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 201
        ticket = ConfigChangeTicket.objects.get(config_key=key)
        # 审核通过（管理员 B）：高风险进入待复核，配置不变
        resp = self.client.post(
            f'/api/v1/system/config-tickets/{ticket.id}/approve/',
            data=json.dumps({'comment': 'ok'}), content_type='application/json',
            **self.admin_b_headers)
        assert resp.status_code == 200
        ticket.refresh_from_db()
        assert ticket.status == 'first_approved'
        cfg = SystemConfig.objects.get(key=key)
        assert cfg.value == serialize_schedule('0 2 * * *', True)
        # 超管复核通过（管理员 C）：写库生效
        resp = self.client.post(
            f'/api/v1/system/config-tickets/{ticket.id}/approve/',
            data=json.dumps({'comment': 'ok'}), content_type='application/json',
            **self.admin_c_headers)
        assert resp.status_code == 200
        ticket.refresh_from_db()
        assert ticket.status == 'approved'
        cfg = SystemConfig.objects.get(key=key)
        assert cfg.value == serialize_schedule('30 2 * * *', True)

    @pytest.mark.integration
    def test_put_when_enable_disable_change_then_summary_has_enabled(self):
        key = self._make_schedule_cfg(enabled=True)
        resp = self.client.put(
            f'/api/v1/system/configs/{key}/',
            data=json.dumps({'value': serialize_schedule('0 2 * * *', False),
                             'reason': '暂停评估任务控制成本'}),
            content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 201
        ticket = ConfigChangeTicket.objects.get(config_key=key)
        summary = json.loads(ticket.change_summary)
        assert summary['schedule']['enabled']['old'] is True
        assert summary['schedule']['enabled']['new'] is False


class TestSchedulerTicketList(SystemAPITestBase):
    """工单列表按 config_key 过滤（定时任务页专用）"""

    @pytest.mark.integration
    def test_list_filtered_by_config_key(self):
        key = schedule_key('filter-job')
        SystemConfig.objects.create(
            key=key, value=serialize_schedule('0 2 * * *', True), value_type='json',
            label='过滤测试', category='schedule', risk_level='high')
        ConfigChangeTicket.objects.create(
            config_key=key, config_label='过滤测试',
            old_value=serialize_schedule('0 2 * * *', True),
            new_value=serialize_schedule('30 2 * * *', True),
            risk_level='high', reason='test', status='pending',
            creator=self.super_admin_b)
        # 另一条非调度类工单，不应被过滤出来
        ConfigChangeTicket.objects.create(
            config_key='LLM_TIMEOUT', config_label='LLM 超时',
            old_value='60', new_value='90', risk_level='normal',
            reason='test', status='pending', creator=self.super_admin_b)

        # 以超管 A 查询（A 不是创建人，能出现在待审核列表）
        resp = self.client.get(
            f'/api/v1/system/config-tickets/?status=pending&config_key={key}',
            **self.admin_a_headers)
        assert resp.status_code == 200
        tickets = resp.json()['tickets']
        assert len(tickets) == 1
        assert tickets[0]['config_key'] == key


class TestSystemConfigListExcludesSchedules(SystemAPITestBase):
    """通用配置列表不暴露调度类配置（由独立定时任务页管理）"""

    @pytest.mark.integration
    def test_list_excludes_schedule_keys(self):
        key = schedule_key('hidden-job')
        SystemConfig.objects.create(
            key=key, value=serialize_schedule('0 2 * * *', True), value_type='json',
            label='隐藏任务', category='schedule', risk_level='high')
        SystemConfig.objects.create(
            key='LLM_TIMEOUT', value='60', value_type='int',
            label='LLM 超时', category='llm', risk_level='normal')

        resp = self.client.get(
            '/api/v1/system/configs/', **self.admin_a_headers)
        assert resp.status_code == 200
        groups = resp.json()['groups']
        all_keys = [c['key'] for items in groups.values() for c in items]
        assert key not in all_keys
        assert 'LLM_TIMEOUT' in all_keys
