"""
apps.retrieval.permission 单元测试 —— 检索层权限过滤 Q 对象构造

覆盖范围：
- build_permission_q：未登录 / super_admin / 普通用户 / root_types / node_ids / node_path_prefix
- Deny Override 铁律：黑名单对所有用户（含超管）生效

不依赖 DB：
build_permission_q 内部调用 _get_user_visible_depts / _get_shared_doc_ids /
_get_blocked_doc_ids 等 helper，这些 helper 直接查 ORM。
mock 后可隔离 DB 依赖，专注验证 Q 对象的构造逻辑与判定优先级。
Q 对象的 SQL 字段名通过 str(q) 检查（Django Q 的 __repr__ 会展开字段名）。
"""
import pytest
from unittest.mock import patch, MagicMock

from django.db.models import Q


# ============================================================================
# Mock 工厂
# ============================================================================
def _make_user(user_id=1, is_authenticated=True, is_super_admin=False,
               department_id=None, team_id=None):
    """构造 mock 用户，对齐 User 模型字段

    is_super_admin 作为属性暴露（User 模型上是 @property），
    MagicMock 直接赋值即可，无需 PropertyMock。
    """
    user = MagicMock()
    user.id = user_id
    user.is_authenticated = is_authenticated
    user.is_super_admin = is_super_admin
    user.department_id = department_id
    user.team_id = team_id
    user.team = None
    return user


# ============================================================================
# 未登录 / 未认证：返回空集 Q
# ============================================================================
@pytest.mark.unit
def test_anonymous_user_returns_empty_q():
    """未登录用户（user=None）应返回 Q(pk__in=[])，不召回任何文档"""
    from apps.retrieval.permission import build_permission_q
    q = build_permission_q(None)
    # Q(pk__in=[]) 的 children 应包含 ('pk__in', [])
    q_str = str(q)
    assert 'pk__in' in q_str
    assert '[]' in q_str


@pytest.mark.unit
def test_unauthenticated_user_returns_empty_q():
    """is_authenticated=False 的用户同样返回空集"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_authenticated=False)
    q = build_permission_q(user)
    assert 'pk__in' in str(q)


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
def test_anonymous_skips_block_list(mock_blocked):
    """未登录用户应短路返回，不触发黑名单查询（性能优化）"""
    from apps.retrieval.permission import build_permission_q
    build_permission_q(None)
    mock_blocked.assert_not_called()


# ============================================================================
# super_admin：全可见（黑名单不绕过）
# ============================================================================
@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
@patch('apps.retrieval.permission._get_user_visible_depts')
def test_super_admin_bypasses_natural_scope(mock_visible_depts, mock_blocked):
    """super_admin 不计算自然可见范围（_get_user_visible_depts 不调用）"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=True)
    mock_blocked.return_value = []

    build_permission_q(user)

    # super_admin 走 Q() 快路径，不计算 visible_depts
    mock_visible_depts.assert_not_called()


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
def test_super_admin_block_list_applied(mock_blocked):
    """super_admin 仍受黑名单约束（Deny Override 铁律：黑名单不绕过）"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=True)
    mock_blocked.return_value = [999]

    q = build_permission_q(user)

    # 黑名单子查询被调用
    mock_blocked.assert_called_once_with(user)
    # Q 对象中应包含 has_block_user 字段
    q_str = str(q)
    assert 'has_block_user' in q_str
    assert 'document_id__in' in q_str


# ============================================================================
# 普通用户：自然可见范围 + Owner + 共享 + 节点继承
# ============================================================================
@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
@patch('apps.retrieval.permission._get_shared_node_paths')
@patch('apps.retrieval.permission._get_shared_doc_ids')
@patch('apps.retrieval.permission._get_user_visible_teams')
@patch('apps.retrieval.permission._get_user_visible_depts')
def test_regular_user_calls_all_helpers(mock_depts, mock_teams, mock_doc_share,
                                        mock_node_paths, mock_blocked):
    """普通用户应计算所有可见范围维度（自然范围 + 共享 + 黑名单）"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=False, department_id=1, team_id=2)
    mock_depts.return_value = {1}
    mock_teams.return_value = {2}
    mock_doc_share.return_value = []
    mock_node_paths.return_value = []
    mock_blocked.return_value = []

    build_permission_q(user)

    mock_depts.assert_called_once_with(user)
    mock_teams.assert_called_once_with(user)
    mock_doc_share.assert_called_once()
    mock_node_paths.assert_called_once()
    mock_blocked.assert_called_once_with(user)


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
@patch('apps.retrieval.permission._get_shared_node_paths')
@patch('apps.retrieval.permission._get_shared_doc_ids')
@patch('apps.retrieval.permission._get_user_visible_teams')
@patch('apps.retrieval.permission._get_user_visible_depts')
def test_regular_user_q_contains_natural_scope(mock_depts, mock_teams, mock_doc_share,
                                               mock_node_paths, mock_blocked):
    """普通用户的 Q 对象应包含自然可见范围字段（visibility_level/owner_id）"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=False, department_id=1, team_id=2)
    mock_depts.return_value = {1}
    mock_teams.return_value = {2}
    mock_doc_share.return_value = []
    mock_node_paths.return_value = []
    mock_blocked.return_value = []

    q = build_permission_q(user)

    q_str = str(q)
    # 自然可见范围：PUBLIC / DEPT_ONLY / TEAM_ONLY
    assert 'visibility_level' in q_str
    # Owner 直接可见
    assert 'owner_id' in q_str


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
@patch('apps.retrieval.permission._get_shared_node_paths')
@patch('apps.retrieval.permission._get_shared_doc_ids')
@patch('apps.retrieval.permission._get_user_visible_teams')
@patch('apps.retrieval.permission._get_user_visible_depts')
def test_regular_user_q_contains_doc_share(mock_depts, mock_teams, mock_doc_share,
                                           mock_node_paths, mock_blocked):
    """有文档级共享时，Q 对象应包含 has_resource_share 和 document_id__in"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=False)
    mock_depts.return_value = set()
    mock_teams.return_value = set()
    mock_doc_share.return_value = [100, 200]  # 共享文档 ID
    mock_node_paths.return_value = []
    mock_blocked.return_value = []

    q = build_permission_q(user)

    q_str = str(q)
    assert 'has_resource_share' in q_str
    assert 'document_id__in' in q_str


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
@patch('apps.retrieval.permission._get_shared_node_paths')
@patch('apps.retrieval.permission._get_shared_doc_ids')
@patch('apps.retrieval.permission._get_user_visible_teams')
@patch('apps.retrieval.permission._get_user_visible_depts')
def test_regular_user_q_contains_node_share(mock_depts, mock_teams, mock_doc_share,
                                            mock_node_paths, mock_blocked):
    """有节点级共享时，Q 对象应包含 node_path__startswith 前缀匹配"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=False)
    mock_depts.return_value = set()
    mock_teams.return_value = set()
    mock_doc_share.return_value = []
    mock_node_paths.return_value = ['/1/5/', '/2/3/']
    mock_blocked.return_value = []

    q = build_permission_q(user)

    q_str = str(q)
    # 节点级共享继承：node_path 前缀匹配
    assert 'node_path__startswith' in q_str
    assert '/1/5/' in q_str
    assert '/2/3/' in q_str


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
@patch('apps.retrieval.permission._get_shared_node_paths')
@patch('apps.retrieval.permission._get_shared_doc_ids')
@patch('apps.retrieval.permission._get_user_visible_teams')
@patch('apps.retrieval.permission._get_user_visible_depts')
def test_regular_user_q_contains_block_list(mock_depts, mock_teams, mock_doc_share,
                                            mock_node_paths, mock_blocked):
    """普通用户的 Q 对象应包含黑名单剔除条件"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=False)
    mock_depts.return_value = set()
    mock_teams.return_value = set()
    mock_doc_share.return_value = []
    mock_node_paths.return_value = []
    mock_blocked.return_value = [500]

    q = build_permission_q(user)

    q_str = str(q)
    assert 'has_block_user' in q_str


# ============================================================================
# 业务过滤：root_types / node_ids / node_path_prefix
# ============================================================================
@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
def test_root_types_filter_applied(mock_blocked):
    """root_types 过滤应在 Q 对象中追加 root_type__in 条件"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=True)
    mock_blocked.return_value = []

    q = build_permission_q(user, root_types=['kb_default', 'kb_team'])

    q_str = str(q)
    assert 'root_type__in' in q_str
    assert 'kb_default' in q_str
    assert 'kb_team' in q_str


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
def test_node_ids_filter_applied(mock_blocked):
    """node_ids 过滤应在 Q 对象中追加 node_id__in 条件"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=True)
    mock_blocked.return_value = []

    q = build_permission_q(user, node_ids=[10, 20, 30])

    q_str = str(q)
    assert 'node_id__in' in q_str
    assert '10' in q_str
    assert '20' in q_str
    assert '30' in q_str


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
def test_node_path_prefix_filter_applied(mock_blocked):
    """node_path_prefix 过滤应在 Q 对象中追加 node_path__startswith 条件"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=True)
    mock_blocked.return_value = []

    q = build_permission_q(user, node_path_prefix='/1/5/')

    q_str = str(q)
    assert 'node_path__startswith' in q_str
    assert '/1/5/' in q_str


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
def test_no_business_filters(mock_blocked):
    """不传业务过滤参数时，Q 对象不应包含 root_type/node_id/node_path 业务字段"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=True)
    mock_blocked.return_value = []

    q = build_permission_q(user)

    q_str = str(q)
    # 仅含黑名单条件，不含业务过滤
    assert 'root_type__in' not in q_str
    assert 'node_id__in' not in q_str
    # node_path__startswith 仅在 node_path_prefix 或节点级共享时出现
    assert 'node_path__startswith' not in q_str


@pytest.mark.unit
@patch('apps.retrieval.permission._get_blocked_doc_ids')
def test_combined_filters(mock_blocked):
    """root_types + node_ids + node_path_prefix 可同时生效"""
    from apps.retrieval.permission import build_permission_q
    user = _make_user(is_super_admin=True)
    mock_blocked.return_value = []

    q = build_permission_q(user, root_types=['kb_default'],
                           node_ids=[1, 2], node_path_prefix='/1/')

    q_str = str(q)
    assert 'root_type__in' in q_str
    assert 'node_id__in' in q_str
    assert 'node_path__startswith' in q_str
