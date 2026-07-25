# 权限体系优化 - 最终方案

---

## 一、角色定义（6 种）

| 角色 | code | 核心职责 |
|------|------|----------|
| 全局管理员 | `super_admin` | 全部配置权限、绕过双审直接发布、修改所有文档权限、可物理销毁文档 |
| 部门负责人 | `dept_manager` | 管辖本部门全部团队、查看本部门所有文档、审批扩大可见范围申请 |
| 团队组长 | `team_leader` | 本团队文档一审、管理文档、调整可见范围、发起/审批本团队共享申请、直接收回对外权限 |
| 文档审核员 | `compliance_reviewer` | 专职合规风控、文档敏感内容二审校验、无日常检索问答权限 |
| 普通员工 | `employee` | 检索权限内已审核文档、上传发起双审工单、发起权限申请 |
| 只读员工 | `readonly` | 检索已发布文档、发起 read 权限申请、禁止上传 |

---

## 二、RBAC 权限体系

### 格式

```
{module}:{action}:{scope}  例：knowledge:read:team
```

| 元素 | 可选值 |
|------|--------|
| module | `knowledge` / `user` / `audit` / `system` |
| action | `read` / `upload` / `manage` / `download` / `manage_users` / `config` |
| scope | `all` / `department` / `team` |

> `personal` scope 已删除。所有文档归属团队节点，无私有文档。

### 权限矩阵

| 权限项 | super_admin | dept_manager | team_leader | compliance_reviewer | employee | readonly |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|
| 检索问答 | all | all | all | ❌ | all | all |
| 上传 | all | all | all | all | all | ❌ |
| 文档管理（编辑/删除/可见范围/归档） | all | department | team | ❌ | ❌ | ❌ |
| 发起权限申请 | — | — | — | — | ✅ | ✅ |
| 审批权限变更 | all | department | team（本团队） | ❌ | ❌ | ❌ |
| 管理用户 | all | department | team | ❌ | ❌ | ❌ |
| 系统配置 | all | ❌ | ❌ | ❌ | ❌ | ❌ |

**约束：**
- `readonly` 仅可申请 `read`
- `employee` 可申请 `read` / `download`
- 后端校验 action 合法性

### 四张 RBAC 保留表

```
Role → RolePermission → Permission({module}:{action}:{scope})
User  → UserRole         ↗
```

`super_admin` 在所有 DRF 权限类和 `has_permission()` 函数中直接放行。

---

## 三、节点结构（固定 4 层）

```
一级：知识库 (kb)        root_type: "研发知识库" / "公共知识库"
  ├─ 二级：部门 (dept)   对应 Department
  │   └─ 三级：团队 (team)   对应 Team
  │       └─ 四级：业务分类 (category)   手动选择
```

**字段：**
- `root_type` — 支持多个知识库
- `path` — 冗余前缀，格式 `/kb_id/dept_id/team_id/cat_id/`
- `document_count` — 注解统计子节点文档数

**规则：**
- 上传自动填充前三层（当前用户的 kb → dept → team），仅四级分类手动选择
- 拥有某节点权限 = 拥有该节点下所有文档权限
- 调整可见范围不修改归属节点，资产归属永久不变

---

## 四、文档主表

```sql
knowledge_document
├─ id / uuid / title / file_name / file_type / file_size / file_hash
├─ file_path / mime_type / status / error_message / chunk_count / version
├─ kb_node_id       ← 一级知识库
├─ dept_node_id     ← 二级部门（归属，不可变）
├─ team_node_id     ← 三级团队（归属，不可变）
├─ category_node_id ← 四级业务分类
├─ owner_id / owner_team_id（快照，防团队变动权限漂移）
├─ visible_scope    ← team / dept / public
├─ secret_level     ← 1~4（4=绝密，禁止 public）
├─ audit_status     ← pending_team / pending_compliance / rejected / passed / archived / deleted
├─ has_deny_user    ← BOOLEAN 标志位
├─ allow_download   ← BOOLEAN（是否允许下载）
├─ allow_share      ← BOOLEAN（是否允许分享）
├─ is_logic_del / delete_time / restored_at / restored_by
├─ tags / extra / root_type
└─ created_at / updated_at
```

### 可见范围三档

| visible_scope | 含义 | 说明 |
|:---:|------|------|
| `team` | 仅归属团队 | 上传默认值 |
| `dept` | 归属全部门 | |
| `public` | 全公司公开 | secret_level=4 禁止 |

### 审批规则

| 操作方向 | 流程 |
|----------|------|
| 扩大（team→dept / team/dept→public） | 团队组长发起 → 部门负责人审批 → 通过生效 |
| 缩小（public→dept / dept→team） | 一键收回，无需审批，即时生效 |

---

## 五、文档双层审核

### 审核范围

**所有角色**（普通员工、团队组长、部门负责人）上传的文档均需走双层审核。仅 `super_admin` 上传可直接跳过。

### 审核链路

```
员工/组长/部门负责人提交
    ↓
系统自动预检（密钥/账号/隐私等高危内容 → 失败直接驳回）
    ↓
第一层：团队组长业务初审
  审核：文档归属、业务价值、初始可见范围合理性
  操作：通过 / 驳回 / 修正基础可见范围后流转
    ↓
第二层：文档审核员合规复审
  审核：代码脱敏、涉密信息、合规校验
  操作：通过 / 驳回 / 自动脱敏放行
    ↓
双审通过 → 自动解析/切片 → Chunk 继承归属与权限 → 写入存储/ES/向量库 → 参与检索
```

### 状态枚举

| 状态 | 含义 | 检索可见 |
|------|------|:---:|
| `pending_team` | 待团队组长一审 | ❌ |
| `pending_compliance` | 待合规二审 | ❌ |
| `rejected` | 审核驳回 | ❌ |
| `passed` | 双审通过 | ✅ |
| `archived` | 归档 | ✅（仅本团队+管理员） |
| `deleted` | 逻辑删除 | ❌ |

---

## 六、权限独立表（三张，物理删除，仅存生效数据）

### 6.1 黑名单表

```sql
CREATE TABLE doc_deny_user (
    id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT NOT NULL,
    uid BIGINT NOT NULL,
    create_by BIGINT,
    create_time TIMESTAMP DEFAULT NOW(),
    UNIQUE (doc_id, uid)
);
```

**联动：**
- 添加 → INSERT + 更新主表 `has_deny_user=TRUE`
- 移除 → DELETE + 若无其他黑名单则 `has_deny_user=FALSE`

### 6.2 个人白名单表

```sql
CREATE TABLE doc_allow_user (
    id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT NOT NULL,
    uid BIGINT NOT NULL,
    expire_time TIMESTAMP NULL,      -- NULL=永久
    audit_record_id BIGINT,          -- 关联审批工单
    create_by BIGINT,
    create_time TIMESTAMP DEFAULT NOW(),
    UNIQUE (doc_id, uid)
);
```

### 6.3 跨团队授权表

```sql
CREATE TABLE doc_cross_team (
    id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT NOT NULL,
    team_code VARCHAR(64) NOT NULL,
    expire_time TIMESTAMP NULL,
    audit_record_id BIGINT,
    create_by BIGINT,
    create_time TIMESTAMP DEFAULT NOW(),
    UNIQUE (doc_id, team_code)
);
```

### 6.4 定时过期

```python
def expire_permissions():
    now = now()
    for r in DocAllowUser.objects.filter(expire_time__lt=now):
        r.delete()
        OperationLog.objects.create(action='doc_grant_expire', ...)
    for r in DocCrossTeam.objects.filter(expire_time__lt=now):
        r.delete()
        OperationLog.objects.create(action='doc_grant_expire', ...)
```

---

## 七、审计日志表

### `knowledge_operation_log`（只追加，不删不改）

```python
ACTION_CHOICES = [
    # 文档
    ('doc_create', '上传文档'),
    ('doc_delete', '删除文档'),
    ('doc_visibility_change', '修改可见范围'),
    ('doc_download', '下载文档'),
    ('doc_reparse', '重新解析'),
    ('doc_restore', '恢复文档'),
    # 节点
    ('node_create', '创建节点'),
    ('node_update', '修改节点'),
    ('node_delete', '删除节点'),
    # 审核
    ('doc_audit_team_pass', '团队一审通过'),
    ('doc_audit_team_reject', '团队一审驳回'),
    ('doc_audit_compliance_pass', '合规二审通过'),
    ('doc_audit_compliance_reject', '合规二审驳回'),
    # 权限
    ('doc_grant', '授权（白名单/跨团队）'),
    ('doc_revoke', '撤销授权'),
    ('doc_grant_expire', '授权到期'),
    ('doc_deny_add', '添加黑名单'),
    ('doc_deny_remove', '移除黑名单'),
    ('doc_archive', '归档文档'),
    ('doc_physical_destroy', '物理销毁'),
]
```

每条绑定：操作人 + 工单 ID + 变更前后状态 JSON + IP + UA。

---

## 八、统一双轨申请/授权

### 申请单表

```sql
CREATE TABLE access_application (
    id BIGSERIAL PRIMARY KEY,
    applicant_id BIGINT,
    target_type VARCHAR(16),          -- doc / team / dept / all
    target_id BIGINT NULL,
    action VARCHAR(16),               -- read / download
    reason TEXT,
    status VARCHAR(16) DEFAULT 'pending',
    reviewer_comment TEXT,
    reviewed_by BIGINT,
    reviewed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP
);
```

**约束：** `readonly` 仅能申请 `read`；`employee` 可申请 `read`/`download`。

### 双轨流程

```
轨道 1：申请（拉）
  用户提交申请 → pending → 审批
  → approved → INSERT INTO doc_allow_user / doc_cross_team
  → 写审计日志

轨道 2：授权（推）
  组长/管理员直接 INSERT
  → 扩大范围/跨团队 → 需审批 → 走 AccessApplication
  → 缩小范围/回收 → 无需审批 → 直接 DELETE + 写日志
```

### 删除的旧表

| 删除 | 替代 |
|------|------|
| `UserCrossScopeAccess` | 并入 `AccessApplication`(target_type=team/dept) |
| `UserScopePermission` | 并入 RBAC 角色权限 + 申请流程 |
| `DocumentAccessRequest` | 并入 `AccessApplication`(target_type=doc) |
| `PermissionApplication` | 并入 `AccessApplication`(target_type=all) |

---

## 九、检索权限判定

### 前置过滤

```
is_logic_del = 0 AND audit_status IN ('passed', 'archived')
super_admin 跳过全部权限校验
```

### Step 1：SQL 粗筛（DocumentVector 冗余字段，单 SQL）

```sql
WHERE visible_scope = 'public'
   OR (visible_scope = 'dept' AND dept_node_id = user.dept_id)
   OR (visible_scope = 'team' AND team_node_id = user.team_id)
```

### Step 2：批量预查（2 次 SQL，仅对 Step 1 不匹配的候选集）

```python
# ① has_deny_user=True → 查 doc_deny_user
denied_ids = DocDenyUser.objects.filter(
    doc_id__in=docs_with_deny, uid=user.id
).values_list('doc_id', flat=True)

# ② visible_scope 不匹配 → 查 cross_team + allow_user
cross_team_doc_ids = DocCrossTeam.objects.filter(
    doc_id__in=remaining, team_code__in=user.teams,
    Q(expire_time__isnull=True) | Q(expire_time__gt=now)
).values_list('doc_id', flat=True)

allow_user_doc_ids = DocAllowUser.objects.filter(
    doc_id__in=remaining, uid=user.id,
    Q(expire_time__isnull=True) | Q(expire_time__gt=now)
).values_list('doc_id', flat=True)
```

### Step 3：逐条判定

```
① has_deny_user and doc.id in denied_ids  → 过滤
② visible_scope = 'public'                → 可见
   visible_scope = 'dept' and match dept   → 可见
   visible_scope = 'team' and match team   → 可见
③ doc.id in cross_team_doc_ids            → 可见
④ doc.id in allow_user_doc_ids            → 可见
⑤ 以上均不满足                            → 过滤
```

---

## 十、热点缓存

```
HotQaCache
├─ question_hash + root_type + visibility_scope
├─ cited_doc_ids: [123, 456]      ← 引用文档 ID
└─ answer + citations

命中流程：
  1. 命中 → 拿到 cited_doc_ids
  2. build_grants_map(user, cited_doc_ids) 批量验证
  3. 全部通过 → 返回
  4. 任一失败 → 失效，重新检索
```

---

## 十一、保留模块

| 模块 | 说明 |
|------|------|
| IP 风控中间件 | 白名单放行、黑名单拦截、过期自动解封 |
| 审计中间件 | 拦截 POST/PUT/DELETE，记录审计日志 |
| 慢请求中间件 | 记录 > 30s 请求 |
| 错误码体系 | 40001/40100/40300/40400/50000 |
| DocumentVector 冗余字段 | 单 SQL 向量检索 + 权限粗筛 |
| JWT 认证 | Access 8h / Refresh 7d |

---

## 十二、全表关系总览

```
┌──────────────────────────────────────────────┐
│                   RBAC 层                    │
│  Role → RolePermission → Permission          │
│  User → UserRole         ↗                  │
│  scope: all / department / team              │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│              KnowledgeNode (固定4层)          │
│  kb → dept → team → category                 │
└──────────────────────┬───────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────┐
│                Document 主表                  │
│  visible_scope / audit_status / has_deny     │
│  4 节点 ID / secret_level / is_logic_del     │
└──┬──────────┬──────────┬──────────┬─────────┘
   │          │          │          │
   ▼          ▼          ▼          ▼
┌───────┐ ┌───────┐ ┌────────┐ ┌──────────────┐
│deny   │ │allow  │ │cross   │ │ op_log       │
│仅生效 │ │仅生效 │ │_team   │ │ 只追加       │
│物理删 │ │物理删 │ │仅生效  │ │ 20 种 action │
└───────┘ └───────┘ └────────┘ └──────────────┘

┌──────────────────────────────────────────────┐
│             AccessApplication                │
│  双轨: 申请(拉) + 授权(推)                    │
│  target_type: doc / team / dept / all        │
│  action: read / download                     │
│  readonly 仅 read                            │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│          DocumentVector (检索冗余)            │
│  visible_scope / team_node_id / dept_node_id │
│  单 SQL: 向量检索 + 权限粗筛                  │
└──────────────────────────────────────────────┘

┌──────────────────────────────────────────────┐
│              HotQaCache                      │
│  + cited_doc_ids (命中后批量权限校验)         │
└──────────────────────────────────────────────┘
```

### 双层审核链路（所有人，除 super_admin）

```
员工/组长/部门负责人上传
    ↓
系统预检（高危内容 → 驳回）
    ↓
第一层：团队组长一审（归属/价值/可见范围）
    ↓
第二层：合规审核员二审（脱敏/合规）
    ↓
passed → 入库 → 可检索
```
