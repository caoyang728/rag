"""
apps.knowledge.access 单元测试 —— 文档访问权限判定（Deny Override 铁律）

覆盖范围：
- 未登录 / 未认证 → 全拒绝
- Owner 全权限（绕过黑名单——所有权原则）
- 黑名单 Deny Override（命中即全拒，对超管也生效）
- super_admin 系统级快路径（绕过 permission_key 但不绕过黑名单）
- kb_admin / 团队组长 → 全权限
- 自然可见范围（PUBLIC / DEPT_ONLY / TEAM_ONLY）
- 跨范围共享白名单 → can_read
- 可读但非管理员的下载/分享受文档 allow 标志控制
- build_user_context 预计算上下文

采用 MagicMock 而非 DB 集成：
resolve_doc_access 的判定优先级是纯逻辑分支（Owner → 黑名单 → 超管 → 管理员 →
可见范围 → 共享 → 兜底），把 _is_denied / _has_active_share / build_user_context
mock 掉后即可隔离 DB 依赖，专注验证优先级顺序与边界铁律。
_visibility_allows_read 是纯函数（仅读 ctx 与 doc 字段），保留真实逻辑以验证可见性分支。
"""
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

import pytest

from apps.knowledge.access import (
    resolve_doc_access,
    build_user_context,
    _visibility_allows_read,
)
from apps.knowledge.models import VisibilityLevel


# ============================================================================
# Mock 工厂：构造无 DB 依赖的 user / doc / ctx
# ============================================================================
def _make_user(user_id=1, is_authenticated=True, is_super_admin=False,
               department_id=None, team_id=None):
    """构造 mock 用户，默认已认证、非超管

    is_super_admin 作为属性暴露，对齐 User 模型的 @property 语义。
    """
    user = MagicMock()
    user.id = user_id
    user.is_authenticated = is_authenticated
    user.is_super_admin = is_super_admin
    user.department_id = department_id
    user.team_id = team_id
    user.team = None
    return user


def _make_doc(doc_id=1, owner_id=999, team_id=None, dept_id=None,
              visibility_level=VisibilityLevel.TEAM_ONLY,
              allow_download=False, allow_share=False, node_path='/'):
    """构造 mock 文档，默认归属他人、TEAM_ONLY、不允许下载分享"""
    doc = MagicMock()
    doc.id = doc_id
    doc.owner_id = owner_id
    doc.team_id = team_id
    doc.dept_id = dept_id
    doc.visibility_level = visibility_level
    doc.allow_download = allow_download
    doc.allow_share = allow_share
    doc.has_block_user = False
    doc.has_resource_share = False
    node = MagicMock()
    node.path = node_path
    doc.node = node
    return doc


def _make_ctx(is_manager=False, managed_team_ids=None, visible_depts=None,
              visible_teams=None):
    """构造 build_user_context 等价的上下文 dict，避免触发真实 DB 查询"""
    return {
        'is_manager': is_manager,
        'is_team_leader': bool(managed_team_ids),
        'managed_team_ids': managed_team_ids or set(),
        'managed_dept_ids': set(),
        'visible_depts': visible_depts or set(),
        'visible_teams': visible_teams or set(),
    }


@contextmanager
def _patch_access_helpers(is_denied=False, has_share=False):
    """统一 patch 黑名单/共享判定函数，避免每个测试重复 with patch 样板

    默认 False：多数场景下用户不在黑名单、无主动共享，
    关闭这两个 DB 依赖后 resolve_doc_access 可作为纯逻辑函数测试。
    返回 (denied_mock, share_mock) 供调用方断言是否被调用。
    """
    with patch('apps.knowledge.access._is_denied', return_value=is_denied) as d, \
            patch('apps.knowledge.access._has_active_share', return_value=has_share) as s:
        yield d, s


# ============================================================================
# 未登录 / 未认证 → 全拒绝
# ============================================================================
@pytest.mark.unit
def test_anonymous_denied():
    """user=None 时所有权限标志应为 False（无认证即无访问）"""
    result = resolve_doc_access(None, _make_doc())
    assert result == {
        'is_owner': False, 'is_manager': False,
        'can_read': False, 'can_download': False, 'can_share': False,
    }


@pytest.mark.unit
def test_unauthenticated_denied():
    """is_authenticated=False 的用户等同匿名，全拒绝"""
    user = _make_user(is_authenticated=False)
    result = resolve_doc_access(user, _make_doc())
    assert not any(result.values())


# ============================================================================
# Owner 全权限（绕过黑名单——所有权原则）
# ============================================================================
@pytest.mark.unit
def test_owner_full_access():
    """文档 Owner 拥有全部权限，且不应触发黑名单判定（所有权原则：唯一绕过黑名单的角色）

    断言 _is_denied 未被调用：Owner 在黑名单判定之前短路返回，
    确保 Owner 永远不会被自己的文档拉黑。
    """
    user = _make_user(user_id=7)
    doc = _make_doc(owner_id=7)
    with _patch_access_helpers(is_denied=True) as (d, _s):
        # 即便 _is_denied 被人为置 True，Owner 仍应全权限（短路在黑名单之前）
        result = resolve_doc_access(user, doc, ctx=_make_ctx())
    assert result['is_owner'] is True
    assert result['can_read'] is True
    assert result['can_download'] is True
    assert result['can_share'] is True
    # 关键：黑名单判定不应被触发
    assert d.called is False


# ============================================================================
# 黑名单 Deny Override（对超管也生效）
# ============================================================================
@pytest.mark.unit
def test_blacklist_denies():
    """非 Owner 命中黑名单 → 全拒绝（Deny Override 铁律）"""
    user = _make_user()
    doc = _make_doc(owner_id=999)  # 非 Owner
    with _patch_access_helpers(is_denied=True):
        result = resolve_doc_access(user, doc, ctx=_make_ctx())
    assert not any(result.values())


@pytest.mark.unit
def test_super_admin_after_blacklist():
    """超管命中黑名单也应被拒绝——黑名单优先级高于 super_admin 快路径

    Deny Override 是不可变铁律，
    防止涉密/离职人员通过超管身份反向获取已剔除的文档。
    """
    user = _make_user(is_super_admin=True)
    doc = _make_doc(owner_id=999)  # 非 Owner，确保走到黑名单分支
    with _patch_access_helpers(is_denied=True):
        result = resolve_doc_access(user, doc, ctx=_make_ctx())
    assert result['can_read'] is False
    assert result['is_manager'] is False


# ============================================================================
# super_admin 系统级快路径（不绕过黑名单）
# ============================================================================
@pytest.mark.unit
def test_super_admin_no_blacklist():
    """超管未命中黑名单 → 全权限（绕过 permission_key，但 is_owner 仍按事实判定）"""
    user = _make_user(is_super_admin=True)
    doc = _make_doc(owner_id=999)  # 非 Owner
    with _patch_access_helpers(is_denied=False):
        result = resolve_doc_access(user, doc, ctx=_make_ctx())
    assert result['is_owner'] is False
    assert result['is_manager'] is True
    assert result['can_read'] is True
    assert result['can_download'] is True
    assert result['can_share'] is True


# ============================================================================
# 管理员 / 团队组长 → 全权限
# ============================================================================
@pytest.mark.unit
def test_kb_admin_full_access():
    """有 kb.manage_all 权限（ctx.is_manager=True）→ 全权限"""
    user = _make_user()
    doc = _make_doc(owner_id=999)
    with _patch_access_helpers(is_denied=False):
        result = resolve_doc_access(user, doc, ctx=_make_ctx(is_manager=True))
    assert result['is_manager'] is True
    assert result['can_read'] is True
    assert result['can_download'] is True
    assert result['can_share'] is True


@pytest.mark.unit
def test_team_leader_access():
    """团队组长：文档归属团队在其管理团队集合中 → 全权限"""
    user = _make_user()
    doc = _make_doc(owner_id=999, team_id=42)
    with _patch_access_helpers(is_denied=False):
        result = resolve_doc_access(
            user, doc, ctx=_make_ctx(is_manager=False, managed_team_ids={42}))
    assert result['is_manager'] is True
    assert result['can_read'] is True
    assert result['can_download'] is True


# ============================================================================
# 自然可见范围（visibility_level）
# ============================================================================
@pytest.mark.unit
def test_visibility_public():
    """PUBLIC 全局全员可读"""
    user = _make_user()
    doc = _make_doc(owner_id=999, visibility_level=VisibilityLevel.PUBLIC)
    with _patch_access_helpers(is_denied=False, has_share=False):
        result = resolve_doc_access(user, doc, ctx=_make_ctx())
    assert result['can_read'] is True


@pytest.mark.unit
def test_visibility_dept_only_match():
    """DEPT_ONLY：doc.dept_id 在用户可见部门集合中 → 可读"""
    user = _make_user()
    doc = _make_doc(owner_id=999, dept_id=10,
                    visibility_level=VisibilityLevel.DEPT_ONLY)
    with _patch_access_helpers(is_denied=False, has_share=False):
        result = resolve_doc_access(
            user, doc, ctx=_make_ctx(visible_depts={10}))
    assert result['can_read'] is True


@pytest.mark.unit
def test_visibility_dept_only_no_match():
    """DEPT_ONLY：doc.dept_id 不在可见部门集合 → 自然可见范围不命中，转共享判定"""
    user = _make_user()
    doc = _make_doc(owner_id=999, dept_id=99,
                    visibility_level=VisibilityLevel.DEPT_ONLY)
    with _patch_access_helpers(is_denied=False, has_share=False):
        result = resolve_doc_access(user, doc, ctx=_make_ctx(visible_depts={10}))
    assert result['can_read'] is False


@pytest.mark.unit
def test_visibility_team_only_match():
    """TEAM_ONLY：doc.team_id 在用户可见团队集合中 → 可读"""
    user = _make_user()
    doc = _make_doc(owner_id=999, team_id=42,
                    visibility_level=VisibilityLevel.TEAM_ONLY)
    with _patch_access_helpers(is_denied=False, has_share=False):
        result = resolve_doc_access(
            user, doc, ctx=_make_ctx(visible_teams={42}))
    assert result['can_read'] is True


# ============================================================================
# 跨范围共享白名单 → can_read
# ============================================================================
@pytest.mark.unit
def test_share_allows_read():
    """自然可见范围不命中但存在有效共享 → can_read=True"""
    user = _make_user()
    # TEAM_ONLY 且团队不在可见集合，自然范围不命中
    doc = _make_doc(owner_id=999, team_id=999,
                    visibility_level=VisibilityLevel.TEAM_ONLY)
    with _patch_access_helpers(is_denied=False, has_share=True):
        result = resolve_doc_access(user, doc, ctx=_make_ctx())
    assert result['can_read'] is True


@pytest.mark.unit
def test_no_access_denied():
    """无 Owner/管理员/可见范围/共享 → 兜底全拒绝"""
    user = _make_user()
    doc = _make_doc(owner_id=999, dept_id=99,
                    visibility_level=VisibilityLevel.DEPT_ONLY)
    with _patch_access_helpers(is_denied=False, has_share=False):
        result = resolve_doc_access(user, doc, ctx=_make_ctx(visible_depts={10}))
    assert not any(result.values())


# ============================================================================
# 可读但非管理员：下载/分享受文档 allow 标志控制
# ============================================================================
@pytest.mark.unit
def test_can_read_not_manager():
    """可读但非管理员：can_download/can_share 由文档 allow_download/allow_share 决定

    普通用户即使有读权限也不自动获得下载/分享权，
    防止通过共享读权限越权导出敏感文档。
    """
    user = _make_user()
    doc = _make_doc(owner_id=999, visibility_level=VisibilityLevel.PUBLIC,
                    allow_download=True, allow_share=False)
    with _patch_access_helpers(is_denied=False, has_share=False):
        result = resolve_doc_access(user, doc, ctx=_make_ctx())
    assert result['can_read'] is True
    assert result['is_manager'] is False
    assert result['can_download'] is True   # 受 doc.allow_download 控制
    assert result['can_share'] is False     # 受 doc.allow_share 控制


# ============================================================================
# _visibility_allows_read 纯函数直接验证
# ============================================================================
@pytest.mark.unit
def test_visibility_allows_read_public():
    """PUBLIC 不依赖 ctx 即可读"""
    doc = _make_doc(visibility_level=VisibilityLevel.PUBLIC)
    assert _visibility_allows_read(doc, _make_ctx()) is True


@pytest.mark.unit
def test_visibility_allows_read_dept_null_dept():
    """DEPT_ONLY 但 doc.dept_id 为 None → 不可读（防止空归属误放行）"""
    doc = _make_doc(dept_id=None, visibility_level=VisibilityLevel.DEPT_ONLY)
    assert _visibility_allows_read(doc, _make_ctx(visible_depts={10})) is False


# ============================================================================
# build_user_context 预计算上下文
# ============================================================================
@pytest.mark.unit
def test_build_user_context_none():
    """user=None 应返回 None（未登录无需计算上下文）"""
    assert build_user_context(None) is None


@pytest.mark.unit
def test_build_user_context_unauthenticated():
    """未认证用户应返回 None"""
    user = _make_user(is_authenticated=False)
    assert build_user_context(user) is None


@pytest.mark.unit
def test_build_user_context_authenticated():
    """已认证用户应返回含全部预期键的上下文 dict

    mock 的 4 个依赖函数：build_user_context 内部聚合了
    可管理部门/团队与部门祖先链，这些是 DB 查询，mock 后可断言聚合逻辑。
    """
    user = _make_user(user_id=5, department_id=10, team_id=None)
    user.is_super_admin = False

    with patch('apps.knowledge.access.get_user_managed_depts', return_value={20}) as m_depts, \
            patch('apps.knowledge.access.get_user_managed_teams', return_value={30}) as m_teams, \
            patch('apps.knowledge.access.get_user_dept_ancestors', return_value={10, 11}) as m_anc, \
            patch('apps.knowledge.access.has_permission', return_value=False) as m_perm:
        ctx = build_user_context(user)

    # 可见部门 = 主部门祖先链 {10,11} ∪ 可管理部门 {20}
    assert ctx['visible_depts'] == {10, 11, 20}
    assert ctx['managed_team_ids'] == {30}
    assert ctx['managed_dept_ids'] == {20}
    assert ctx['visible_teams'] == {30}
    assert ctx['is_manager'] is False  # 非超管且无 kb.manage_all
    assert ctx['is_team_leader'] is True  # 管理团队非空
    # 部门祖先链查询应被调用一次（主部门），团队为空不触发团队部门祖先查询
    m_anc.assert_called_once_with(10)
    m_perm.assert_called_once_with(user, 'kb.manage_all')
