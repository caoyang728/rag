"""
apps.system.views 补充测试 —— 覆盖 test_views*.py 未触及的异常/边界/权限分支

覆盖范围（行号对应 apps/system/views.py 当前版本）：
- HealthView：DB 组件异常（196-198）
- SystemConfigView：列表 EMBEDDING/RERANK 未知值回显（264, 267-270）、PUT 无 key 400（289）、
  int 非数字 400（399）、bool 原生布尔（408）、审计失败不阻断（454-456）、
  变更摘要异常（376-378）、_write_audit 全分支（501-520）、options 解析失败（597-598）、
  _get_llm_model_options（531-541）、_get_llm_model_options_map 异常（557-559）、
  _get_business_tables 三分支（567-591）
- LLMModelViewSet：retrieve 无权限（715）、校验错误全分支（733/745/758/761-762/768）、
  name-only 直改成功与保存失败（834-843）、model_type/model_name 变更工单（860/864）、
  无字段变更 400（871）、update/destroy 审计失败（926-927/1009-1010）、
  缓存失效失败（1044-1046）、_write_audit 异常（1070-1072）
- TicketViewSet：列表无权限（1292）、ticket_type 过滤（1313）、数字搜索（1327-1328）、
  page 非法 400（1367-1368）、POST 无权限（1410）、非法 ticket_type（1419）、
  new_value 缺失（1464）、schedule cron 非法（1475-1479）、模型工单全分支（1525-1619）
- _TicketOperationBase：GET 405（1632）、配置缺失生效失败（1656/1855）、
  缓存失效失败（1675-1677/1763-1764）、依赖回滚（1699-1704/1738-1739）、
  审批链走完 409（1798）、model 生效失败返回（1859）、驳回无权限（1879）、
  复核驳回权限（1905-1907）
- _TicketMixin._check_model_dependency 模型不存在（1137）、_write_audit 异常（1125-1127）
- TaskLogView page 非法参数降级（2018-2019）、TaskRetryView 无权限（2111）

Mock 策略与既有测试一致：外部依赖（Redis/psycopg/审计写库/缓存失效）用
unittest.mock 隔离；审批状态机走真实 DB 保证契约正确。
"""
import json
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from apps.system.models import SystemConfig, LLMModel
from apps.system.scheduler_registry import schedule_key, serialize_schedule
from apps.system.views import (
    ApproveTicketView,
    LLMModelViewSet,
    SystemConfigView,
    _TicketMixin,
    _build_system_chain,
)
from apps.users.models import (
    Permission, Role, RolePermissionRel, UserRoleRel, GrantStatus,
    TicketList, TicketStatus, TicketBizType,
    TicketConfigDetail, TicketModelDetail,
)
from apps.system.tests.test_views import SystemAPITestBase


def _post_ticket_action(client, ticket_id, action, headers, comment=''):
    """POST 统一工单动作接口（approve/reject/withdraw）"""
    return client.post(
        f'/api/v1/system/tickets/{ticket_id}/{action}/',
        data=json.dumps({'comment': comment}),
        content_type='application/json',
        **headers)


def _create_maintainer(username='maintainer'):
    """创建"维护管理员"：持有 system.config.write 权限但非超管

    用于覆盖高风险项复核阶段"非超管持写权限"被 403 拦截的分支
    （approve/reject 的 SUPER_ADMIN 节点权限校验）。
    """
    from apps.users.models import User
    user = User.objects.create_user(username=username, email=f'{username}@test.com',
                                    password='testpass123')
    role, _ = Role.objects.get_or_create(role_key='maintainer',
                                         defaults={'name': '维护管理员', 'is_builtin': False})
    perm, _ = Permission.objects.get_or_create(
        permission_key='system.config.write', permission_name='配置写', module='system')
    RolePermissionRel.objects.get_or_create(role=role, permission=perm)
    UserRoleRel.objects.get_or_create(
        user=user, role=role, defaults={'status': GrantStatus.ACTIVE})
    return user


class _TicketActionMixin:
    """工单动作请求辅助"""

    def _approve(self, ticket_id, headers, comment='ok'):
        return _post_ticket_action(self.client, ticket_id, 'approve', headers, comment)

    def _reject(self, ticket_id, headers, comment='no'):
        return _post_ticket_action(self.client, ticket_id, 'reject', headers, comment)


class TestHealthViewDbDown(SystemAPITestBase):
    """HealthView：DB 组件异常分支"""

    @pytest.mark.integration
    def test_health_when_db_down_then_ok_false(self):
        fake_llm = MagicMock()
        fake_llm.provider = 'deepseek'
        with patch('apps.system.views.connection') as mock_conn, \
             patch('redis.Redis.ping', return_value=True), \
             patch('apps.llm.factory.get_llm', return_value=fake_llm):
            mock_conn.cursor.side_effect = RuntimeError('db down')
            resp = self.client.get('/api/v1/system/health/')
        assert resp.status_code == 200
        data = resp.json()
        # DB 失败时整体 ok=False，其余组件不受影响
        assert data['ok'] is False
        assert data['components']['db']['ok'] is False
        assert 'error' in data['components']['db']
        assert data['components']['redis']['ok'] is True
        assert data['components']['llm']['ok'] is True


class TestSystemConfigViewEdge(SystemAPITestBase):
    """SystemConfig 列表/PUT 异常与边界分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        self.cfg = SystemConfig.objects.create(
            key='LLM_TIMEOUT', value='60', value_type='int',
            label='LLM 超时', category='llm', risk_level='normal')
        self.bool_cfg = SystemConfig.objects.create(
            key='ANALYTICS_ENABLED', value='false', value_type='bool',
            label='评估开关', category='analytics')
        # 当前值不在模型列表中的历史遗留项
        SystemConfig.objects.create(
            key='EMBEDDING_MODEL', value='legacy-emb', value_type='string',
            label='Embedding 模型', category='embedding')
        SystemConfig.objects.create(
            key='RERANK_MODEL', value='legacy-rerank', value_type='string',
            label='Rerank 模型', category='retrieval')
        # options 存储非法 JSON 的脏数据
        SystemConfig.objects.create(
            key='AGENT_MODE', value='docker', value_type='string',
            label='Agent 模式', category='agent', options='not-json')

    def _put(self, key, value, reason='r', headers=None):
        return self.client.put(
            f'/api/v1/system/configs/{key}/',
            data=json.dumps({'value': value, 'reason': reason}),
            content_type='application/json',
            **headers or self.admin_a_headers)

    def _config_by_key(self, resp):
        return {item['key']: item for group in resp.json()['groups'].values() for item in group}

    @pytest.mark.integration
    def test_list_embedding_and_rerank_unknown_value_appended(self):
        """EMBEDDING/RERANK 当前值不在模型列表中时作为回显项插入"""
        resp = self.client.get('/api/v1/system/configs/', **self.normal_headers)
        assert resp.status_code == 200
        by_key = self._config_by_key(resp)
        assert by_key['EMBEDDING_MODEL']['options'][0]['value'] == 'legacy-emb'
        assert '未在模型管理中' in by_key['EMBEDDING_MODEL']['options'][0]['label']
        assert by_key['RERANK_MODEL']['options'][0]['value'] == 'legacy-rerank'
        assert '未在模型管理中' in by_key['RERANK_MODEL']['options'][0]['label']

    @pytest.mark.integration
    def test_put_without_key_400(self):
        """PUT 不带 key 应 400"""
        resp = self.client.put(
            '/api/v1/system/configs/',
            data=json.dumps({'value': '120', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'key required' in resp.json()['detail']

    @pytest.mark.integration
    def test_put_int_non_numeric_400(self):
        """int 类型配置传入非数字应 400"""
        resp = self._put('LLM_TIMEOUT', 'abc')
        assert resp.status_code == 400
        assert '整数' in resp.json()['detail']

    @pytest.mark.integration
    def test_put_bool_native_bool_normalized(self):
        """bool 配置传入原生布尔值：JSON true 规范化为 'true'"""
        resp = self._put('ANALYTICS_ENABLED', True)
        assert resp.status_code == 201
        assert resp.json()['new_value'] == 'true'

    @pytest.mark.integration
    def test_put_audit_failure_does_not_block(self):
        """审计日志写失败不阻断工单创建"""
        with patch('apps.audit.models.AuditLog.objects.create',
                   side_effect=RuntimeError('audit down')):
            resp = self._put('LLM_TIMEOUT', '120')
        assert resp.status_code == 201
        assert resp.json()['new_value'] == '120'

    @pytest.mark.integration
    def test_list_bad_options_parsed_empty(self):
        """options 非法 JSON 时解析为空数组，不 500"""
        resp = self.client.get('/api/v1/system/configs/', **self.normal_headers)
        by_key = self._config_by_key(resp)
        assert by_key['AGENT_MODE']['options'] == []

    @pytest.mark.unit
    def test_compute_change_summary_parse_error_returns_empty(self):
        """多值类配置拆分异常时返回空串（不抛 500）"""
        view = SystemConfigView()
        assert view._compute_change_summary('BUSINESS_DB_TABLES', ['a'], 'a,b') == ''
        assert view._compute_change_summary('BUSINESS_DB_TABLES', 'a', {'b': 1}) == ''

    @pytest.mark.integration
    def test_write_audit_success_and_failure(self):
        """SystemConfigView._write_audit：成功落库 / 失败仅告警"""
        from apps.audit.models import AuditLog
        request = MagicMock()
        request.user = self.super_admin_a
        request.method = 'PUT'
        request.path = '/api/v1/system/configs/LLM_TIMEOUT/'
        request.META = {'REMOTE_ADDR': '127.0.0.1', 'HTTP_USER_AGENT': 'test-agent'}
        view = SystemConfigView()
        view._write_audit(request, self.cfg, '60', '120')
        assert AuditLog.objects.filter(target_id='LLM_TIMEOUT', action='update_system_config').exists()
        # 审计失败仅告警
        with patch('apps.audit.models.AuditLog.objects.create',
                   side_effect=RuntimeError('audit down')):
            view._write_audit(request, self.cfg, '60', '120')

    @pytest.mark.integration
    def test_get_llm_model_options_full(self):
        """_get_llm_model_options：返回列表 / 当前值回显 / 异常返回空"""
        LLMModel.objects.create(
            name='DeepSeek 对话', provider='deepseek', model_type='llm',
            model_name='deepseek-chat', is_active=True)
        view = SystemConfigView()
        opts = view._get_llm_model_options('llm')
        assert any(o['value'] == 'deepseek-chat' for o in opts)
        # 当前值不在列表中时插入回显项
        opts = view._get_llm_model_options('llm', current_value='ghost-model')
        assert opts[0]['value'] == 'ghost-model'
        assert '未在模型管理中' in opts[0]['label']
        # DB 异常时降级为空列表
        with patch('apps.system.models.LLMModel') as mock_cls:
            mock_cls.objects.filter.side_effect = RuntimeError('db down')
            assert view._get_llm_model_options('llm') == []

    @pytest.mark.unit
    def test_get_llm_model_options_map_error_returns_empty_groups(self):
        """_get_llm_model_options_map 异常时返回三类型空分组"""
        with patch('apps.system.models.LLMModel') as mock_cls:
            mock_cls.objects.filter.side_effect = RuntimeError('db down')
            result = SystemConfigView()._get_llm_model_options_map()
        assert result == {'llm': [], 'embedding': [], 'rerank': []}

    @pytest.mark.integration
    def test_get_business_tables_via_django_connection(self, settings):
        """未配置 BUSINESS_DB_DSN 时用 Django 默认连接读取 public schema 表名"""
        settings.BUSINESS_DB_DSN = ''
        result = SystemConfigView()._get_business_tables()
        # 测试库中存在真实业务表（users 等），列表非空
        assert len(result) > 0
        assert all('value' in t and 'label' in t for t in result)

    @pytest.mark.unit
    def test_get_business_tables_via_dsn(self, settings, monkeypatch):
        """配置 BUSINESS_DB_DSN 时直连业务库读取表名"""
        settings.BUSINESS_DB_DSN = 'postgres://u:p@biz-host/biz_db'
        fake_cursor = MagicMock()
        fake_cursor.fetchall.return_value = [('tab_a',), ('tab_b',)]
        fake_conn = MagicMock()
        fake_conn.cursor.return_value = fake_cursor
        fake_psycopg = SimpleNamespace(connect=MagicMock(return_value=fake_conn))
        monkeypatch.setitem(sys.modules, 'psycopg', fake_psycopg)
        result = SystemConfigView()._get_business_tables()
        fake_psycopg.connect.assert_called_once_with('postgres://u:p@biz-host/biz_db')
        assert result == [{'value': 'tab_a', 'label': 'tab_a'},
                          {'value': 'tab_b', 'label': 'tab_b'}]

    @pytest.mark.unit
    def test_get_business_tables_connection_error_returns_empty(self, settings, monkeypatch):
        """业务库连接失败时返回空列表，前端降级为自由输入"""
        settings.BUSINESS_DB_DSN = 'postgres://u:p@biz-host/biz_db'
        fake_psycopg = SimpleNamespace(
            connect=MagicMock(side_effect=Exception('conn refused')))
        monkeypatch.setitem(sys.modules, 'psycopg', fake_psycopg)
        result = SystemConfigView()._get_business_tables()
        assert result == []


class TestLLMModelViewSetEdge(SystemAPITestBase):
    """LLMModelViewSet 校验边界 / 审计与缓存失败分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        self.model = LLMModel.objects.create(
            name='DeepSeek 对话', provider='deepseek', model_type='llm',
            base_url='https://api.deepseek.com', model_name='deepseek-chat',
            timeout=120, is_active=True)

    def _put(self, data):
        return self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps(data), content_type='application/json',
            **self.admin_a_headers)

    @pytest.mark.integration
    def test_retrieve_no_permission_403(self):
        """retrieve 无 system.config.read 权限应 403"""
        resp = self.client.get(
            f'/api/v1/system/llm-models/{self.model.id}/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_missing_required_fields_400(self):
        """create 缺必填字段（name）应 400 并逐字段报错"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({'provider': 'p', 'model_type': 'llm', 'model_name': 'm'}),
            content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'name' in resp.json()['errors']

    @pytest.mark.integration
    def test_create_base_url_too_long_400(self):
        """base_url 超过 1000 字符应 400"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({'name': 'x', 'provider': 'p', 'model_type': 'llm',
                             'base_url': 'https://a.com/' + 'a' * 1000, 'model_name': 'm'}),
            content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'base_url' in resp.json()['errors']

    @pytest.mark.integration
    def test_create_timeout_too_large_400(self):
        """timeout 超过 86400 秒应 400"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({'name': 'x', 'provider': 'p', 'model_type': 'llm',
                             'model_name': 'm', 'timeout': 90000}),
            content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'timeout' in resp.json()['errors']

    @pytest.mark.integration
    def test_create_timeout_non_int_400(self):
        """timeout 非整数应 400"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({'name': 'x', 'provider': 'p', 'model_type': 'llm',
                             'model_name': 'm', 'timeout': 'abc'}),
            content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'timeout' in resp.json()['errors']

    @pytest.mark.integration
    def test_create_name_too_long_400(self):
        """字符串字段超过 255 字符应 400"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({'name': 'n' * 256, 'provider': 'p', 'model_type': 'llm',
                             'model_name': 'm'}),
            content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'name' in resp.json()['errors']

    @pytest.mark.integration
    def test_update_name_only_direct_save(self):
        """仅改 name 且规范化后无其他字段时直接生效（200）"""
        with patch.object(LLMModelViewSet, '_validate_model_payload',
                          return_value=(None, {'name': 'Renamed'})):
            resp = self.client.put(
                f'/api/v1/system/llm-models/{self.model.id}/',
                data=json.dumps({'name': 'Renamed'}), content_type='application/json',
                **self.admin_a_headers)
        assert resp.status_code == 200
        self.model.refresh_from_db()
        assert self.model.name == 'Renamed'

    @pytest.mark.integration
    def test_update_name_save_failure_400(self):
        """改名直改时 save 失败（重名冲突）返回 400 而非 500"""
        with patch.object(LLMModelViewSet, '_validate_model_payload',
                          return_value=(None, {'name': 'Dup'})), \
             patch.object(LLMModel, 'save', side_effect=Exception('unique violated')):
            resp = self.client.put(
                f'/api/v1/system/llm-models/{self.model.id}/',
                data=json.dumps({'name': 'Dup'}), content_type='application/json',
                **self.admin_a_headers)
        assert resp.status_code == 400
        assert '已存在' in resp.json()['detail']

    @pytest.mark.integration
    def test_update_model_type_creates_ticket(self):
        """修改 model_type 纳入变更字段并创建工单"""
        resp = self._put({'model_type': 'embedding', 'reason': 'r'})
        assert resp.status_code == 202
        ticket = TicketList.objects.get(id=resp.json()['ticket_id'])
        assert 'model_type' in ticket.changed_fields

    @pytest.mark.integration
    def test_update_model_name_creates_ticket(self):
        """修改 model_name 纳入变更字段并创建工单"""
        resp = self._put({'model_name': 'deepseek-v3', 'reason': 'r'})
        assert resp.status_code == 202
        ticket = TicketList.objects.get(id=resp.json()['ticket_id'])
        assert 'model_name' in ticket.changed_fields

    @pytest.mark.integration
    def test_update_no_changed_fields_400(self):
        """规范化后无任何字段变更应 400"""
        with patch.object(LLMModelViewSet, '_validate_model_payload',
                          return_value=(None, {})):
            resp = self._put({'reason': 'r'})
        assert resp.status_code == 400
        assert '未检测到字段变更' in resp.json()['detail']

    @pytest.mark.integration
    def test_update_audit_failure_still_202(self):
        """update 审计写失败不阻断工单创建"""
        with patch('apps.audit.models.AuditLog.objects.create',
                   side_effect=RuntimeError('audit down')):
            resp = self._put({'base_url': 'https://new.api.com', 'reason': 'r'})
        assert resp.status_code == 202

    @pytest.mark.integration
    def test_destroy_no_permission_403(self):
        """destroy 无写权限应 403"""
        resp = self.client.delete(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'reason': 'r'}), content_type='application/json',
            **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_destroy_audit_failure_still_202(self):
        """destroy 审计写失败不阻断删除工单创建"""
        with patch('apps.audit.models.AuditLog.objects.create',
                   side_effect=RuntimeError('audit down')):
            resp = self.client.delete(
                f'/api/v1/system/llm-models/{self.model.id}/',
                data=json.dumps({'reason': 'r'}), content_type='application/json',
                **self.admin_a_headers)
        assert resp.status_code == 202

    @pytest.mark.integration
    def test_create_invalidate_cache_failure_not_blocking(self):
        """创建后 LLMModel 缓存失效失败不阻断创建"""
        with patch('apps.system.config_loader.invalidate_llm_model_cache',
                   side_effect=RuntimeError('redis down')):
            resp = self.client.post(
                '/api/v1/system/llm-models/',
                data=json.dumps({'name': 'New Model', 'provider': 'openai',
                                 'model_type': 'llm', 'model_name': 'new-m',
                                 'timeout': 30}),
                content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 201

    @pytest.mark.integration
    def test_create_write_audit_failure_not_blocking(self):
        """创建后模型管理审计写失败不阻断创建"""
        with patch('apps.audit.models.AuditLog.objects.create',
                   side_effect=RuntimeError('audit down')):
            resp = self.client.post(
                '/api/v1/system/llm-models/',
                data=json.dumps({'name': 'New Model 2', 'provider': 'openai',
                                 'model_type': 'embedding', 'model_name': 'new-m2',
                                 'timeout': 30}),
                content_type='application/json', **self.admin_a_headers)
        assert resp.status_code == 201


class TestTicketViewSetEdge(_TicketActionMixin, SystemAPITestBase):
    """TicketViewSet 列表/创建/操作错误路径补充"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()
        self.cfg = SystemConfig.objects.create(
            key='LLM_TIMEOUT', value='60', value_type='int',
            label='LLM 超时', category='llm', risk_level='normal')
        self.model = LLMModel.objects.create(
            name='DeepSeek 对话', provider='deepseek', model_type='llm',
            base_url='https://api.deepseek.com', model_name='deepseek-chat',
            timeout=120, is_active=True)

    def _post_ticket(self, data, headers=None):
        return self.client.post(
            '/api/v1/system/tickets/',
            data=json.dumps(data), content_type='application/json',
            **headers or self.admin_a_headers)

    def _make_config_ticket(self, config_key='LLM_TIMEOUT', applicant=None, status=None,
                            chain_risk='normal', current_step=0, new_value='120'):
        """ORM 直接创建配置工单（approve/reject 流程测试用）"""
        ticket = TicketList.objects.create(
            ticket_no=f'CFGEXT{uuid.uuid4().hex[:12]}', title='配置变更',
            biz_type=TicketBizType.CONFIG, operation='modify',
            config_key=config_key, risk_level=chain_risk,
            status=status or TicketStatus.PENDING,
            applicant=applicant or self.super_admin_a,
            approval_chain=_build_system_chain(chain_risk),
            current_step=current_step)
        TicketConfigDetail.objects.create(
            ticket=ticket, config_label='LLM 超时', reason='r',
            old_value='60', new_value=new_value, change_summary='')
        return ticket

    def _make_model_ticket(self, operation, changed_fields=None, chain_risk='normal',
                           applicant=None, status=None):
        """ORM 直接创建模型工单"""
        ticket = TicketList.objects.create(
            ticket_no=f'MDLEXT{uuid.uuid4().hex[:12]}', title=f'模型变更·{operation}',
            biz_type=TicketBizType.MODEL, operation=operation,
            risk_level=chain_risk, status=status or TicketStatus.PENDING,
            applicant=applicant or self.super_admin_a,
            target_model_id=self.model.id,
            approval_chain=_build_system_chain(chain_risk),
            current_step=0)
        TicketModelDetail.objects.create(
            ticket=ticket, reason='r', target_model_snapshot={'name': self.model.name},
            changed_fields=changed_fields or {},
            dependency_refs=[])
        return ticket

    @pytest.mark.integration
    def test_list_no_permission_403(self):
        """工单列表无读权限应 403"""
        resp = self.client.get('/api/v1/system/tickets/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_list_filter_by_ticket_type(self):
        """?ticket_type=config 只返回配置类工单"""
        self._post_ticket({'ticket_type': 'config', 'config_key': 'LLM_TIMEOUT',
                           'new_value': '120', 'reason': 'r'})
        self._make_model_ticket('update_normal', changed_fields={'timeout': {'old': 120, 'new': 300}})
        resp = self.client.get(
            '/api/v1/system/tickets/?ticket_type=config', **self.admin_b_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] >= 1
        assert all(t['ticket_type'] == 'config' for t in resp.json()['tickets'])

    @pytest.mark.integration
    def test_list_search_by_numeric_id(self):
        """?search=<数字> 按 id/target_model_id 精确匹配"""
        resp = self._post_ticket({'ticket_type': 'config', 'config_key': 'LLM_TIMEOUT',
                                  'new_value': '120', 'reason': 'r'})
        ticket_id = resp.json()['id']
        resp = self.client.get(
            f'/api/v1/system/tickets/?search={ticket_id}', **self.admin_b_headers)
        assert resp.status_code == 200
        assert any(t['id'] == ticket_id for t in resp.json()['tickets'])

    @pytest.mark.integration
    def test_list_invalid_page_400(self):
        """page 非数字应 400 而非 500"""
        resp = self.client.get('/api/v1/system/tickets/?page=abc', **self.admin_a_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_no_permission_403(self):
        """创建工单无写权限应 403"""
        resp = self._post_ticket(
            {'ticket_type': 'config', 'config_key': 'LLM_TIMEOUT',
             'new_value': '120', 'reason': 'r'},
            headers=self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_post_invalid_ticket_type_400(self):
        """ticket_type 取值非法应 400"""
        resp = self._post_ticket({'ticket_type': 'bogus', 'reason': 'r'})
        assert resp.status_code == 400
        assert 'ticket_type' in resp.json()['detail']

    @pytest.mark.integration
    def test_post_config_missing_new_value_400(self):
        """config 工单缺 new_value 应 400"""
        resp = self._post_ticket({'ticket_type': 'config', 'config_key': 'LLM_TIMEOUT',
                                  'reason': 'r'})
        assert resp.status_code == 400
        assert 'new_value' in resp.json()['detail']

    @pytest.mark.integration
    def test_post_schedule_invalid_cron_400(self):
        """schedule 工单 cron 非法应 400"""
        key = schedule_key('extra-test-job')
        SystemConfig.objects.create(
            key=key, value=serialize_schedule('0 2 * * *', True), value_type='json',
            label='测试任务', category='schedule', risk_level='high')
        resp = self._post_ticket({'ticket_type': 'schedule', 'config_key': key,
                                  'new_value': serialize_schedule('0 99 * * *', True),
                                  'reason': 'r'})
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_model_ticket_missing_target_400(self):
        """model 工单缺 target_model_id 应 400"""
        resp = self._post_ticket({'ticket_type': 'model', 'operation': 'delete',
                                  'reason': 'r'})
        assert resp.status_code == 400
        assert 'target_model_id' in resp.json()['detail']

    @pytest.mark.integration
    def test_post_model_ticket_non_int_id_400(self):
        """target_model_id 非整数应 400"""
        resp = self._post_ticket({'ticket_type': 'model', 'target_model_id': 'abc',
                                  'operation': 'delete', 'reason': 'r'})
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_model_ticket_not_found_404(self):
        """模型不存在应 404"""
        resp = self._post_ticket({'ticket_type': 'model', 'target_model_id': 99999,
                                  'operation': 'delete', 'reason': 'r'})
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_post_model_ticket_invalid_operation_400(self):
        """operation 取值非法应 400"""
        resp = self._post_ticket({'ticket_type': 'model', 'target_model_id': self.model.id,
                                  'operation': 'foo', 'reason': 'r'})
        assert resp.status_code == 400
        assert 'operation' in resp.json()['detail']

    @pytest.mark.integration
    def test_post_model_ticket_update_normal_requires_changed_fields(self):
        """update_normal 缺 changed_fields 应 400"""
        resp = self._post_ticket({'ticket_type': 'model', 'target_model_id': self.model.id,
                                  'operation': 'update_normal', 'reason': 'r'})
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_post_model_ticket_duplicate_pending_409(self):
        """已有待审批工单时重复提交应 409"""
        self._make_model_ticket('update_normal', changed_fields={'timeout': {'old': 120, 'new': 300}})
        resp = self._post_ticket({'ticket_type': 'model', 'target_model_id': self.model.id,
                                  'operation': 'update_normal',
                                  'changed_fields': {'timeout': {'old': 120, 'new': 300}},
                                  'reason': 'r'})
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_post_model_ticket_delete_with_dependency_400(self):
        """delete 操作模型被配置引用应 400"""
        SystemConfig.objects.create(
            key='LLM_BASE_MODEL', value='deepseek-chat', value_type='string',
            label='LLM 基础模型', category='llm')
        resp = self._post_ticket({'ticket_type': 'model', 'target_model_id': self.model.id,
                                  'operation': 'delete', 'reason': 'r'})
        assert resp.status_code == 400
        assert '禁止删除' in resp.json()['detail']

    @pytest.mark.integration
    def test_post_model_ticket_update_normal_201(self):
        """update_normal 工单创建成功，快照/变更字段写入详情子表"""
        resp = self._post_ticket({'ticket_type': 'model', 'target_model_id': self.model.id,
                                  'operation': 'update_normal',
                                  'changed_fields': {'timeout': {'old': 120, 'new': 300}},
                                  'reason': '调大超时'})
        assert resp.status_code == 201
        data = resp.json()
        assert data['status'] == TicketStatus.PENDING
        assert data['operation'] == 'update_normal'
        assert 'timeout' in data['changed_fields']
        assert data['snapshot_data']['model_name'] == 'deepseek-chat'
        # 模型本身未被修改
        self.model.refresh_from_db()
        assert self.model.timeout == 120

    @pytest.mark.integration
    def test_post_model_ticket_deactivate_201(self):
        """deactivate 工单创建成功"""
        resp = self._post_ticket({'ticket_type': 'model', 'target_model_id': self.model.id,
                                  'operation': 'deactivate', 'reason': '停用'})
        assert resp.status_code == 201
        assert resp.json()['operation'] == 'deactivate'

    @pytest.mark.integration
    def test_serialize_deactivate_operation_fields(self):
        """deactivate 工单序列化：changed_fields 展示为 ['is_active']"""
        ticket = self._make_model_ticket('deactivate',
                                         changed_fields={'is_active': {'old': True, 'new': False}})
        resp = self.client.get(
            f'/api/v1/system/tickets/{ticket.id}/', **self.admin_a_headers)
        assert resp.status_code == 200
        assert resp.json()['changed_fields'] == ['is_active']

    @pytest.mark.integration
    def test_ticket_mixin_dependency_missing_model_returns_empty(self):
        """_check_model_dependency 模型不存在时返回空列表"""
        assert _TicketMixin()._check_model_dependency(99999) == []

    @pytest.mark.integration
    def test_operation_get_method_not_allowed_405(self):
        """approve 接口 GET 返回 405 而非 401"""
        resp = self.client.get('/api/v1/system/tickets/1/approve/', **self.admin_a_headers)
        assert resp.status_code == 405

    @pytest.mark.integration
    def test_approve_config_missing_cfg_returns_400(self):
        """审批时配置项已被删除：返回 400 且工单不生效"""
        ticket = self._make_config_ticket(config_key='GONE_KEY')
        resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 400
        assert '不存在或已被删除' in resp.json()['detail']
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.PENDING

    @pytest.mark.integration
    def test_approve_config_invalidate_failure_still_applies(self):
        """审批生效后缓存失效失败不阻断（5min TTL 兜底）"""
        ticket = self._make_config_ticket(config_key='LLM_TIMEOUT')
        with patch('apps.system.config_loader.invalidate_config_cache',
                   side_effect=RuntimeError('redis down')):
            resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.EXECUTED
        self.cfg.refresh_from_db()
        assert self.cfg.value == '120'

    @pytest.mark.integration
    def test_approve_model_deactivate_dependency_rollback(self):
        """生效阶段发现依赖：工单回滚到 PENDING（审批时无依赖、应用时被引用）"""
        ticket = self._make_model_ticket(
            'deactivate', changed_fields={'is_active': {'old': True, 'new': False}})
        # 审批入口的依赖检查通过，但 _apply_model_ticket 应用时发现依赖 → 回滚
        with patch.object(ApproveTicketView, '_check_model_dependency',
                          side_effect=[[], ['LLM_BASE_MODEL']]):
            resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 400
        assert '停用操作已回滚' in resp.json()['detail']
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.PENDING
        assert ticket.current_step == 0
        assert ticket.executed_at is None
        self.model.refresh_from_db()
        assert self.model.is_active is True

    @pytest.mark.integration
    def test_approve_model_delete_dependency_rollback(self):
        """删除生效阶段发现依赖：工单回滚到 PENDING 且模型未被删除"""
        ticket = self._make_model_ticket('delete')
        # 审批入口的依赖检查通过，但 _apply_model_ticket 应用时发现依赖 → 回滚
        with patch.object(ApproveTicketView, '_check_model_dependency',
                          side_effect=[[], ['LLM_BASE_MODEL']]):
            resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 400
        assert '操作已回滚' in resp.json()['detail']
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.PENDING
        assert ticket.current_step == 0
        assert ticket.target_model_id == self.model.id
        # 模型未被删除
        assert LLMModel.objects.filter(id=self.model.id).exists()

    @pytest.mark.integration
    def test_approve_model_invalidate_failure_still_applies(self):
        """模型工单生效后缓存失效失败不阻断"""
        ticket = self._make_model_ticket(
            'update_normal', changed_fields={'timeout': {'old': 120, 'new': 300}})
        with patch('apps.system.config_loader.invalidate_llm_model_cache',
                   side_effect=RuntimeError('redis down')):
            resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 200
        self.model.refresh_from_db()
        assert self.model.timeout == 300

    @pytest.mark.integration
    def test_approve_chain_exhausted_409(self):
        """审批链已走完（防御分支）应 409"""
        ticket = TicketList.objects.create(
            ticket_no=f'CHAINEX{uuid.uuid4().hex[:12]}', title='链异常',
            biz_type=TicketBizType.CONFIG, operation='modify', config_key='LLM_TIMEOUT',
            risk_level='normal', status=TicketStatus.PENDING,
            applicant=self.super_admin_a, approval_chain=[], current_step=0)
        resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_approve_ticket_write_audit_failure_not_blocking(self):
        """审批审计写失败不阻断审批主流程"""
        ticket = self._make_config_ticket(config_key='LLM_TIMEOUT')
        with patch('apps.audit.models.AuditLog.objects.create',
                   side_effect=RuntimeError('audit down')):
            resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.EXECUTED

    @pytest.mark.integration
    def test_approve_super_admin_node_by_maintainer_403(self):
        """高风险项复核节点：持写权限但非超管的维护管理员被 403 拦截"""
        from rest_framework_simplejwt.tokens import RefreshToken
        maintainer = _create_maintainer('maint_approve')
        headers = {'HTTP_AUTHORIZATION': f'Bearer {str(RefreshToken.for_user(maintainer).access_token)}'}
        ticket = self._make_config_ticket(chain_risk='high')
        self._approve(ticket.id, self.admin_b_headers)  # 审核通过进入待复核
        resp = self._approve(ticket.id, headers)
        assert resp.status_code == 403
        assert '仅超级管理员可操作' in resp.json()['detail']

    @pytest.mark.integration
    def test_reject_no_permission_403(self):
        """驳回无写权限应 403"""
        ticket = self._make_config_ticket()
        resp = self._reject(ticket.id, self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_reject_super_admin_node_by_maintainer_403(self):
        """高风险项复核驳回：持写权限但非超管的维护管理员被 403 拦截"""
        from rest_framework_simplejwt.tokens import RefreshToken
        maintainer = _create_maintainer('maint_reject')
        headers = {'HTTP_AUTHORIZATION': f'Bearer {str(RefreshToken.for_user(maintainer).access_token)}'}
        ticket = self._make_config_ticket(chain_risk='high')
        self._approve(ticket.id, self.admin_b_headers)  # 审核通过进入待复核
        resp = self._reject(ticket.id, headers)
        assert resp.status_code == 403
        assert '仅超级管理员可操作' in resp.json()['detail']

    @pytest.mark.integration
    def test_reject_super_admin_node_same_as_approver_403(self):
        """复核驳回人不能与审核人相同"""
        ticket = self._make_config_ticket(chain_risk='high')
        self._approve(ticket.id, self.admin_b_headers)
        resp = self._reject(ticket.id, self.admin_b_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_reject_super_admin_node_200(self):
        """超管复核驳回成功，工单置为 REJECTED"""
        ticket = self._make_config_ticket(chain_risk='high')
        self._approve(ticket.id, self.admin_b_headers)
        resp = self._reject(ticket.id, self.admin_c_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.REJECTED


class TestTaskBoardEdge(SystemAPITestBase):
    """任务看板：分页参数降级与权限分支"""

    @pytest.fixture(autouse=True)
    def _env(self):
        self._init_env()

    @pytest.mark.integration
    def test_task_log_invalid_page_falls_back(self):
        """page/page_size 非法时降级为默认值而非 500"""
        resp = self.client.get(
            '/api/v1/system/tasks/?page=abc&page_size=xyz', **self.admin_a_headers)
        assert resp.status_code == 200
        assert resp.json()['page'] == 1
        assert resp.json()['page_size'] == 50

    @pytest.mark.integration
    def test_retry_no_permission_403(self):
        """重试任务无读权限应 403"""
        resp = self.client.post(
            '/api/v1/system/tasks/not-a-uuid/retry/', data={},
            content_type='application/json', **self.normal_headers)
        assert resp.status_code == 403
