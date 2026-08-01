"""
apps.users.perm_cache - RBAC 权限分层缓存（L1~L5）+ 延迟双删

缓存分层（TTL 统一 1 小时 = 3600s）：
- L1 perm:fn:{uid}            用户最终功能权限点集合（并集，Set<str>）
- L2 perm:scope:dept:{uid}    可见 dept_id 列表（List<int>，含默认归属）
- L3 perm:scope:team:{uid}    可见 team_id 列表（List<int>，含默认归属）
- L4 perm:scope:level:{uid}   最高数据范围等级（str：TEAM/DEPT/GLOBAL）
- L5 perm:doc:{res_type}:{res_id}   资源临时授权用户清单（List<{uid, access_level, expires_at}>）

uid=user_id。

关键约束：
- super_admin 不走缓存（系统级快路径，get 直接返回 None 让调用方走快路径，set 为 no-op）。
- set 时把 set 转为 list 存储（set 结构对 JSON 序列化不友好），get 时再转回 set。
- 失效采用"延迟双删"防并发脏写回填：立即删一次 → 异步延迟 0.8s 再删一次。
- LocMemCache 不支持 scan_iter/delete_pattern，所有 pattern 操作 try/except 兜底降级
  （参考 apps/users/signals.py 的 _invalidate_visibility_cache 写法）。
"""
import threading
import time

from celery import shared_task
from django.core.cache import cache
from loguru import logger

# 缓存 TTL：1 小时（与设计文档一致，覆盖典型工作日权限变更传播窗口）
CACHE_TTL = 3600
# 延迟双删间隔（秒）—— 800ms，覆盖主从复制延迟 + 业务回填窗口，过期前再清一次脏写
DELAYED_DELETE_SECONDS = 0.8


# ============================================================================
# Key 生成函数
# ============================================================================

def _key_l1(user_id):
    """L1 key：用户功能权限点集合"""
    return f'perm:fn:{user_id}'


def _key_l2(user_id):
    """L2 key：用户可见部门 id 列表"""
    return f'perm:scope:dept:{user_id}'


def _key_l3(user_id):
    """L3 key：用户可见团队 id 列表"""
    return f'perm:scope:team:{user_id}'


def _key_l4(user_id):
    """L4 key：用户最高数据范围等级"""
    return f'perm:scope:level:{user_id}'


def _key_l5(res_type, res_id):
    """L5 key：资源临时授权用户清单（按资源维度，非用户维度）"""
    return f'perm:doc:{res_type}:{res_id}'


# ============================================================================
# 可缓存性判定
# ============================================================================

def _is_cacheable(user):
    """判断用户是否可走缓存：未登录 / super_admin 一律不走缓存

    什么时候走到这里：所有 L1~L4 的 get/set 入口先调用本函数。
    - 未登录：无 user_id，无法建 key，且匿名鉴权另有快路径
    - super_admin：系统级快路径直接放行，缓存其权限集既无意义又易脏（super_admin 变更极低频）
    """
    if user is None:
        return False
    if not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_super_admin', False):
        return False
    return True


# ============================================================================
# L1~L5 读写封装
# ============================================================================

def get_perm_fn(user):
    """读取 L1：用户功能权限点集合

    输入：user 对象（需有 id / is_authenticated / is_super_admin）
    输出：set<str> 权限点集合；未命中或不可缓存返回 None
          （调用方收到 None 应回源 get_user_permissions(user)，不要把 None 当空集）
    什么时候走到：前端菜单/按钮渲染、鉴权中间件高频判定前先查缓存。
    """
    if not _is_cacheable(user):
        return None
    raw = cache.get(_key_l1(user.id))
    if raw is None:
        return None
    # 存储时把 set 转成了 list（set 不可 JSON 序列化），此处转回 set 去重
    return set(raw)


def set_perm_fn(user, perm_set):
    """写入 L1：用户功能权限点集合

    输入：perm_set 为 set<str>；存为 list 以兼容 JSON 序列化。
    super_admin / 未登录跳过（不缓存）。
    """
    if not _is_cacheable(user):
        return
    cache.set(_key_l1(user.id), list(perm_set or []), CACHE_TTL)


def get_scope_dept(user):
    """读取 L2：用户可见部门 id 集合

    输出：set<int>；未命中或不可缓存返回 None（调用方回源 get_user_managed_depts(user)）。
    什么时候走到：部门级数据检索过滤、文档可见范围判定前的缓存预取。
    """
    if not _is_cacheable(user):
        return None
    raw = cache.get(_key_l2(user.id))
    return set(raw) if raw is not None else None


def set_scope_dept(user, dept_ids):
    """写入 L2：用户可见部门 id 集合（set<int> 存为 list）"""
    if not _is_cacheable(user):
        return
    cache.set(_key_l2(user.id), list(dept_ids or []), CACHE_TTL)


def get_scope_team(user):
    """读取 L3：用户可见团队 id 集合

    输出：set<int>；未命中或不可缓存返回 None（调用方回源 get_user_managed_teams(user)）。
    什么时候走到：团队级数据检索过滤前的缓存预取。
    """
    if not _is_cacheable(user):
        return None
    raw = cache.get(_key_l3(user.id))
    return set(raw) if raw is not None else None


def set_scope_team(user, team_ids):
    """写入 L3：用户可见团队 id 集合（set<int> 存为 list）"""
    if not _is_cacheable(user):
        return
    cache.set(_key_l3(user.id), list(team_ids or []), CACHE_TTL)


def get_scope_level(user):
    """读取 L4：用户最高数据范围等级

    输出：str（TEAM/DEPT/GLOBAL）；未命中或不可缓存返回 None
          （调用方回源 get_user_data_scope_level(user)）。
    什么时候走到：检索层决定过滤粒度（全局级/部门级/团队级）前的缓存预取。
    """
    if not _is_cacheable(user):
        return None
    return cache.get(_key_l4(user.id))


def set_scope_level(user, level):
    """写入 L4：用户最高数据范围等级（str 枚举值，直接存）"""
    if not _is_cacheable(user):
        return
    cache.set(_key_l4(user.id), level, CACHE_TTL)


def get_perm_doc(res_type, res_id):
    """读取 L5：资源临时授权用户清单

    输入：res_type（KNOWLEDGE_BASE/KNOWLEDGE_NODE/DOCUMENT）、res_id
    输出：list<{uid, access_level, expires_at}>；未命中返回 None
          （调用方回源查 ResourceShare 表重建清单）。
    什么时候走到：文档/节点可见性判定时，先查该资源的临时授权清单缓存。
    """
    return cache.get(_key_l5(res_type, res_id))


def set_perm_doc(res_type, res_id, entries):
    """写入 L5：资源临时授权用户清单（list[dict]，直接存，后端负责序列化）"""
    cache.set(_key_l5(res_type, res_id), list(entries or []), CACHE_TTL)


# ============================================================================
# Redis pattern 扫描辅助（LocMem 兜底降级）
# ============================================================================

def _collect_by_pattern(pattern):
    """按 glob 模式收集匹配的缓存 key（仅 Redis 后端支持 scan_iter）

    返回 str key 列表。用于无法精确定位用户/资源的失效场景（组织树调整、USER 级共享变更等）。
    非 Redis 后端（LocMem）不支持 scan，返回空列表——开发环境降级，生产环境用 Redis；
    拿到空列表时调用方等价于无操作（LocMem 进程内缓存重启即清，可接受）。
    """
    keys = []
    try:
        from django_redis import get_redis_connection
        conn = get_redis_connection('default')
        for k in conn.scan_iter(pattern):
            # django_redis 原生连接返回 bytes，统一解码为 str 以兼容 cache.delete API
            keys.append(k.decode('utf-8') if isinstance(k, (bytes, bytearray)) else k)
    except ImportError:
        # 未安装 django_redis（非 Redis 后端），无法 scan，静默降级
        logger.debug(f'django_redis unavailable, cannot scan pattern: {pattern}')
    except Exception as e:
        logger.error(f'scan pattern failed {pattern}: {e}')
    return keys


# ============================================================================
# 延迟双删：Celery task + 立即删 + 调度延迟删
# ============================================================================

@shared_task
def delayed_delete_keys(keys):
    """延迟删除缓存 key（延迟双删的第 2 次删除）

    什么时候走到：由 invalidate_keys 通过 Celery 延迟 0.8s 调度执行。
    作用：清除"主从复制延迟窗口内被并发请求脏回填"的 key，保证最终一致。
    风险点：权限缓存若漏删会导致越权，故此任务必须可靠执行（Celery worker 需常驻）。
    """
    if not keys:
        return
    try:
        cache.delete_many(keys)
    except Exception as e:
        # 兜底逐个删，避免某后端 delete_many 异常导致整批漏删
        for k in keys:
            try:
                cache.delete(k)
            except Exception:
                pass
        logger.error(f'delayed_delete_keys fallback delete failed: {e}')


def invalidate_keys(keys):
    """立即删除 + 调度延迟双删

    策略（防并发脏写回填）：
    1. 立即 cache.delete_many(keys) —— 第 1 次删（应在 DB 事务提交后调用）
    2. 异步延迟 0.8s 再删一次 —— 第 2 次删，清除并发回填的脏数据

    延迟用 Celery task；Celery broker 不可用时降级为 daemon 线程
    （WSGI/ASGI 下线程不可靠，仅兜底，生产应保证 Celery 可用）。
    输入 keys 会过滤 None 项并去重，空列表直接返回。
    """
    # 过滤 None + 去重（保持顺序），保证传给 Celery 的 payload 干净
    keys = list(dict.fromkeys(k for k in (keys or []) if k))
    if not keys:
        return

    # 第 1 次删：立即
    try:
        cache.delete_many(keys)
    except Exception as e:
        for k in keys:
            try:
                cache.delete(k)
            except Exception:
                pass
        logger.error(f'invalidate_keys immediate delete failed: {e}')

    # 第 2 次删：延迟（防并发脏写回填）
    try:
        delayed_delete_keys.apply_async(args=(keys,), countdown=DELAYED_DELETE_SECONDS)
    except Exception as e:
        # Celery broker 不可用 —— 降级为线程，尽力而为（权限安全优先，宁可多删）
        def _delayed():
            time.sleep(DELAYED_DELETE_SECONDS)
            try:
                cache.delete_many(keys)
            except Exception:
                pass

        t = threading.Thread(target=_delayed, daemon=True)
        t.start()
        logger.warning(f'Celery unavailable, fallback to thread for delayed delete: {e}')


# ============================================================================
# 失效函数（对齐设计文档失效映射表）
# ============================================================================

def invalidate_user_perms(user_id):
    """失效某用户 L1~L4 全部个人缓存

    场景：用户调岗（dept_id/team_id 变）、被拉黑、个人授权变更等需要全量刷新时调用。
    直接用 user_id 精确建 key 一次性删（高效，走延迟双删）。
    """
    if not user_id:
        return
    # 精确建 key，走延迟双删
    keys = [
        _key_l1(user_id),
        _key_l2(user_id),
        _key_l3(user_id),
        _key_l4(user_id),
    ]
    invalidate_keys(keys)


def invalidate_role_perms(role_id):
    """失效所有持有该 role 用户的 L1（按角色反查用户，批量失效）

    场景：RolePermissionRel 变化（角色权限点新增/删除/启停）→ 持有该角色的所有用户
    的功能权限集过期，必须重算。
    为什么只失效 L1：data_scope 是 Role 字段，权限点变化不影响数据范围等级（L4），
    也不影响可见 dept/team（L2/L3 由属地授权表决定），故仅清 L1。
    三张授权表（全局/部门属地/团队属地）都可能绑定同一 role，全部反查避免漏失效。
    """
    if not role_id:
        return
    # 懒导入避免模块加载期与 models 产生循环依赖
    from .models import UserRoleRel, UserDeptScopeRel, UserTeamScopeRel

    keys = []
    # 三张授权表统一反查 user_id，构建各自 L1 key
    for qs in (
        UserRoleRel.objects.filter(role_id=role_id).values_list('user_id', flat=True),
        UserDeptScopeRel.objects.filter(role_id=role_id).values_list('user_id', flat=True),
        UserTeamScopeRel.objects.filter(role_id=role_id).values_list('user_id', flat=True),
    ):
        for uid in qs:
            keys.append(_key_l1(uid))
    if keys:
        invalidate_keys(keys)


def invalidate_resource_share(res_type, res_id, share_scope_type=None, share_scope_id=None):
    """失效资源 L5；USER 级共享另清该 user 的 L5 关联

    场景：ResourceShare 新增/撤销/过期。
    - 始终失效该资源 L5（perm:doc:{res_type}:{res_id}），重建时从 ResourceShare 表回源。
    - share_scope_type=USER：L5 按资源维度存储，无法精确定位该 user 出现在哪些资源的 L5 里，
      故 scan 全量 perm:doc:* 清除（保守过失效——权限安全优先于性能；USER 级共享变更低频，
      可接受。后续若加 user→resource 反向索引可改为精确失效）。
    """
    if not res_type or res_id is None:
        return
    # 第 1 步：精确失效该资源 L5（LocMem 也生效）
    invalidate_keys([_key_l5(res_type, res_id)])

    # 第 2 步：USER 级共享变更，全量清 L5 以清掉该 user 在其他资源 L5 中的关联记录
    if share_scope_type == 'USER':
        l5_keys = _collect_by_pattern('perm:doc:*')
        if l5_keys:
            invalidate_keys(l5_keys)


def invalidate_resource_block(res_type, res_id, blocked_user_id=None):
    """失效资源 L5 + 被拉黑人个人缓存

    场景：ResourceBlockList 新增/解封/到期。Deny Override 铁律下黑名单优先级最高，
    拉黑/解封必须立即生效，故同时清资源 L5 与被拉黑人 L1~L4（强制其所有鉴权决策重算）。
    - 失效对应 resource 的 L5：资源授权清单重建
    - 被拉黑人个人缓存（L1~L4）：blocked_user_id 给出时调用 invalidate_user_perms 全清，
      保守过失效——拉黑是高风险权限事件，宁可在该用户下次请求时全部重算
    """
    if not res_type or res_id is None:
        return
    invalidate_keys([_key_l5(res_type, res_id)])
    if blocked_user_id:
        # 被拉黑人是高风险权限事件，全量清 L1~L4 强制其下次鉴权重算
        invalidate_user_perms(blocked_user_id)


def invalidate_org_change():
    """失效 L2/L3/L4 全量（部门/团队树调整/软删）

    场景：部门或团队的增删改、树形结构调整、软删 —— 影响所有用户的可见 dept/team 集合
    与最高数据范围等级，但无法精确定位受影响用户，按前缀 scan 批量清。
    为什么不动 L1：组织树变化不改变角色权限点（功能权限），L1 无需失效。
    LocMem 后端 scan 降级为无操作（开发环境可接受，生产用 Redis）。
    """
    keys = []
    for pattern in (
        'perm:scope:dept:*',
        'perm:scope:team:*',
        'perm:scope:level:*',
    ):
        keys.extend(_collect_by_pattern(pattern))
    if keys:
        invalidate_keys(keys)
