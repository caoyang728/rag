"""
apps.system.views 接口集成测试 —— 系统配置 & 模型管理 & 变更工单

覆盖范围：
- SystemConfigView：列表 / 详情 / 废弃项 404 / PUT 创建工单（含只读项、缺原因、同值、权限校验）
- LLMModelViewSet：列表权限 / 创建 / 校验失败 / 改名直改 / 改字段建工单 / 重复工单 / 删除建高风险工单 / 依赖拦截
- ConfigChangeTicketViewSet：普通项审批生效 / 高风险项审核+超管复核 / 防自审 / 驳回 / 撤回
- 认证与权限：匿名 401、普通用户无权限 403、超管放行

采用 pytest-django（django_db）+ JWT：
配置/模型变更涉及 ORM 写入、工单状态机与事务一致性，
需真实 DB + 真实权限链路验证端到端契约，mock 会掩盖审批闭环漏洞。
审计日志写入在视图内被 try/except 包裹，失败不阻断主流程，故无需额外构造审计表。
"""
import json
from unittest.mock import patch

import pytest
from django.test import Client
from rest_framework_simplejwt.tokens import RefreshToken

from apps.users.models import User, Role, UserRoleRel, GrantStatus
from apps.users.models import TicketList, TicketStatus, TicketBizType
from apps.system.models import (
    SystemConfig, LLMModel,
)


def _get_or_create_role(role_key, **defaults):
    """获取或创建内置角色，补齐默认字段"""
    default_map = {
        'super_admin': dict(name='超级管理员', is_builtin=True),
        'viewer': dict(name='查看者', is_builtin=True),
        'contributor': dict(name='贡献者', is_builtin=True),
    }
    defaults = {**default_map.get(role_key, {}), **defaults}
    role, _ = Role.objects.get_or_create(role_key=role_key, defaults=defaults)
    return role


def _create_test_user(username, password='testpass123', is_super_admin=False, **extra):
    """创建测试用户，可选绑定 super_admin 角色"""
    extra.setdefault('email', f'{username}@test.com')
    user = User.objects.create_user(username=username, password=password, **extra)
    if is_super_admin:
        admin_role = _get_or_create_role('super_admin')
        UserRoleRel.objects.get_or_create(
            user=user, role=admin_role,
            defaults={'status': GrantStatus.ACTIVE})
    return user


def _get_auth_token(user):
    """生成 JWT access token"""
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token)


@pytest.mark.django_db
class SystemAPITestBase:
    """系统 API 测试公共基类 —— 准备超管/普通用户 + JWT header（子类自动继承 django_db）"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """pytest fixture：注入 client/三方超管/普通用户 + JWT header"""
        self._init_env()

    def _init_env(self):
        """构造测试环境：client/角色/三方超管/普通用户 + JWT header（供子类复用）"""
        self.client = Client()
        _get_or_create_role('viewer')
        _get_or_create_role('contributor')

        # 三个超管用于覆盖"创建/审核/超管复核"三方分离的高风险审批流程
        self.super_admin_a = _create_test_user(
            username='admin_a', password='pass12345', is_super_admin=True)
        self.super_admin_b = _create_test_user(
            username='admin_b', password='pass12345', is_super_admin=True)
        self.super_admin_c = _create_test_user(
            username='admin_c', password='pass12345', is_super_admin=True)
        self.normal_user = _create_test_user(
            username='normal', password='pass12345', is_super_admin=False)

        self.anon_headers = {}
        self.normal_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.normal_user)}'}
        self.admin_a_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.super_admin_a)}'}
        self.admin_b_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.super_admin_b)}'}
        self.admin_c_headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(self.super_admin_c)}'}


# ============================================================================
# SystemConfigView —— 配置列表 / 详情 / PUT 创建变更工单
# ============================================================================
class TestSystemConfigView(SystemAPITestBase):
    """SystemConfig 配置读取与工单创建测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充配置项"""
        self._init_env()
        # 普通可改配置项
        self.cfg = SystemConfig.objects.create(
            key='LLM_TIMEOUT', value='60', value_type='int',
            label='LLM 超时', category='llm', risk_level='normal')
        # 高风险配置项
        self.high_cfg = SystemConfig.objects.create(
            key='LOGIN_LOCK_THRESHOLD', value='5', value_type='int',
            label='登录锁定阈值', category='security', risk_level='high')
        # 只读配置项（改了需重建索引，只能改 .env）
        self.readonly_cfg = SystemConfig.objects.create(
            key='EMBEDDING_DIM', value='1024', value_type='int',
            label='向量维度', category='embedding', is_readonly=True)
        # 已废弃配置项
        self.deprecated_cfg = SystemConfig.objects.create(
            key='PRODUCTION_EVAL_HOURLY_GUARANTEE', value='1', value_type='int',
            label='保底', category='eval')

    @pytest.mark.integration
    def test_list_authenticated_200(self):
        """已登录用户可查看配置列表（按 category 分组）"""
        resp = self.client.get('/api/v1/system/configs/', **self.normal_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert 'groups' in data
        assert 'total' in data
        # 废弃项不应出现在列表中
        keys = [item['key'] for items in data['groups'].values() for item in items]
        assert 'PRODUCTION_EVAL_HOURLY_GUARANTEE' not in keys
        assert 'LLM_TIMEOUT' in keys

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名用户访问应 401（IsAuthenticated 拦截）"""
        resp = self.client.get('/api/v1/system/configs/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_retrieve_single_200(self):
        """按 key 获取单条配置"""
        resp = self.client.get(
            '/api/v1/system/configs/LLM_TIMEOUT/', **self.normal_headers)
        assert resp.status_code == 200
        assert resp.json()['key'] == 'LLM_TIMEOUT'

    @pytest.mark.integration
    def test_retrieve_deprecated_404(self):
        """废弃的配置项直接返回 404，不对外暴露"""
        resp = self.client.get(
            '/api/v1/system/configs/PRODUCTION_EVAL_HOURLY_GUARANTEE/',
            **self.normal_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_retrieve_not_found_404(self):
        """不存在的 key 返回 404"""
        resp = self.client.get(
            '/api/v1/system/configs/NOT_EXIST/', **self.normal_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_put_creates_ticket_201(self):
        """超管提交配置变更应创建 pending 工单（不直接落库改值）"""
        resp = self.client.put(
            '/api/v1/system/configs/LLM_TIMEOUT/',
            data=json.dumps({'value': '120', 'reason': '调大超时'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data['status'] == TicketStatus.PENDING
        assert data['old_value'] == '60'
        assert data['new_value'] == '120'
        assert data['risk_level'] == 'normal'
        # 配置项本身未变（等审批通过才生效）
        self.cfg.refresh_from_db()
        assert self.cfg.value == '60'

    @pytest.mark.integration
    def test_put_no_permission_403(self):
        """普通用户无 system.config.write 权限应 403"""
        resp = self.client.put(
            '/api/v1/system/configs/LLM_TIMEOUT/',
            data=json.dumps({'value': '120', 'reason': 'r'}),
            content_type='application/json',
            **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_put_readonly_409(self):
        """只读项禁止提交工单（需改 .env 重启）"""
        resp = self.client.put(
            '/api/v1/system/configs/EMBEDDING_DIM/',
            data=json.dumps({'value': '768', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_put_missing_reason_400(self):
        """未填变更原因应 400"""
        resp = self.client.put(
            '/api/v1/system/configs/LLM_TIMEOUT/',
            data=json.dumps({'value': '120'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_put_same_value_400(self):
        """新值与当前值一致应 400，避免无效审批"""
        resp = self.client.put(
            '/api/v1/system/configs/LLM_TIMEOUT/',
            data=json.dumps({'value': '60', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_put_deprecated_400(self):
        """废弃项禁止修改"""
        resp = self.client.put(
            '/api/v1/system/configs/PRODUCTION_EVAL_HOURLY_GUARANTEE/',
            data=json.dumps({'value': '0', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_put_invalid_int_value_400(self):
        """int 类型配置传入小数应 400（后端兜底校验）"""
        resp = self.client.put(
            '/api/v1/system/configs/LLM_TIMEOUT/',
            data=json.dumps({'value': '3.5', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_put_not_found_404(self):
        """不存在的配置项提交工单应 404"""
        resp = self.client.put(
            '/api/v1/system/configs/NOT_EXIST/',
            data=json.dumps({'value': 'x', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_put_high_risk_creates_ticket_with_high_risk(self):
        """高风险项提交工单时 risk_level 冗余为 high"""
        resp = self.client.put(
            '/api/v1/system/configs/LOGIN_LOCK_THRESHOLD/',
            data=json.dumps({'value': '10', 'reason': '放宽锁定'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 201
        assert resp.json()['risk_level'] == 'high'


# ============================================================================
# LLMModelViewSet —— 模型配置 CRUD 与审批
# ============================================================================
class TestLLMModelViewSet(SystemAPITestBase):
    """LLMModel 模型管理接口测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充模型配置"""
        self._init_env()
        self.model = LLMModel.objects.create(
            name='DeepSeek 对话', provider='deepseek', model_type='llm',
            base_url='https://api.deepseek.com', model_name='deepseek-chat',
            timeout=120, is_active=True)

    @pytest.mark.integration
    def test_list_super_admin_200(self):
        """超管可查看模型列表（按 model_type 分组）"""
        resp = self.client.get('/api/v1/system/llm-models/', **self.admin_a_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert 'groups' in data
        assert 'total' in data
        assert data['total'] >= 1

    @pytest.mark.integration
    def test_list_normal_user_403(self):
        """普通用户无 system.config.read 权限应 403"""
        resp = self.client.get('/api/v1/system/llm-models/', **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_list_anonymous_401(self):
        """匿名用户访问应 401"""
        resp = self.client.get('/api/v1/system/llm-models/', **self.anon_headers)
        assert resp.status_code in [401, 403]

    @pytest.mark.integration
    def test_retrieve_200(self):
        """超管可获取单条模型"""
        resp = self.client.get(
            f'/api/v1/system/llm-models/{self.model.id}/', **self.admin_a_headers)
        assert resp.status_code == 200
        assert resp.json()['id'] == self.model.id

    @pytest.mark.integration
    def test_create_201(self):
        """新增模型无需审批，直接创建"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({
                'name': 'Embed 模型', 'provider': 'openai',
                'model_type': 'embedding', 'base_url': 'https://api.openai.com',
                'model_name': 'text-embedding-3-small', 'timeout': 60,
            }),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 201
        assert resp.json()['model_name'] == 'text-embedding-3-small'

    @pytest.mark.integration
    def test_create_no_permission_403(self):
        """普通用户无 system.config.write 权限应 403"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({'name': 'x', 'provider': 'p', 'model_type': 'llm',
                              'model_name': 'm'}),
            content_type='application/json',
            **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_invalid_base_url_400(self):
        """base_url 必须以 http/https 开头，防止 SSRF"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({
                'name': 'x', 'provider': 'p', 'model_type': 'llm',
                'base_url': 'ftp://evil', 'model_name': 'm',
            }),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'base_url' in resp.json().get('errors', {})

    @pytest.mark.integration
    def test_create_invalid_model_type_400(self):
        """model_type 必须在 llm/embedding/rerank 之内"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({
                'name': 'x', 'provider': 'p', 'model_type': 'invalid',
                'model_name': 'm',
            }),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'model_type' in resp.json().get('errors', {})

    @pytest.mark.integration
    def test_create_invalid_timeout_400(self):
        """timeout 必须 >= 1 秒"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({
                'name': 'x', 'provider': 'p', 'model_type': 'llm',
                'model_name': 'm', 'timeout': 0,
            }),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'timeout' in resp.json().get('errors', {})

    @pytest.mark.integration
    def test_update_no_permission_403(self):
        """普通用户无 system.config.write 权限更新模型应 403"""
        resp = self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'name': '改名'}),
            content_type='application/json',
            **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_update_field_creates_ticket_202(self):
        """修改 base_url 等字段应创建工单走审批，返回 202"""
        resp = self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'base_url': 'https://new.api.com', 'reason': '换地址'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 202
        data = resp.json()
        assert data['ticket_id']
        # 工单为普通修改、pending 状态
        ticket = TicketList.objects.get(id=data['ticket_id'])
        assert ticket.operation == 'update_normal'
        assert ticket.status == TicketStatus.PENDING
        # 模型本身未被改（等审批通过）
        self.model.refresh_from_db()
        assert self.model.base_url == 'https://api.deepseek.com'

    @pytest.mark.integration
    def test_update_duplicate_pending_ticket_409(self):
        """已有待审批工单时再次提交应 409，避免重复审批"""
        # 先建一个工单
        self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'base_url': 'https://a.com', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        # 再次提交应冲突
        resp = self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'base_url': 'https://b.com', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_destroy_creates_high_risk_ticket_202(self):
        """删除模型应创建高风险工单（超管复核）"""
        resp = self.client.delete(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'reason': '废弃'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 202
        ticket = TicketList.objects.get(biz_type=TicketBizType.MODEL,
                                        target_model_id=self.model.id, operation='delete')
        assert ticket.risk_level == 'high'
        assert ticket.status == TicketStatus.PENDING
        # 模型未被物理删除（等超管复核）
        self.model.refresh_from_db()

    @pytest.mark.integration
    def test_destroy_with_dependency_400(self):
        """模型被 SystemConfig 引用时禁止删除"""
        SystemConfig.objects.create(
            key='LLM_BASE_MODEL', value='deepseek-chat', value_type='string',
            label='LLM 基础模型', category='llm')
        resp = self.client.delete(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400


# ============================================================================
# ConfigChangeTicketViewSet —— 配置变更工单审批流
# ============================================================================
class TestConfigChangeTicketFlow(SystemAPITestBase):
    """配置变更工单审批全流程测试"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充配置项"""
        self._init_env()
        self.normal_cfg = SystemConfig.objects.create(
            key='LLM_TIMEOUT', value='60', value_type='int',
            label='LLM 超时', category='llm', risk_level='normal')
        self.high_cfg = SystemConfig.objects.create(
            key='LOGIN_LOCK_THRESHOLD', value='5', value_type='int',
            label='登录锁定阈值', category='security', risk_level='high')

    def _create_ticket(self, cfg, creator, new_value):
        """辅助：以指定用户创建一个配置变更工单"""
        headers = {'HTTP_AUTHORIZATION': f'Bearer {_get_auth_token(creator)}'}
        return self.client.put(
            f'/api/v1/system/configs/{cfg.key}/',
            data=json.dumps({'value': new_value, 'reason': '测试变更'}),
            content_type='application/json',
            **headers)

    @pytest.mark.integration
    def test_normal_ticket_approve_applies_config(self):
        """普通项：审核通过即生效，配置值被写入"""
        # admin_a 创建工单
        resp = self._create_ticket(self.normal_cfg, self.super_admin_a, '120')
        ticket_id = resp.json()['id']
        # admin_b 审核（!= 创建人）
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/approve/',
            data=json.dumps({'comment': '同意'}),
            content_type='application/json',
            **self.admin_b_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.EXECUTED
        # 配置应已生效
        self.normal_cfg.refresh_from_db()
        assert self.normal_cfg.value == '120'
        # 工单记录了生效时间
        ticket = TicketList.objects.get(id=ticket_id)
        assert ticket.executed_at is not None

    @pytest.mark.integration
    def test_high_risk_ticket_two_stage_approval(self):
        """高风险项：审核通过进入待复核，超管复核后才生效"""
        # admin_a 创建工单
        resp = self._create_ticket(self.high_cfg, self.super_admin_a, '10')
        ticket_id = resp.json()['id']
        # admin_b 审核 -> 进入待超管复核
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/approve/',
            data=json.dumps({'comment': '审核通过'}),
            content_type='application/json',
            **self.admin_b_headers)
        assert resp.status_code == 200
        # 高风险项审核通过后进入待复核：状态仍为 PENDING，审批链 step 前进
        assert resp.json()['status'] == TicketStatus.PENDING
        # 配置尚未生效
        self.high_cfg.refresh_from_db()
        assert self.high_cfg.value == '5'
        # admin_c 超管复核 -> 生效（!= 创建人且 != 审核人）
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/approve/',
            data=json.dumps({'comment': '复核通过'}),
            content_type='application/json',
            **self.admin_c_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.EXECUTED
        self.high_cfg.refresh_from_db()
        assert self.high_cfg.value == '10'

    @pytest.mark.integration
    def test_high_risk_super_admin_review_by_non_super_admin_403(self):
        """高风险项复核仅超管可操作，普通管理员应 403"""
        resp = self._create_ticket(self.high_cfg, self.super_admin_a, '10')
        ticket_id = resp.json()['id']
        # admin_b 审核 -> first_approved
        self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/approve/',
            data=json.dumps({'comment': 'ok'}),
            content_type='application/json',
            **self.admin_b_headers)
        # 普通用户尝试复核应 403
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/approve/',
            data=json.dumps({'comment': 'r'}),
            content_type='application/json',
            **self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_self_approve_forbidden(self):
        """创建人不能审批自己的工单（防自审）"""
        resp = self._create_ticket(self.normal_cfg, self.super_admin_a, '120')
        ticket_id = resp.json()['id']
        # admin_a 自己审批应 403
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/approve/',
            data=json.dumps({'comment': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_super_admin_review_same_as_reviewer_forbidden(self):
        """超管复核人不能与审核人相同"""
        resp = self._create_ticket(self.high_cfg, self.super_admin_a, '10')
        ticket_id = resp.json()['id']
        # admin_b 审核 -> first_approved
        self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/approve/',
            data=json.dumps({'comment': 'ok'}),
            content_type='application/json',
            **self.admin_b_headers)
        # admin_b 再做超管复核应 403（与审核人相同）
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/approve/',
            data=json.dumps({'comment': 'r'}),
            content_type='application/json',
            **self.admin_b_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_reject_ticket(self):
        """驳回需填原因，工单状态置为 rejected，配置不变"""
        resp = self._create_ticket(self.normal_cfg, self.super_admin_a, '120')
        ticket_id = resp.json()['id']
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/reject/',
            data=json.dumps({'comment': '不合理'}),
            content_type='application/json',
            **self.admin_b_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.REJECTED
        # 配置不变
        self.normal_cfg.refresh_from_db()
        assert self.normal_cfg.value == '60'

    @pytest.mark.integration
    def test_reject_without_comment_400(self):
        """驳回未填原因应 400"""
        resp = self._create_ticket(self.normal_cfg, self.super_admin_a, '120')
        ticket_id = resp.json()['id']
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/reject/',
            data=json.dumps({}),
            content_type='application/json',
            **self.admin_b_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_withdraw_by_creator(self):
        """创建人可撤回待审批工单"""
        resp = self._create_ticket(self.normal_cfg, self.super_admin_a, '120')
        ticket_id = resp.json()['id']
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/withdraw/',
            data=json.dumps({'comment': '撤回'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.CANCELLED

    @pytest.mark.integration
    def test_withdraw_by_non_creator_403(self):
        """非创建人不可撤回"""
        resp = self._create_ticket(self.normal_cfg, self.super_admin_a, '120')
        ticket_id = resp.json()['id']
        resp = self.client.post(
            f'/api/v1/system/tickets/{ticket_id}/withdraw/',
            data=json.dumps({'comment': 'r'}),
            content_type='application/json',
            **self.admin_b_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_create_ticket_via_endpoint(self):
        """POST /tickets/ 创建工单主入口与 PUT /configs/<key>/ 行为一致"""
        resp = self.client.post(
            '/api/v1/system/tickets/',
            data=json.dumps({
                'ticket_type': 'config',
                'config_key': 'LLM_TIMEOUT',
                'new_value': '90',
                'reason': '通过工单入口创建',
            }),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 201
        assert resp.json()['status'] == TicketStatus.PENDING

    @pytest.mark.integration
    def test_list_tickets_excludes_creator(self):
        """待审核列表自动排除创建人自己的工单（避免自审）"""
        self._create_ticket(self.normal_cfg, self.super_admin_a, '120')
        resp = self.client.get(
            '/api/v1/system/tickets/', **self.admin_a_headers)
        assert resp.status_code == 200
        for t in resp.json()['tickets']:
            assert t['creator'] != self.super_admin_a.username
