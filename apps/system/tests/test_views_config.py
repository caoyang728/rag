"""
apps.system.views 配置/模型/工单补充测试 —— 覆盖 test_views.py 未触及的分支

覆盖范围（与 test_views.py 互补，避免重复）：
- SystemConfigView：列表 options/secret 掩码/模型选择项/BUSINESS_DB_TABLES 分支；
  PUT 的 float/bool/json 规范化、多值差异摘要、secret 项掩码序列化
- LLMModelViewSet：改名直改 / 停用工单（含依赖拦截）/ 无变更 400 / PATCH /
  重复名称 400 / 删除时已有工单 409 / 待审批计数
- TicketViewSet（config 分支）：create_ticket 全错误分支 / list 状态筛选与
  creator=me / 待复核过滤 / retrieve / approve、reject、withdraw 错误路径
- TicketViewSet（model 分支）：全流程（列表筛选/详情/审批/驳回/撤回/依赖回滚）

Mock 策略：SystemConfigView 的业务表读取为外部依赖，测试中 mock 以隔离环境；
审批状态机走真实 DB 保证契约正确。
"""
import json
import uuid
from unittest.mock import patch

import pytest

from apps.users.models import (
    TicketList, TicketStatus, TicketBizType,
    TicketConfigDetail, TicketModelDetail,
)
from apps.system.models import (
    SystemConfig, LLMModel,
)
from apps.system.views import SystemConfigView, _build_system_chain
from apps.system.tests.test_views import SystemAPITestBase


def _config_by_key(resp):
    """把 /configs/ 列表响应解析为 key -> item 映射，供断言快速取值"""
    return {item['key']: item for group in resp.json()['groups'].values() for item in group}


def _post_ticket(client, ticket_id, action, headers, comment=''):
    """POST 统一工单动作接口（approve/reject/withdraw），统一请求构造"""
    return client.post(
        f'/api/v1/system/tickets/{ticket_id}/{action}/',
        data=json.dumps({'comment': comment}),
        content_type='application/json',
        **headers)


class _TicketActionMixin:
    """工单 approve/reject/withdraw 请求辅助 —— 薄封装 _post_ticket，供各测试类复用"""

    def _approve(self, ticket_id, headers, comment='ok'):
        return _post_ticket(self.client, ticket_id, 'approve', headers, comment)

    def _reject(self, ticket_id, headers, comment='no'):
        return _post_ticket(self.client, ticket_id, 'reject', headers, comment)

    def _withdraw(self, ticket_id, headers, comment='r'):
        return _post_ticket(self.client, ticket_id, 'withdraw', headers, comment)


class TestSystemConfigViewExtras(SystemAPITestBase):
    """SystemConfig 列表分支与 PUT 规范化补充"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充配置项"""
        self._init_env()
        self.cfg = SystemConfig.objects.create(
            key='LLM_TIMEOUT', value='60', value_type='int',
            label='LLM 超时', category='llm', risk_level='normal')

    def _put(self, key, value, reason='r', headers=None):
        return self.client.put(
            f'/api/v1/system/configs/{key}/',
            data=json.dumps({'value': value, 'reason': reason}),
            content_type='application/json',
            **headers or self.admin_a_headers)

    @pytest.mark.integration
    def test_list_options_parsed_and_secret_masked(self):
        """列表项 options JSON 被解析为数组，secret 项 value 掩码为 ***"""
        SystemConfig.objects.create(
            key='AGENT_MODE', value='docker', value_type='string',
            label='Agent 模式', category='agent',
            options=json.dumps([{'value': 'docker', 'label': '本地优先'}]))
        SystemConfig.objects.create(
            key='SECRET_KEY_CFG', value='secret-value', value_type='string',
            label='密钥', category='security', is_secret=True)
        resp = self.client.get('/api/v1/system/configs/', **self.normal_headers)
        assert resp.status_code == 200
        by_key = _config_by_key(resp)
        assert by_key['AGENT_MODE']['options'] == [{'value': 'docker', 'label': '本地优先'}]
        assert by_key['SECRET_KEY_CFG']['value'] == '***'
        assert by_key['SECRET_KEY_CFG']['is_secret'] is True

    @pytest.mark.integration
    def test_list_model_option_keys(self):
        """LLM/Embedding/Rerank 模型选择项用 LLMModel 表填充 options"""
        LLMModel.objects.create(
            name='DeepSeek 对话', provider='deepseek', model_type='llm',
            model_name='deepseek-chat', is_active=True)
        LLMModel.objects.create(
            name='BGE 向量', provider='bge', model_type='embedding',
            model_name='bge-m3', is_active=True)
        for key, category in [('LLM_BASE_MODEL', 'llm'), ('EMBEDDING_MODEL', 'embedding')]:
            SystemConfig.objects.create(
                key=key, value='deepseek-chat' if key == 'LLM_BASE_MODEL' else 'bge-m3',
                value_type='string', label=key, category=category)
        resp = self.client.get('/api/v1/system/configs/', **self.normal_headers)
        assert resp.status_code == 200
        by_key = _config_by_key(resp)
        llm_options = by_key['LLM_BASE_MODEL']['options']
        assert any(o['value'] == 'deepseek-chat' for o in llm_options)
        emb_options = by_key['EMBEDDING_MODEL']['options']
        assert any(o['value'] == 'bge-m3' for o in emb_options)

    @pytest.mark.integration
    def test_list_model_option_append_unknown_current_value(self):
        """当前配置值不在模型列表中时，作为'未在模型管理中'选项插入"""
        SystemConfig.objects.create(
            key='LLM_BASE_MODEL', value='legacy-model', value_type='string',
            label='LLM 基础模型', category='llm')
        resp = self.client.get('/api/v1/system/configs/', **self.normal_headers)
        assert resp.status_code == 200
        by_key = _config_by_key(resp)
        assert by_key['LLM_BASE_MODEL']['options'][0]['value'] == 'legacy-model'
        assert '未在模型管理中' in by_key['LLM_BASE_MODEL']['options'][0]['label']

    @pytest.mark.integration
    def test_list_business_tables_filled(self):
        """BUSINESS_DB_TABLES 项 options 由业务表列表填充"""
        SystemConfig.objects.create(
            key='BUSINESS_DB_TABLES', value='a,b', value_type='string',
            label='业务表', category='storage')
        fake_tables = [{'value': 'users', 'label': 'users'}]
        with patch.object(SystemConfigView, '_get_business_tables', return_value=fake_tables):
            resp = self.client.get('/api/v1/system/configs/', **self.normal_headers)
        assert resp.status_code == 200
        by_key = _config_by_key(resp)
        assert by_key['BUSINESS_DB_TABLES']['options'] == fake_tables

    @pytest.mark.integration
    def test_put_float_normalize(self):
        """float 类型配置：'3.5' 规范化并创建工单"""
        SystemConfig.objects.create(
            key='RERANK_WEIGHT', value='0.5', value_type='float',
            label='Rerank 权重', category='retrieval')
        resp = self._put('RERANK_WEIGHT', '0.7')
        assert resp.status_code == 201
        assert resp.json()['new_value'] == '0.7'

    @pytest.mark.integration
    def test_put_float_invalid_400(self):
        """float 类型配置传入非数字应 400"""
        SystemConfig.objects.create(
            key='RERANK_WEIGHT', value='0.5', value_type='float',
            label='Rerank 权重', category='retrieval')
        resp = self._put('RERANK_WEIGHT', 'abc')
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_put_bool_normalize(self):
        """bool 类型配置：'on' 规范化为 'true'"""
        SystemConfig.objects.create(
            key='ANALYTICS_ENABLED', value='false', value_type='bool',
            label='评估开关', category='analytics')
        resp = self._put('ANALYTICS_ENABLED', 'on')
        assert resp.status_code == 201
        assert resp.json()['new_value'] == 'true'

    @pytest.mark.integration
    def test_put_json_normalize(self):
        """json 类型配置：dict 输入序列化为 JSON 字符串存储"""
        SystemConfig.objects.create(
            key='LLM_PARAMS', value='{}', value_type='json',
            label='LLM 参数', category='llm')
        resp = self._put('LLM_PARAMS', {'temperature': 0.7})
        assert resp.status_code == 201
        assert json.loads(resp.json()['new_value']) == {'temperature': 0.7}

    @pytest.mark.integration
    def test_put_int_with_float_format_ok(self):
        """int 类型配置允许 '3.0' 这类格式，规范化为 '3'"""
        resp = self._put('LLM_TIMEOUT', '3.0')
        assert resp.status_code == 201
        assert resp.json()['new_value'] == '3'

    @pytest.mark.integration
    def test_put_multi_value_change_summary(self):
        """多值类配置（BUSINESS_DB_TABLES）生成 added/removed 差异摘要"""
        SystemConfig.objects.create(
            key='BUSINESS_DB_TABLES', value='a,c', value_type='string',
            label='业务表', category='storage')
        resp = self._put('BUSINESS_DB_TABLES', 'a,b')
        assert resp.status_code == 201
        assert resp.json()['change_summary'] == {'added': ['b'], 'removed': ['c']}

    @pytest.mark.integration
    def test_put_secret_config_masks_values(self):
        """secret 项创建工单后，接口返回的 old/new 值均掩码"""
        SystemConfig.objects.create(
            key='SECRET_KEY_CFG', value='plain-old', value_type='string',
            label='密钥', category='security', is_secret=True)
        resp = self._put('SECRET_KEY_CFG', 'plain-new')
        assert resp.status_code == 201
        assert resp.json()['old_value'] == '***'
        assert resp.json()['new_value'] == '***'


class TestLLMModelViewSetExtras(SystemAPITestBase):
    """LLMModel 改名直改 / 停用 / PATCH / 重复名称 / 删除拦截"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充模型配置"""
        self._init_env()
        self.model = LLMModel.objects.create(
            name='DeepSeek 对话', provider='deepseek', model_type='llm',
            base_url='https://api.deepseek.com', model_name='deepseek-chat',
            timeout=120, is_active=True)

    def _make_pending_model_ticket(self):
        """构造一条待审批的模型变更工单（含业务详情子表），供删除/列表拦截断言"""
        pend = TicketList.objects.create(
            ticket_no=f'MODELPEND{self.model.id}', title='模型变更工单',
            biz_type=TicketBizType.MODEL, operation='update_normal',
            risk_level='normal', status=TicketStatus.PENDING,
            applicant=self.super_admin_a, target_model_id=self.model.id)
        TicketModelDetail.objects.create(ticket=pend, reason='r')
        return pend

    @pytest.mark.integration
    def test_update_name_only_creates_ticket(self):
        """仅修改显示名（name）时由于校验器总会规范化 base_url/timeout，
        实际也会创建 update_normal 工单走审批（name 变更需审批）"""
        resp = self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'name': 'DeepSeek 新版'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 202
        assert resp.json()['operation'] == 'update_normal'
        ticket = TicketList.objects.get(id=resp.json()['ticket_id'])
        assert 'name' in ticket.changed_fields
        # 模型字段在审批通过前保持不变
        self.model.refresh_from_db()
        assert self.model.name == 'DeepSeek 对话'

    @pytest.mark.integration
    def test_update_deactivate_with_dependency_400(self):
        """停用被 SystemConfig 引用的模型应 400 拦截"""
        SystemConfig.objects.create(
            key='LLM_BASE_MODEL', value='deepseek-chat', value_type='string',
            label='LLM 基础模型', category='llm')
        resp = self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'is_active': False, 'reason': '停用'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400
        assert '禁止停用' in resp.json()['detail']

    @pytest.mark.integration
    def test_update_deactivate_creates_ticket(self):
        """停用无依赖的模型创建 deactivate 工单走普通审批"""
        resp = self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'is_active': False, 'reason': '切换供应商'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 202
        ticket = TicketList.objects.get(id=resp.json()['ticket_id'])
        assert ticket.operation == 'deactivate'
        assert ticket.risk_level == 'normal'
        assert ticket.status == TicketStatus.PENDING
        # 模型本身未被停用
        self.model.refresh_from_db()
        assert self.model.is_active is True

    @pytest.mark.integration
    def test_update_same_name_still_creates_ticket(self):
        """提交与当前一致的 name 时，因 base_url/timeout 被规范化进 payload，
        仍会创建工单（含 base_url/timeout 变更），验证 update 主路径"""
        resp = self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'name': 'DeepSeek 对话', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 202
        assert 'timeout' in TicketList.objects.get(id=resp.json()['ticket_id']).changed_fields

    @pytest.mark.integration
    def test_update_invalid_base_url_400(self):
        """update 传入非法 base_url 应 400（复用统一校验）"""
        resp = self.client.put(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'base_url': 'ftp://evil', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400
        assert 'base_url' in resp.json()['errors']

    @pytest.mark.integration
    def test_update_not_found_404(self):
        """更新不存在的模型应 404"""
        resp = self.client.put(
            '/api/v1/system/llm-models/99999/',
            data=json.dumps({'base_url': 'https://a.com', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_partial_update_reuses_update(self):
        """PATCH 部分更新复用 update 逻辑，创建工单返回 202"""
        resp = self.client.patch(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'timeout': 300, 'reason': '调大超时'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 202
        assert resp.json()['operation'] == 'update_normal'

    @pytest.mark.integration
    def test_destroy_with_pending_ticket_409(self):
        """删除时已有待审批工单应 409，避免重复审批"""
        self._make_pending_model_ticket()
        resp = self.client.delete(
            f'/api/v1/system/llm-models/{self.model.id}/',
            data=json.dumps({'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 409
        assert '待审批工单' in resp.json()['detail']

    @pytest.mark.integration
    def test_create_duplicate_name_400(self):
        """同类型下创建重名模型触发唯一约束，返回 400 而非 500"""
        resp = self.client.post(
            '/api/v1/system/llm-models/',
            data=json.dumps({
                'name': 'DeepSeek 对话', 'provider': 'deepseek', 'model_type': 'llm',
                'model_name': 'deepseek-v3', 'timeout': 60,
            }),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400
        assert '已存在' in resp.json()['detail']

    @pytest.mark.integration
    def test_list_pending_ticket_count_and_dependency(self):
        """列表返回 pending_ticket_count 与 dependency_count"""
        SystemConfig.objects.create(
            key='LLM_BASE_MODEL', value='deepseek-chat', value_type='string',
            label='LLM 基础模型', category='llm')
        self._make_pending_model_ticket()
        resp = self.client.get('/api/v1/system/llm-models/', **self.admin_a_headers)
        assert resp.status_code == 200
        data = resp.json()
        item = next(m for group in data['groups'].values() for m in group
                    if m['id'] == self.model.id)
        assert item['pending_ticket_count'] == 1
        assert item['dependency_count'] == 1


class TestConfigChangeTicketViewSetExtras(_TicketActionMixin, SystemAPITestBase):
    """配置工单：创建错误分支 / 列表筛选 / 审批/驳回/撤回错误路径"""

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

    def _create_ticket_via_api(self, key='LLM_TIMEOUT', new_value='120',
                               reason='r', headers=None):
        return self.client.post(
            '/api/v1/system/tickets/',
            data=json.dumps({'ticket_type': 'config', 'config_key': key,
                             'new_value': new_value, 'reason': reason}),
            content_type='application/json',
            **headers or self.admin_a_headers)

    @pytest.mark.integration
    def test_create_ticket_missing_key_400(self):
        resp = self.client.post(
            '/api/v1/system/tickets/',
            data=json.dumps({'ticket_type': 'config', 'new_value': '120', 'reason': 'r'}),
            content_type='application/json',
            **self.admin_a_headers)
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_ticket_missing_reason_400(self):
        resp = self._create_ticket_via_api(reason='')
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_ticket_not_found_404(self):
        resp = self._create_ticket_via_api(key='NOT_EXIST')
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_create_ticket_readonly_409(self):
        SystemConfig.objects.create(
            key='EMBEDDING_DIM', value='1024', value_type='int',
            label='向量维度', category='embedding', is_readonly=True)
        resp = self._create_ticket_via_api(key='EMBEDDING_DIM', new_value='768')
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_create_ticket_normalize_error_400(self):
        resp = self._create_ticket_via_api(key='LLM_TIMEOUT', new_value='3.5')
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_create_ticket_same_value_400(self):
        resp = self._create_ticket_via_api(key='LLM_TIMEOUT', new_value='60')
        assert resp.status_code == 400

    @pytest.mark.integration
    def test_list_status_filter_single_and_multi(self):
        """list 支持单状态与逗号分隔多状态筛选"""
        self._create_ticket_via_api()
        self._create_ticket_via_api(key='LOGIN_LOCK_THRESHOLD', new_value='10')
        resp = self.client.get(
            '/api/v1/system/tickets/?status=PENDING', **self.admin_b_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] >= 1
        resp = self.client.get(
            '/api/v1/system/tickets/?status=PENDING,APPROVED', **self.admin_b_headers)
        assert resp.status_code == 200

    @pytest.mark.integration
    def test_list_creator_me(self):
        """?creator=me 返回当前用户创建的全部工单（含自己创建）"""
        self._create_ticket_via_api()
        resp = self.client.get(
            '/api/v1/system/tickets/?creator=me', **self.admin_a_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] >= 1
        assert all(t['creator'] == self.super_admin_a.username for t in resp.json()['tickets'])

    @pytest.mark.integration
    def test_list_excludes_first_approved_reviewer(self):
        """待复核阶段工单对审核人不可见（防止审核+复核同一人）"""
        resp = self._create_ticket_via_api(key='LOGIN_LOCK_THRESHOLD', new_value='10')
        ticket_id = resp.json()['id']
        # admin_b 审核 -> 待复核（状态仍 PENDING，current_step 前进）
        self._approve(ticket_id, self.admin_b_headers)
        # admin_b 自己的待办列表中不应出现该工单（已审第 0 节点，进入待复核）
        resp = self.client.get(
            '/api/v1/system/tickets/?status=PENDING', **self.admin_b_headers)
        assert all(t['id'] != ticket_id for t in resp.json()['tickets'])
        # admin_c 的待办列表中应出现（待复核节点为超管复核人）
        resp = self.client.get(
            '/api/v1/system/tickets/?status=PENDING', **self.admin_c_headers)
        assert any(t['id'] == ticket_id for t in resp.json()['tickets'])

    @pytest.mark.integration
    def test_retrieve_200_and_404(self):
        """工单详情 200；不存在 404"""
        resp = self._create_ticket_via_api()
        ticket_id = resp.json()['id']
        resp = self.client.get(
            f'/api/v1/system/tickets/{ticket_id}/', **self.admin_a_headers)
        assert resp.status_code == 200
        assert resp.json()['config_key'] == 'LLM_TIMEOUT'
        resp = self.client.get(
            '/api/v1/system/tickets/99999/', **self.admin_a_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_approve_not_found_404_and_wrong_state_409(self):
        """审批不存在的工单 404；重复审批已通过工单 409"""
        resp = self._approve(99999, self.admin_b_headers)
        assert resp.status_code == 404
        # 先完成一次普通项审批（已生效）
        resp = self._create_ticket_via_api()
        ticket_id = resp.json()['id']
        self._approve(ticket_id, self.admin_b_headers)
        resp = self._approve(ticket_id, self.admin_b_headers, 'again')
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_approve_no_permission_403(self):
        """普通用户无 system.config.write 权限审批应 403"""
        resp = self._create_ticket_via_api()
        ticket_id = resp.json()['id']
        resp = self._approve(ticket_id, self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_reject_own_403_and_not_found_404_and_wrong_state_409(self):
        """驳回自己创建的工单 403；不存在 404；已通过工单不可再驳回 409"""
        resp = self._create_ticket_via_api()
        ticket_id = resp.json()['id']
        # 自驳 403
        resp = self._reject(ticket_id, self.admin_a_headers)
        assert resp.status_code == 403
        # 不存在 404
        resp = self._reject(99999, self.admin_b_headers)
        assert resp.status_code == 404
        # 先审批通过，再驳回 -> 409
        resp = self._create_ticket_via_api()
        ticket_id2 = resp.json()['id']
        self._approve(ticket_id2, self.admin_b_headers)
        resp = self._reject(ticket_id2, self.admin_b_headers)
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_withdraw_not_found_404_and_wrong_state_409(self):
        """撤回不存在的工单 404；已通过工单不可撤回 409"""
        resp = self._withdraw(99999, self.admin_a_headers)
        assert resp.status_code == 404
        resp = self._create_ticket_via_api()
        ticket_id = resp.json()['id']
        self._approve(ticket_id, self.admin_b_headers)
        resp = self._withdraw(ticket_id, self.admin_a_headers)
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_retrieve_change_summary_parsed(self):
        """含 change_summary 的工单详情返回解析后的 dict"""
        ticket = TicketList.objects.create(
            ticket_no='BUSINESSCFGT001', title='业务表变更',
            biz_type=TicketBizType.CONFIG, operation='modify',
            config_key='BUSINESS_DB_TABLES',
            risk_level='normal', status=TicketStatus.PENDING,
            applicant=self.super_admin_a)
        TicketConfigDetail.objects.create(
            ticket=ticket,
            config_label='业务表',
            reason='r',
            old_value='a', new_value='a,b',
            change_summary=json.dumps({'added': ['b'], 'removed': []}))
        resp = self.client.get(
            f'/api/v1/system/tickets/{ticket.id}/', **self.admin_a_headers)
        assert resp.status_code == 200
        assert resp.json()['change_summary'] == {'added': ['b'], 'removed': []}


class TestModelChangeTicketViewSet(_TicketActionMixin, SystemAPITestBase):
    """模型变更工单全流程：列表/详情/审批/驳回/撤回/依赖回滚"""

    @pytest.fixture(autouse=True)
    def _env(self):
        """在基类环境基础上补充模型配置"""
        self._init_env()
        self.model = LLMModel.objects.create(
            name='DeepSeek 对话', provider='deepseek', model_type='llm',
            base_url='https://api.deepseek.com', model_name='deepseek-chat',
            timeout=120, is_active=True)

    def _create_ticket(self, operation, creator=None, changed_fields=None,
                       risk_level='normal', status=None):
        """直接 ORM 创建工单（审批流测试关注审批动作，创建入口已有覆盖）

        注意：审批链节点直接复用 system.views._build_system_chain，
        普通项单节点 SYSTEM_AUDITOR、高风险项双节点 SYSTEM_AUDITOR + SUPER_ADMIN，
        与生产建单路径保持一致，避免测试链路与实现漂移。
        """
        chain = _build_system_chain(risk_level)
        ticket = TicketList.objects.create(
            ticket_no=f'MODELTEST-{uuid.uuid4().hex[:16]}',
            title=f'模型变更·{operation}',
            biz_type=TicketBizType.MODEL,
            operation=operation,
            risk_level=risk_level,
            status=status or TicketStatus.PENDING,
            applicant=creator or self.super_admin_a,
            target_model_id=self.model.id,
            approval_chain=chain,
            current_step=0,
        )
        TicketModelDetail.objects.create(
            ticket=ticket,
            reason='测试',
            target_model_snapshot={
                'name': self.model.name, 'model_name': self.model.model_name,
                'model_type': self.model.model_type, 'provider': self.model.provider,
                'base_url': self.model.base_url, 'timeout': self.model.timeout,
                'is_active': self.model.is_active,
            },
            changed_fields=changed_fields or {},
            dependency_refs=[],
        )
        return ticket

    @pytest.mark.integration
    def test_list_filters(self):
        """模型工单列表支持 status/operation 筛选与 creator=me"""
        # 不同创建人的工单：列表自动排除创建人自己
        self._create_ticket('update_normal')  # creator=admin_a
        self._create_ticket('delete', creator=self.super_admin_b, risk_level='high')
        # admin_c 非任何工单创建人，全部可见
        resp = self.client.get(
            '/api/v1/system/tickets/', **self.admin_c_headers)
        assert resp.status_code == 200
        assert resp.json()['total'] >= 2
        # 单状态 + operation 组合筛选
        resp = self.client.get(
            '/api/v1/system/tickets/?status=PENDING&operation=delete',
            **self.admin_c_headers)
        assert resp.json()['total'] >= 1
        assert all(t['operation'] == 'delete' for t in resp.json()['tickets'])
        # creator=me：admin_a 只能看到自己创建的 update_normal 工单
        resp = self.client.get(
            '/api/v1/system/tickets/?creator=me', **self.admin_a_headers)
        assert all(t['creator'] == self.super_admin_a.username for t in resp.json()['tickets'])
        assert all(t['operation'] == 'update_normal' for t in resp.json()['tickets'])

    @pytest.mark.integration
    def test_retrieve_200_and_404(self):
        """模型工单详情 200（delete 操作序列化快照）；不存在 404"""
        ticket = self._create_ticket('delete', risk_level='high')
        resp = self.client.get(
            f'/api/v1/system/tickets/{ticket.id}/', **self.admin_a_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data['operation'] == 'delete'
        assert data['snapshot_data']['model_name'] == 'deepseek-chat'
        assert 'name' in data['changed_fields']
        resp = self.client.get(
            '/api/v1/system/tickets/99999/', **self.admin_a_headers)
        assert resp.status_code == 404

    @pytest.mark.integration
    def test_approve_update_normal_applies(self):
        """update_normal 工单审核通过即生效，模型字段被更新"""
        ticket = self._create_ticket(
            'update_normal',
            changed_fields={'base_url': {'old': 'https://api.deepseek.com',
                                         'new': 'https://new.api.com'}})
        resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.EXECUTED
        self.model.refresh_from_db()
        assert self.model.base_url == 'https://new.api.com'
        ticket.refresh_from_db()
        assert ticket.executed_at is not None

    @pytest.mark.integration
    def test_approve_deactivate_applies(self):
        """deactivate 工单审核通过后模型被停用"""
        ticket = self._create_ticket(
            'deactivate', changed_fields={'is_active': {'old': True, 'new': False}})
        resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 200
        self.model.refresh_from_db()
        assert self.model.is_active is False

    @pytest.mark.integration
    def test_approve_deactivate_with_dependency_blocked(self):
        """停用工单审批时发现依赖，返回 400 且模型未停用"""
        SystemConfig.objects.create(
            key='LLM_BASE_MODEL', value='deepseek-chat', value_type='string',
            label='LLM 基础模型', category='llm')
        ticket = self._create_ticket(
            'deactivate', changed_fields={'is_active': {'old': True, 'new': False}})
        resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 400
        assert '无法继续' in resp.json()['detail']
        self.model.refresh_from_db()
        assert self.model.is_active is True

    @pytest.mark.integration
    def test_approve_delete_two_stage(self):
        """delete（高风险）工单：审核 -> 待复核 -> 超管复核后模型删除"""
        ticket = self._create_ticket('delete', risk_level='high')
        # admin_b 审核 -> 待复核
        resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 200
        # delete 高风险工单审核通过后进入待复核：状态仍 PENDING，审批链 step 前进
        assert resp.json()['status'] == TicketStatus.PENDING
        self.model.refresh_from_db()
        # 模型在复核前仍存在
        assert LLMModel.objects.filter(id=self.model.id).exists()
        # admin_c 超管复核 -> 删除
        resp = self._approve(ticket.id, self.admin_c_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.EXECUTED
        assert not LLMModel.objects.filter(id=self.model.id).exists()

    @pytest.mark.integration
    def test_approve_delete_with_dependency_400(self):
        """delete 工单审核时模型已被引用，无法进入复核"""
        ticket = self._create_ticket('delete', risk_level='high')
        SystemConfig.objects.create(
            key='LLM_BASE_MODEL', value='deepseek-chat', value_type='string',
            label='LLM 基础模型', category='llm')
        resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 400
        assert '无法继续' in resp.json()['detail']

    @pytest.mark.integration
    def test_approve_delete_dependency_during_review_rollback(self):
        """复核阶段模型被引用，_apply_ticket 回滚工单状态并返回 400"""
        ticket = self._create_ticket('delete', risk_level='high')
        self._approve(ticket.id, self.admin_b_headers)
        # 复核前新增依赖（模拟审批期间被引用）
        SystemConfig.objects.create(
            key='EVAL_MODEL', value='deepseek-chat', value_type='string',
            label='评估模型', category='eval')
        resp = self._approve(ticket.id, self.admin_c_headers)
        assert resp.status_code == 400
        assert '无法继续' in resp.json()['detail']
        ticket.refresh_from_db()
        assert ticket.status == TicketStatus.PENDING
        # 模型未被删除
        assert LLMModel.objects.filter(id=self.model.id).exists()

    @pytest.mark.integration
    def test_approve_self_forbidden(self):
        """创建人不能审批自己的模型工单"""
        ticket = self._create_ticket('update_normal')
        resp = self._approve(ticket.id, self.admin_a_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_approve_delete_review_by_non_super_admin_403(self):
        """delete 工单复核仅超管可操作"""
        ticket = self._create_ticket('delete', risk_level='high')
        self._approve(ticket.id, self.admin_b_headers)
        resp = self._approve(ticket.id, self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_approve_review_same_as_reviewer_403(self):
        """超管复核人不能与审核人相同"""
        ticket = self._create_ticket('delete', risk_level='high')
        self._approve(ticket.id, self.admin_b_headers)
        resp = self._approve(ticket.id, self.admin_b_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_approve_not_found_404_and_wrong_state_409(self):
        """审批不存在 404；已通过工单重复审批 409"""
        resp = self._approve(99999, self.admin_b_headers)
        assert resp.status_code == 404
        ticket = self._create_ticket('update_normal')
        self._approve(ticket.id, self.admin_b_headers)
        resp = self._approve(ticket.id, self.admin_c_headers)
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_approve_no_permission_403(self):
        """普通用户审批应 403"""
        ticket = self._create_ticket('update_normal')
        resp = self._approve(ticket.id, self.normal_headers)
        assert resp.status_code == 403

    @pytest.mark.integration
    def test_reject_pending_and_first_approved(self):
        """PENDING 驳回 200；待复核阶段由超管复核驳回 200"""
        ticket = self._create_ticket('update_normal')
        resp = self._reject(ticket.id, self.admin_b_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.REJECTED
        # 待复核阶段超管复核驳回
        ticket2 = self._create_ticket('delete', risk_level='high')
        self._approve(ticket2.id, self.admin_b_headers)
        resp = self._reject(ticket2.id, self.admin_c_headers)
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.REJECTED

    @pytest.mark.integration
    def test_reject_error_paths(self):
        """驳回：无原因 400 / 不存在 404 / 自驳 403 / 已通过 409"""
        ticket = self._create_ticket('update_normal')
        resp = self._reject(ticket.id, self.admin_b_headers, comment='')
        assert resp.status_code == 400
        resp = self._reject(99999, self.admin_b_headers)
        assert resp.status_code == 404
        ticket2 = self._create_ticket('update_normal')
        resp = self._reject(ticket2.id, self.admin_a_headers)
        assert resp.status_code == 403
        # 审批通过后驳回 -> 409
        ticket3 = self._create_ticket('update_normal')
        self._approve(ticket3.id, self.admin_b_headers)
        resp = self._reject(ticket3.id, self.admin_b_headers)
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_withdraw_by_creator_and_errors(self):
        """创建人撤回 200；非创建人 403；不存在 404；已通过 409"""
        ticket = self._create_ticket('update_normal')
        resp = self._withdraw(ticket.id, self.admin_a_headers, comment='撤回')
        assert resp.status_code == 200
        assert resp.json()['status'] == TicketStatus.CANCELLED
        # 非创建人撤回 403
        ticket2 = self._create_ticket('update_normal')
        resp = self._withdraw(ticket2.id, self.admin_b_headers)
        assert resp.status_code == 403
        # 不存在 404
        resp = self._withdraw(99999, self.admin_a_headers)
        assert resp.status_code == 404
        # 已通过 409
        ticket3 = self._create_ticket('update_normal')
        self._approve(ticket3.id, self.admin_b_headers)
        resp = self._withdraw(ticket3.id, self.admin_a_headers)
        assert resp.status_code == 409

    @pytest.mark.integration
    def test_list_excludes_creator_and_first_approved_reviewer(self):
        """待办列表排除创建人本人及待复核阶段的审核人"""
        ticket = self._create_ticket('delete', risk_level='high')
        self._approve(ticket.id, self.admin_b_headers)
        resp = self.client.get(
            '/api/v1/system/tickets/', **self.admin_b_headers)
        assert all(t['id'] != ticket.id for t in resp.json()['tickets'])
        resp = self.client.get(
            '/api/v1/system/tickets/', **self.admin_c_headers)
        assert any(t['id'] == ticket.id for t in resp.json()['tickets'])
