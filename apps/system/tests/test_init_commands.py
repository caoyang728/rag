"""
系统初始化命令（init_system 及 init/ 子包）测试

覆盖范围（与 management/commands/init_system.py 及 init/ 子包逐函数对齐）：
- common.py 公共工具：数据库连接测试（mock psycopg）/ 迁移检查 / 表存在检查 / yaml 加载
- roles / permissions / role_permissions：角色、权限点、角色-权限映射初始化
- departments / teams：部门、团队初始化（含部门缺失降级）
- users：初始用户创建（含角色绑定与角色缺失降级）
- global_memories：全局记忆初始化
- system_configs：系统配置 KV 初始化（_normalize_value 纯函数 + 新建/保留/force 覆盖/dry_run）+ 调度配置
- models：LLM/Embedding/Rerank 模型初始化（缺字段跳过 / 已存在保留 / force 覆盖）
- init_system Command：编排流程（配置文件缺失 / 数据库不可达 / 迁移缺失 / 表缺失 / 配置损坏 /
  dry_run 不写库 / 全量初始化 / 已初始化增量模式 / --with-org 合并组织数据）

说明：
- 使用最小 config dict 构造数据，不依赖包内真实 yaml，保证用例自包含
- 所有 DB 操作用 @pytest.mark.django_db（事务自动回滚）
- 纯逻辑函数（_normalize_value / load_config 等）用 @pytest.mark.unit 标记
"""
import json

from unittest.mock import patch

import pytest

from django.core.management import call_command

from apps.system.management.commands.init import (
    common,
    roles,
    permissions,
    role_permissions,
    departments,
    teams,
    users,
    global_memories,
    system_configs,
    models,
)


# ============================================================================
# 纯逻辑单元测试（无 DB 依赖）
# ============================================================================

@pytest.mark.unit
class TestNormalizeValue:
    """system_configs._normalize_value 纯函数：按 value_type 规范化存储值"""

    def test_bool_accepts_bool_true(self):
        assert system_configs._normalize_value(True, 'bool') == 'true'

    def test_bool_accepts_bool_false(self):
        assert system_configs._normalize_value(False, 'bool') == 'false'

    def test_bool_accepts_string_true_variants(self):
        # yaml 中 true/false 可能被解析成字符串 '1'/'yes'/'on' 等，需统一归一
        for v in ('1', 'true', 'yes', 'on', 'True', 'ON'):
            assert system_configs._normalize_value(v, 'bool') == 'true'

    def test_bool_accepts_other_string_as_false(self):
        assert system_configs._normalize_value('0', 'bool') == 'false'
        assert system_configs._normalize_value('off', 'bool') == 'false'

    def test_bool_accepts_numeric(self):
        assert system_configs._normalize_value(1, 'bool') == 'true'
        assert system_configs._normalize_value(0, 'bool') == 'false'

    def test_json_converts_dict_to_string(self):
        # list/dict 转 json 字符串存储，方便前端直接解析
        assert system_configs._normalize_value({'a': 1}, 'json') == '{"a": 1}'

    def test_json_keeps_string_as_is(self):
        assert system_configs._normalize_value('{"a": 1}', 'json') == '{"a": 1}'

    def test_other_type_converts_to_string(self):
        assert system_configs._normalize_value(12.5, 'string') == '12.5'


@pytest.mark.unit
class TestLoadConfig:
    """common.load_config：yaml 配置文件加载"""

    def test_load_config_when_file_valid_then_returns_dict(self, tmp_path):
        cfg = tmp_path / 'init.yaml'
        cfg.write_text('roles:\n  - code: admin\n', encoding='utf-8')
        data = common.load_config(str(cfg))
        assert data == {'roles': [{'code': 'admin'}]}

    def test_load_config_when_file_invalid_then_returns_none(self, tmp_path):
        # 非法 yaml 内容时返回 None，由编排命令提前终止
        cfg = tmp_path / 'bad.yaml'
        cfg.write_text('roles: [unclosed\n', encoding='utf-8')
        assert common.load_config(str(cfg)) is None


# ============================================================================
# common 工具（DB 相关）
# ============================================================================

@pytest.mark.integration
class TestCheckTableExists:
    """common.check_table_exists：表存在检查"""

    @pytest.mark.django_db
    def test_when_table_exists_then_true(self):
        assert common.check_table_exists('user_role_list') is True

    @pytest.mark.django_db
    def test_when_table_missing_then_false(self):
        assert common.check_table_exists('not_exist_table_xyz') is False


@pytest.mark.integration
class TestCheckMigrations:
    """common.check_migrations：users 迁移执行状态检查"""

    @pytest.mark.django_db
    def test_when_migrations_applied_then_true(self):
        # 测试库由 pytest-django 执行全部迁移，users 迁移必然存在
        assert common.check_migrations() is True

    @pytest.mark.django_db
    def test_when_no_migration_records_then_false(self):
        # 模拟空迁移记录：直接 patch django.db.connection 返回 0
        # （check_migrations 函数内 `from django.db import connection`，须 patch django.db.connection）
        with patch('django.db.connection') as mock_conn:
            mock_conn.cursor.return_value.fetchone.return_value = (0,)
            assert common.check_migrations() is False

    @pytest.mark.django_db
    def test_when_db_error_then_false(self):
        with patch('django.db.connection') as mock_conn:
            mock_conn.cursor.side_effect = RuntimeError('db down')
            assert common.check_migrations() is False


@pytest.mark.unit
class TestDbConnection:
    """common.test_db_connection：psycopg 直连测试（mock psycopg 隔离）"""

    def _patch_env(self, monkeypatch, clear_url=True):
        if clear_url:
            monkeypatch.delenv('DATABASE_URL', raising=False)
        monkeypatch.setenv('PG_DB_HOST', 'pg-host')
        monkeypatch.setenv('PG_DB_PORT', '5433')
        monkeypatch.setenv('PG_DB_DATABASE', 'rag_test')
        monkeypatch.setenv('PG_DB_USER', 'rag_user')

    def test_when_connect_success_then_true(self, monkeypatch):
        self._patch_env(monkeypatch)
        fake_cursor = type('C', (), {
            'execute': lambda self, sql: None,
            'fetchone': lambda self: ('PostgreSQL 16.0',),
        })()
        fake_conn = type('C', (), {'cursor': lambda self: fake_cursor, 'close': lambda self: None})()
        with patch('psycopg.connect', return_value=fake_conn) as m:
            assert common.test_db_connection() is True
            # 使用环境变量拼接的 DSN（非 DATABASE_URL）
            m.assert_called_once()
            assert m.call_args.kwargs['host'] == 'pg-host'

    def test_when_connect_raises_psycopg_error_then_false(self, monkeypatch):
        self._patch_env(monkeypatch)
        with patch('psycopg.connect', side_effect=RuntimeError('connect refused')):
            assert common.test_db_connection() is False

    def test_when_uses_database_url_then_parses(self, monkeypatch):
        # DATABASE_URL 存在时走 urlparse 分支解析 DSN
        monkeypatch.setenv('DATABASE_URL', 'postgres://u:p@db-host:5999/rag_db')
        fake_cursor = type('C', (), {
            'execute': lambda self, sql: None,
            'fetchone': lambda self: ('PostgreSQL 15',),
        })()
        fake_conn = type('C', (), {'cursor': lambda self: fake_cursor, 'close': lambda self: None})()
        with patch('psycopg.connect', return_value=fake_conn) as m:
            assert common.test_db_connection() is True
            assert m.call_args.kwargs['host'] == 'db-host'
            assert m.call_args.kwargs['port'] == 5999
            assert m.call_args.kwargs['dbname'] == 'rag_db'


# ============================================================================
# 角色 / 权限 / 角色-权限映射
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestCreateRoles:
    """init.roles.create_roles"""

    def _config(self):
        return {'roles': [
            {'code': 'admin', 'name': '管理员', 'is_builtin': True,
             'role_type': 'SUPER_ADMIN', 'data_scope': 'ALL'},
            {'code': 'viewer', 'name': '只读', 'description': '只读角色'},
        ]}

    def test_create_then_roles_persisted(self):
        assert roles.create_roles(self._config()) == 2
        from apps.users.models import Role
        admin = Role.objects.get(role_key='admin')
        assert admin.is_builtin is True
        assert admin.role_type == 'SUPER_ADMIN'
        assert Role.objects.get(role_key='viewer').description == '只读角色'

    def test_when_role_exists_then_skipped(self):
        from apps.users.models import Role
        Role.objects.create(role_key='admin', name='已有管理员')
        # 已存在角色跳过，不覆盖用户调整过的 name
        assert roles.create_roles(self._config()) == 1
        assert Role.objects.get(role_key='admin').name == '已有管理员'

    def test_dry_run_then_nothing_created(self):
        assert roles.create_roles(self._config(), dry_run=True) == 2
        from apps.users.models import Role
        assert Role.objects.count() == 0


@pytest.mark.integration
@pytest.mark.django_db
class TestCreatePermissions:
    """init.permissions.create_permissions"""

    def _config(self):
        return {'permissions': [
            {'code': 'system.read', 'name': '系统读', 'module': 'system', 'is_builtin': True},
            {'code': 'system.write', 'name': '系统写', 'module': 'system'},
        ]}

    def test_create_then_permissions_persisted(self):
        assert permissions.create_permissions(self._config()) == 2
        from apps.users.models import Permission
        p = Permission.objects.get(permission_key='system.read')
        assert p.module == 'system'
        assert p.is_builtin is True

    def test_when_perm_exists_then_skipped(self):
        from apps.users.models import Permission
        Permission.objects.create(permission_key='system.read', permission_name='旧名')
        assert permissions.create_permissions(self._config()) == 1
        assert Permission.objects.get(permission_key='system.read').permission_name == '旧名'

    def test_dry_run_then_nothing_created(self):
        assert permissions.create_permissions(self._config(), dry_run=True) == 2
        from apps.users.models import Permission
        assert Permission.objects.count() == 0


@pytest.mark.integration
@pytest.mark.django_db
class TestCreateRolePermissions:
    """init.role_permissions.create_role_permissions"""

    def _setup(self):
        from apps.users.models import Role, Permission
        role = Role.objects.create(role_key='admin', name='管理员')
        p1 = Permission.objects.create(permission_key='sys.read', permission_name='读')
        p2 = Permission.objects.create(permission_key='sys.write', permission_name='写')
        return role, p1, p2

    def test_create_then_mappings_persisted(self):
        role, p1, p2 = self._setup()
        config = {'role_permissions': {'admin': ['sys.read', 'sys.write']}}
        assert role_permissions.create_role_permissions(config) == 2
        from apps.users.models import RolePermissionRel
        assert RolePermissionRel.objects.filter(role=role).count() == 2

    def test_when_role_missing_then_skipped(self):
        self._setup()
        config = {'role_permissions': {'ghost_role': ['sys.read']}}
        assert role_permissions.create_role_permissions(config) == 0

    def test_when_perm_missing_then_skipped(self):
        role, p1, p2 = self._setup()
        config = {'role_permissions': {'admin': ['sys.read', 'no_such_perm']}}
        assert role_permissions.create_role_permissions(config) == 1

    def test_when_mapping_exists_then_skipped(self):
        from apps.users.models import RolePermissionRel
        role, p1, p2 = self._setup()
        RolePermissionRel.objects.create(role=role, permission=p1)
        config = {'role_permissions': {'admin': ['sys.read', 'sys.write']}}
        assert role_permissions.create_role_permissions(config) == 1

    def test_dry_run_then_nothing_created(self):
        role, p1, p2 = self._setup()
        config = {'role_permissions': {'admin': ['sys.read']}}
        assert role_permissions.create_role_permissions(config, dry_run=True) == 1
        from apps.users.models import RolePermissionRel
        assert RolePermissionRel.objects.filter(role=role).count() == 0


# ============================================================================
# 部门 / 团队
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestCreateDepartments:
    """init.departments.create_departments"""

    def _config(self):
        return {'departments': [
            {'code': 'dept_a', 'name': '部门A', 'sort_order': 1},
            {'code': 'dept_b', 'name': '部门B'},
        ]}

    def test_create_then_departments_persisted(self):
        assert departments.create_departments(self._config()) == 2
        from apps.users.models import Department
        assert Department.objects.get(code='dept_a').sort_order == 1

    def test_when_dept_exists_then_skipped(self):
        from apps.users.models import Department
        Department.objects.create(code='dept_a', name='已有部门')
        assert departments.create_departments(self._config()) == 1
        assert Department.objects.get(code='dept_a').name == '已有部门'

    def test_dry_run_then_nothing_created(self):
        assert departments.create_departments(self._config(), dry_run=True) == 2
        from apps.users.models import Department
        assert Department.objects.count() == 0


@pytest.mark.integration
@pytest.mark.django_db
class TestCreateTeams:
    """init.teams.create_teams"""

    def test_create_then_teams_persisted(self):
        from apps.users.models import Department
        dept = Department.objects.create(code='dept_a', name='部门A')
        config = {'teams': [
            {'code': 'team_1', 'name': '团队1', 'department': '部门A'},
            {'code': 'team_2', 'name': '团队2', 'department': '部门A'},
        ]}
        assert teams.create_teams(config) == 2
        from apps.users.models import Team
        assert Team.objects.get(code='team_1').department_id == dept.id
        assert Team.objects.get(code='team_2').department_id == dept.id

    def test_when_department_missing_then_create_fails_without_record(self):
        # Team.department 非空约束：部门不存在时 init 创建抛 IntegrityError 被吞掉，
        # 返回 0 且不落库（注意：该异常会破坏当前事务，故不再做后续 DB 查询）
        config = {'teams': [{'code': 'team_x', 'name': '团队X', 'department': '不存在部门'}]}
        assert teams.create_teams(config) == 0

    def test_when_team_exists_then_skipped(self):
        from apps.users.models import Department, Team
        dept = Department.objects.create(code='dept_a', name='部门A')
        Team.objects.create(code='team_1', name='已有团队', department=dept)
        config = {'teams': [{'code': 'team_1', 'name': '团队1', 'department': '部门A'}]}
        assert teams.create_teams(config) == 0

    def test_dry_run_then_nothing_created(self):
        config = {'teams': [{'code': 'team_1', 'name': '团队1'}]}
        assert teams.create_teams(config, dry_run=True) == 1
        from apps.users.models import Team
        assert Team.objects.count() == 0


# ============================================================================
# 初始用户
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestCreateUsers:
    """init.users.create_users"""

    def test_create_then_user_with_role_bound(self):
        from apps.users.models import Role
        Role.objects.create(role_key='super_admin', name='超管')
        config = {'users': [
            {'username': 'admin', 'email': 'admin@test.com', 'password': 'p@ss12345',
             'real_name': '管理员', 'role': 'super_admin'},
        ]}
        assert users.create_users(config) == 1
        from apps.users.models import User, UserRoleRel
        u = User.objects.get(username='admin')
        assert u.real_name == '管理员'
        assert UserRoleRel.objects.filter(user=u).count() == 1

    def test_when_role_missing_then_user_created_without_role(self):
        config = {'users': [
            {'username': 'admin', 'email': 'admin@test.com', 'password': 'p@ss12345',
             'role': 'no_such_role'},
        ]}
        assert users.create_users(config) == 1
        from apps.users.models import User, UserRoleRel
        u = User.objects.get(username='admin')
        assert UserRoleRel.objects.filter(user=u).count() == 0

    def test_when_user_exists_then_skipped(self):
        from apps.users.models import User
        User.objects.create_user(username='admin', email='a@test.com', password='x12345')
        config = {'users': [
            {'username': 'admin', 'email': 'admin@test.com', 'password': 'p@ss12345'},
        ]}
        assert users.create_users(config) == 0

    def test_dry_run_then_nothing_created(self):
        config = {'users': [
            {'username': 'admin', 'email': 'admin@test.com', 'password': 'p@ss12345'},
        ]}
        assert users.create_users(config, dry_run=True) == 1
        from apps.users.models import User
        assert User.objects.filter(username='admin').count() == 0


# ============================================================================
# 全局记忆
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestCreateGlobalMemories:
    """init.global_memories.create_global_memories"""

    def _config(self):
        return {'global_memories': [
            {'key': 'company_rule', 'content': '公司规则', 'scope_root_types': ['company_doc'],
             'priority': 10, 'is_enabled': True},
        ]}

    def test_create_then_memory_persisted(self):
        assert global_memories.create_global_memories(self._config()) == 1
        from apps.memory.models import GlobalMemory
        gm = GlobalMemory.objects.get(key='company_rule')
        assert gm.scope_root_types == ['company_doc']
        assert gm.priority == 10

    def test_when_key_exists_then_skipped(self):
        from apps.memory.models import GlobalMemory
        GlobalMemory.objects.create(key='company_rule', content='旧内容')
        assert global_memories.create_global_memories(self._config()) == 0
        assert GlobalMemory.objects.get(key='company_rule').content == '旧内容'

    def test_dry_run_then_nothing_created(self):
        assert global_memories.create_global_memories(self._config(), dry_run=True) == 1
        from apps.memory.models import GlobalMemory
        assert GlobalMemory.objects.count() == 0


# ============================================================================
# 系统配置（KV）与调度配置
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestCreateSystemConfigs:
    """init.system_configs.create_system_configs"""

    def _config(self):
        return {'system_configs': [
            {'key': 'max_tokens', 'value': 2048, 'value_type': 'int',
             'label': '最大Token', 'description': '单次最大', 'unit': '个',
             'category': 'llm', 'risk_level': 'normal'},
            {'key': 'enable_audit', 'value': True, 'value_type': 'bool',
             'label': '开启审计', 'category': 'security', 'is_secret': False},
            {'key': 'whitelist', 'value': ['1.1.1.1'], 'value_type': 'json',
             'label': '白名单', 'options': ['a', 'b'], 'category': 'security'},
        ]}

    def test_create_then_configs_persisted(self):
        assert system_configs.create_system_configs(self._config()) == 3
        from apps.system.models import SystemConfig
        assert SystemConfig.objects.get(key='max_tokens').value == '2048'
        assert SystemConfig.objects.get(key='enable_audit').value == 'true'
        # options 列表转 JSON 字符串存储
        wl = SystemConfig.objects.get(key='whitelist')
        assert json.loads(wl.value) == ['1.1.1.1']
        assert json.loads(wl.options) == ['a', 'b']

    def test_when_exists_without_force_then_keep_value_update_metadata(self):
        from apps.system.models import SystemConfig
        SystemConfig.objects.create(key='max_tokens', value='9999', value_type='int',
                                    label='旧标签', category='llm')
        # 非 force：保留已存在项 value，另两个新增项创建，返回 created=2
        assert system_configs.create_system_configs(self._config()) == 2
        obj = SystemConfig.objects.get(key='max_tokens')
        assert obj.value == '9999'
        assert obj.label == '最大Token'

    def test_when_exists_with_force_then_value_overwritten(self):
        from apps.system.models import SystemConfig
        SystemConfig.objects.create(key='max_tokens', value='9999', value_type='int',
                                    label='旧标签', category='llm')
        assert system_configs.create_system_configs(self._config(), force=True) == 2
        obj = SystemConfig.objects.get(key='max_tokens')
        assert obj.value == '2048'

    def test_dry_run_then_nothing_created(self):
        assert system_configs.create_system_configs(self._config(), dry_run=True) == 3
        from apps.system.models import SystemConfig
        assert SystemConfig.objects.count() == 0

    def test_secret_value_not_logged_in_created(self):
        # is_secret 项返回计数正常，仅日志脱敏（此处验证创建成功即可）
        cfg = {'system_configs': [
            {'key': 'api_key', 'value': 'sk-secret', 'value_type': 'string', 'is_secret': True},
        ]}
        assert system_configs.create_system_configs(cfg) == 1
        from apps.system.models import SystemConfig
        assert SystemConfig.objects.get(key='api_key').value == 'sk-secret'


@pytest.mark.integration
@pytest.mark.django_db
class TestCreateScheduleConfigs:
    """init.system_configs.create_schedule_configs：基于调度注册表单一数据源"""

    def test_create_then_schedule_configs_persisted(self):
        from apps.system.scheduler_registry import SCHEDULED_TASKS, schedule_key
        assert len(SCHEDULED_TASKS) > 0
        assert system_configs.create_schedule_configs() == len(SCHEDULED_TASKS)
        from apps.system.models import SystemConfig
        key = schedule_key(SCHEDULED_TASKS[0]['name'])
        obj = SystemConfig.objects.get(key=key)
        assert obj.value_type == 'json'

    def test_when_exists_without_force_then_keep_value(self):
        from apps.system.scheduler_registry import SCHEDULED_TASKS, schedule_key
        key = schedule_key(SCHEDULED_TASKS[0]['name'])
        from apps.system.models import SystemConfig
        SystemConfig.objects.create(key=key, value='{"original": true}', value_type='json',
                                    label='旧', category='schedule')
        assert system_configs.create_schedule_configs() == len(SCHEDULED_TASKS) - 1
        assert SystemConfig.objects.get(key=key).value == '{"original": true}'

    def test_when_exists_with_force_then_value_overwritten(self):
        from apps.system.scheduler_registry import SCHEDULED_TASKS, schedule_key
        key = schedule_key(SCHEDULED_TASKS[0]['name'])
        from apps.system.models import SystemConfig
        SystemConfig.objects.create(key=key, value='{"original": true}', value_type='json',
                                    label='旧', category='schedule')
        system_configs.create_schedule_configs(force=True)
        obj = SystemConfig.objects.get(key=key)
        assert obj.value != '{"original": true}'


# ============================================================================
# 模型配置
# ============================================================================

@pytest.mark.integration
@pytest.mark.django_db
class TestCreateLlmModels:
    """init.models.create_llm_models"""

    def _config(self):
        return {'llm_models': [
            {'name': 'DeepSeek 主模型', 'provider': 'deepseek', 'model_type': 'llm',
             'base_url': 'https://api.deepseek.com', 'model_name': 'deepseek-chat', 'is_active': True},
            {'name': 'Embedding 模型', 'provider': 'bge', 'model_type': 'embedding',
             'model_name': 'bge-large-zh'},
        ]}

    def test_create_then_models_persisted(self):
        assert models.create_llm_models(self._config()) == 2
        from apps.system.models import LLMModel
        m = LLMModel.objects.get(model_type='llm', name='DeepSeek 主模型')
        assert m.provider == 'deepseek'
        assert m.model_name == 'deepseek-chat'

    def test_when_missing_key_fields_then_skipped(self):
        # 缺 name/model_type 的脏数据跳过，避免落库
        cfg = {'llm_models': [
            {'name': '  ', 'model_type': 'llm'},
            {'name': 'ok_model', 'model_type': 'llm', 'model_name': 'x'},
        ]}
        assert models.create_llm_models(cfg) == 1

    def test_when_exists_without_force_then_kept(self):
        from apps.system.models import LLMModel
        LLMModel.objects.create(name='DeepSeek 主模型', provider='old', model_type='llm',
                                model_name='old-model')
        assert models.create_llm_models(self._config()) == 1
        m = LLMModel.objects.get(model_type='llm', name='DeepSeek 主模型')
        assert m.provider == 'old'

    def test_when_exists_with_force_then_overwritten(self):
        from apps.system.models import LLMModel
        LLMModel.objects.create(name='DeepSeek 主模型', provider='old', model_type='llm',
                                model_name='old-model')
        assert models.create_llm_models(self._config(), force=True) == 1
        m = LLMModel.objects.get(model_type='llm', name='DeepSeek 主模型')
        assert m.provider == 'deepseek'

    def test_dry_run_then_nothing_created(self):
        assert models.create_llm_models(self._config(), dry_run=True) == 2
        from apps.system.models import LLMModel
        assert LLMModel.objects.count() == 0


# ============================================================================
# init_system 命令编排
# ============================================================================

@pytest.mark.integration
class TestInitSystemCommand:
    """init_system Command.handle 编排流程（mock DB 前置检查，聚焦分支逻辑）"""

    def _write_config(self, tmp_path, content):
        cfg = tmp_path / 'init.yaml'
        cfg.write_text(content, encoding='utf-8')
        return str(cfg)

    def _minimal_yaml(self):
        return (
            'roles:\n'
            '  - code: admin\n'
            '    name: 管理员\n'
            'permissions:\n'
            '  - code: sys.read\n'
            '    name: 系统读\n'
            'users:\n'
            '  - username: init_admin\n'
            '    email: init_admin@test.com\n'
            '    password: p@ss12345\n'
            'system_configs:\n'
            '  - key: max_tokens\n'
            '    value: 2048\n'
            '    value_type: int\n'
            'llm_models:\n'
            '  - name: TestModel\n'
            '    model_type: llm\n'
            '    model_name: test-chat\n'
            'global_memories:\n'
            '  - key: company_rule\n'
            '    content: 规则\n'
        )

    @pytest.mark.django_db
    def test_when_config_missing_then_returns(self, tmp_path):
        call_command('init_system', config=str(tmp_path / 'no.yaml'))
        # 无异常即通过；未写入任何数据
        from apps.system.models import SystemConfig
        assert SystemConfig.objects.count() == 0

    @pytest.mark.django_db
    def test_when_db_unreachable_then_returns(self, tmp_path):
        cfg = self._write_config(tmp_path, self._minimal_yaml())
        with patch('apps.system.management.commands.init.common.test_db_connection',
                   return_value=False):
            call_command('init_system', config=cfg)
        from apps.users.models import Role
        assert Role.objects.count() == 0

    @pytest.mark.django_db
    def test_when_migrations_missing_then_returns(self, tmp_path):
        cfg = self._write_config(tmp_path, self._minimal_yaml())
        with patch('apps.system.management.commands.init.common.test_db_connection',
                   return_value=True), \
             patch('apps.system.management.commands.init.common.check_migrations',
                   return_value=False):
            call_command('init_system', config=cfg)
        from apps.users.models import Role
        assert Role.objects.count() == 0

    @pytest.mark.django_db
    def test_when_role_table_missing_then_returns(self, tmp_path):
        cfg = self._write_config(tmp_path, self._minimal_yaml())
        with patch('apps.system.management.commands.init.common.test_db_connection',
                   return_value=True), \
             patch('apps.system.management.commands.init.common.check_migrations',
                   return_value=True), \
             patch('apps.system.management.commands.init.common.check_table_exists',
                   return_value=False):
            call_command('init_system', config=cfg)
        from apps.users.models import Role
        assert Role.objects.count() == 0

    @pytest.mark.django_db
    def test_dry_run_then_nothing_created(self, tmp_path):
        cfg = self._write_config(tmp_path, self._minimal_yaml())
        call_command('init_system', config=cfg, dry_run=True)
        from apps.users.models import Role, Permission, User
        from apps.system.models import SystemConfig, LLMModel
        from apps.memory.models import GlobalMemory
        assert Role.objects.count() == 0
        assert Permission.objects.count() == 0
        assert User.objects.filter(username='init_admin').count() == 0
        assert SystemConfig.objects.count() == 0
        assert LLMModel.objects.count() == 0
        assert GlobalMemory.objects.count() == 0

    @pytest.mark.django_db
    def test_full_init_then_data_created(self, tmp_path):
        cfg = self._write_config(tmp_path, self._minimal_yaml())
        call_command('init_system', config=cfg)
        from apps.users.models import Role, Permission, User
        from apps.system.models import SystemConfig, LLMModel
        from apps.memory.models import GlobalMemory
        assert Role.objects.filter(role_key='admin').exists()
        assert Permission.objects.filter(permission_key='sys.read').exists()
        assert User.objects.filter(username='init_admin').exists()
        assert SystemConfig.objects.filter(key='max_tokens').exists()
        assert LLMModel.objects.filter(name='TestModel').exists()
        assert GlobalMemory.objects.filter(key='company_rule').exists()

    @pytest.mark.django_db
    def test_when_initialized_and_no_force_then_incremental(self, tmp_path):
        # 预先创建 super_admin 角色 → 视为已初始化，非 force 走增量分支
        from apps.users.models import Role
        Role.objects.create(role_key='super_admin', name='超管')
        cfg = self._write_config(tmp_path, self._minimal_yaml())
        call_command('init_system', config=cfg)
        # 增量模式只补系统配置/模型，不重新创建角色/用户
        from apps.users.models import User
        from apps.system.models import SystemConfig
        assert Role.objects.filter(role_key='admin').count() == 0
        assert User.objects.filter(username='init_admin').count() == 0
        assert SystemConfig.objects.filter(key='max_tokens').exists()

    @pytest.mark.django_db
    def test_with_org_then_creates_departments_and_teams(self, tmp_path):
        # --with-org 时合并开发示例组织数据（departments/teams）
        cfg = self._write_config(tmp_path, self._minimal_yaml())
        call_command('init_system', config=cfg, with_org=True)
        from apps.users.models import Department, Team
        assert Department.objects.count() > 0
        assert Team.objects.count() > 0
