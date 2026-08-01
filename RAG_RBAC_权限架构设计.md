# 企业级 RBAC + 多租户 + 部门Team 权限架构设计

> 适用范围：**RAG 知识库平台权限体系**；向量库不在本次方案讨论范围内。

---

## 一、整体架构定位

### 核心架构思想：人事组织、角色能力、数据管辖范围 三层彻底解耦

1. **人事组织**：静态档案，决定员工归属（不变）
2. **角色模板**：功能权限点集合，只定义「能做什么」（通用模板、全局复用）
3. **数据管辖 Scope**：动态绑定，决定「能管哪些资源」（可跨组织授权）

### 适用业务规则（严格匹配业务需求）

- 一个员工：唯一租户、唯一主部门、唯一所属团队
- 员工可视边界：租户级 / 部门级 / 团队级
- 支持跨团队、跨部门代管权限（大厂最小权限设计）
- **RAG 文档严格隔离**：
  - 租户级：看本租户所有部门
  - 部门级：只看本部门所有团队，看不到其他部门
  - 团队级：只看自己管辖团队，看不到本部门其他团队
- **无个人级文档**：所有文档必选团队 / 部门 / 租户（公开）三级之一
- **跨范围访问必须申请**：Owner 或组织管理员批准，Owner 可随时撤销

---

## 二、层级结构

```
Tenant（租户）
  └── Dept（部门，树形结构，parent_dept_id 自关联）
        └── Team（团队，隶属于单一部门）
              └── User（员工：单租户 / 单主部门 / 单所属团队）
```

---

## 三、角色体系设计（内置系统角色，不可删除）

> 只维护 **固定 7 个内置角色模板**，全局复用，不重复建角色。  
> 角色 = **一组命名的权限点集合**（绑定关系见第四章）。

### 角色定义与权限边界

| 角色 Key | 角色名称 | 角色类型 | 数据范围 | 能力说明 |
|---|---|---|---|---|
| `tenant_super_admin` | 租户超级管理员 | 全局租户角色 | 租户级 | 租户最高权限，全租户人员/文档/配置全权管理 |
| `tenant_user_admin` | 租户人员管理员 | 全局租户角色 | 租户级 | 仅管理租户组织、人员、部门、团队，不可操作文档 |
| `tenant_kb_admin` | 租户文档管理员 | 全局租户角色 | 租户级 | 仅管理全租户知识库、文档资源，不可管理人 |
| `tenant_compliance_admin` | 租户合规管理员 | 全局租户角色 | 租户级 | 全租户只读审计、日志查看，无修改权限 |
| `dept_manager` | 部门经理 | 部门管理角色 | 部门级 | 管理指定部门人员、部门级知识库，可审批上推文档至租户级 |
| `team_leader` | 团队组长 | 团队管理角色 | 团队级 | 管理指定团队人员、团队级知识库 |
| `staff_user` | 普通员工 | 普通角色 | 团队级 | 仅查看/上传本人所属团队文档，无管理权限 |

### 角色类型枚举

| 枚举值 | 含义 |
|---|---|
| `GLOBAL_TENANT` | 全局租户角色（授权无需绑定 Scope） |
| `DEPT_SCOPE` | 部门管理角色（授权必须绑定具体 Dept ID） |
| `TEAM_SCOPE` | 团队管理角色（授权必须绑定具体 Team ID） |
| `NORMAL_USER` | 普通角色（默认随人事归属生效） |

### 超级管理员配置规范（飞书 / 阿里云标准）

| 项目 | 强制规则 |
|---|---|
| 最少数量 | **至少 2 人**（单人 = 单点故障锁死风险） |
| 推荐数量 | **2 ~ 3 人**（超过 3 人反而扩大风险面） |
| 变更规则 | 新增 / 撤销 super_admin：**必须 1 人发起 + 另 1 人批准**（双人复核） |

---

## 四、功能权限点体系（Permission Point）

> 解决「谁能做什么不在代码里硬编码」问题。代码只判断 `permission_key`，永不判断 `role_key`。

### 4.1 设计原则

```
❌ 错误：if user.role in ['tenant_super_admin', 'tenant_kb_admin']: upload_doc()
✅ 正确：if user.has_permission('kb.document.upload'): upload_doc()
```

### 4.2 Permission 权限点表

| 字段 | 说明 |
|---|---|
| `permission_key` | 唯一标识，三段式 `模块.资源.动作`，例：`kb.document.upload` |
| `permission_name` | 显示名称，例：「上传文档」 |
| `module` | 所属模块：`org` / `user` / `kb` / `system` / `compliance` |
| `is_built_in` | 是否系统内置 |

### 4.3 内置权限点清单（节选）

| permission_key | 名称 | 挂在角色举例 |
|---|---|---|
| `org.tenant.config` | 修改租户配置 | `tenant_super_admin` |
| `org.dept.create` | 创建部门 | `tenant_super_admin`, `tenant_user_admin` |
| `org.team.create` | 创建团队 | `tenant_super_admin`, `tenant_user_admin`, `dept_manager` |
| `user.invite` | 邀请员工 | `tenant_user_admin`, `dept_manager` |
| `role.grant.global` | 授予全局角色 | `tenant_super_admin`（需另一个 super_admin 批准） |
| `role.grant.dept` | 授予部门角色 | `tenant_super_admin`, `tenant_user_admin` |
| `role.grant.team` | 授予团队角色 | `tenant_user_admin`, `dept_manager` |
| `kb.document.upload` | 上传文档 | `tenant_kb_admin`, `dept_manager`, `team_leader`, `staff_user` |
| `kb.document.delete` | 删除文档 | `tenant_kb_admin`, `dept_manager`, `team_leader` |
| `kb.document.promote_to_dept` | 文档上推至部门级 | `team_leader` |
| `kb.document.promote_to_tenant` | 文档上推至租户级（公开） | `dept_manager` |
| `kb.document.access.approve` | 批准他人文档访问申请 | `tenant_kb_admin`, `dept_manager`, `team_leader`, Owner 本人 |
| `audit.log.view` | 查看审计日志 | `tenant_super_admin`, `tenant_compliance_admin` |

### 4.4 RolePermissionRel（角色-权限点绑定表）

| 字段 | 说明 |
|---|---|
| `role_key` | FK → Role.role_key |
| `permission_key` | FK → Permission.permission_key |
| `tenant_id` | 租户 ID |

> **7 个内置角色的绑定关系 = 系统初始数据**，启动时种子写入，不可删除（但自定义角色可复用该表）。

---

## 五、核心设计精髓（大厂核心思想）

### 1. 彻底解耦

- **人事归属 ≠ 权限管辖范围**
- 用户在 A 团队，可以被授权管理 B 团队 / B 部门
- 角色只是「能力模板（权限点集合）」，不带任何组织 ID
- 代码判断权限点，不判断角色名（角色可以随意增减映射，代码零改动）

### 2. 两类授权模式（行业标准）

#### ① 全局角色授权

租户超级管理员、租户人员管理员、租户文档管理员、租户合规管理员

> 只需：**用户 + 角色**（+ 可选有效期）

#### ② 属地管理授权

部门经理、团队组长

> 需要：**用户 + 角色 + 具体管辖资源 ID**（+ 可选有效期）

### 3. 权限合并规则（固定、无歧义）

- **功能权限**：所有角色取 **权限点并集**
- **数据权限**：取 **最高范围优先级**

  > `租户级 > 授权部门级 > 授权团队级 > 本人团队级`

### 4. 授权提升防护（Privilege Escalation Guard）—— 强制约束

| 授权者身份 | 可授予的角色上限 | 可授予的管辖范围上限 |
|---|---|---|
| `tenant_super_admin` | 所有角色（含另一个 super_admin，需双人审批） | 全租户任意 |
| `tenant_user_admin` | 除 `tenant_super_admin` 外的全局角色 + dept/team 角色 | 全租户任意组织 |
| `dept_manager` | `team_leader` / `staff_user` | 仅限本人管辖的部门及子部门 |
| `team_leader` | `staff_user` | 仅限本人管辖团队 |
| `staff_user` | ❌ 不可授予任何角色 | —— |

> 所有 grant / revoke 接口 **前置校验** 本矩阵，不通过直接拦截。

---

## 六、数据库模型设计（Django 最终版）

### 公共字段（所有表统一追加）

| 字段 | 类型 | 说明 |
|---|---|---|
| `created_at` | `DateTime` | 创建时间 |
| `updated_at` | `DateTime` | 更新时间 |
| `created_by` | `FK(User)` | 创建人 |
| `updated_by` | `FK(User)` | 更新人 |
| `is_deleted` | `Boolean` | 软删除标记（见第十一章） |
| `deleted_at` | `DateTime NULL` | 删除时间 |

---

### 6.1 组织架构 & 知识库节点树模型

#### 6.1.1 人事组织架构模型

| 模型 | 核心字段 | 说明 |
|---|---|---|
| `Tenant` | `tenant_id`, `name`, `status` | 租户 |
| `Dept` | `dept_id`, `tenant_id`, `parent_dept_id`（树形自关联）, `name` | 部门（租户内树形） |
| `Team` | `team_id`, `tenant_id`, `dept_id`（强制 FK）, `name` | 团队（隶属于单一部门） |
| `User` | `user_id`, `tenant_id`, `dept_id`, `team_id`, `email`, `status` | 员工：单租户、单主部门、单所属团队 |

#### 6.1.2 KnowledgeNode（知识库节点树模型）—— 团队下的版本/语言/技术栈等细分分类

> 场景：团队级可见范围确定后，团队内部还可以继续分层。
> 例：团队「后端组」下建节点 `版本/v1.0`、`版本/v2.0`、`语言/Java`、`语言/Python`；文档必须挂在**某个叶子节点**上。
> 节点授权默认**向下继承**：给「语言/Java」开权限 → 自动拥有其下所有子节点 + 子节点下所有文档的权限。

**节点树选型：路径枚举（Materialized Path）+ parent_id 双写模式。**

| 方案 | 场景适配 | 说明 |
|---|---|---|
| `parent_id` 自关联 | UI 树形渲染 / 找直接父子 | 简单直观 |
| `path` 路径枚举列 | **鉴权继承判断（核心）** | 例：`/root/backend/lang/java/` 查询 Java 下所有后代：`WHERE path LIKE '/root/backend/lang/java/%'`，一次前缀索引搞定，不需要递归 CTE |
| 闭包表（ancestor-descendant） | 不推荐 | 写复杂、占空间；MySQL 8+ CTE + 路径枚举已经覆盖绝大多数场景 |

| 字段 | 说明 |
|---|---|
| `node_id` | 节点主键 |
| `tenant_id` | 租户 ID |
| `kb_id` | 所属知识库 ID（可选；多知识库场景用，单知识库可省略） |
| `parent_node_id` | 父节点 ID（根节点 = NULL / 0） |
| `node_name` | 节点名，如「Java」「版本2.0」 |
| `path` | **路径枚举（核心）**：从根到本节点的 node_id 路径，分隔符 `|`；例 `|1|5|12|78|`（首尾加分隔符避免前缀误匹配） |
| `depth` | 节点层级深度（根 = 1），便于前端折叠渲染 |
| `owner_user_id` | 节点 Owner（默认为创建人，可作为节点级共享申请的审批人） |
| `sort_order` | 同级排序 |
| `is_deleted` | 软删除标记 + `deleted_at` |

> **为什么 path 首尾加分隔符？** 经典反坑：如果不加，节点 `|12|` 和节点 `|123|` 会用 `LIKE '%|12%'` 误匹配；正确写法 `path LIKE '%|12|%'` 或 `path LIKE '|1|5|12|%'`。

**关键索引：**
```sql
CREATE INDEX idx_node_tree_path      ON knowledge_node(tenant_id, path(255));  -- 前缀索引（MySQL 前缀长度按路径最大长度设）
CREATE INDEX idx_node_tree_parent    ON knowledge_node(tenant_id, parent_node_id);
CREATE UNIQUE INDEX idx_node_unique  ON knowledge_node(tenant_id, kb_id, parent_node_id, node_name);  -- 同级同名不允许
```

**Document 归属字段追加**（详见第八章资源层级标记）：每个 `document` 必须 `knowledge_node_id FK → knowledge_node.node_id`（文档必须挂在某个节点上，根节点也可以）。

---

### 6.2 角色与权限点模型

#### Role（角色表）

| 字段 | 说明 |
|---|---|
| `tenant_id` | 租户 ID |
| `role_key` | 角色唯一标识（全局唯一索引，内置 7 个固定） |
| `role_type` | 枚举：GLOBAL_TENANT / DEPT_SCOPE / TEAM_SCOPE / NORMAL_USER |
| `data_scope` | 数据权限等级枚举 |
| `is_built_in` | 是否系统内置（内置 = 不可删除） |

#### Permission（权限点表） & RolePermissionRel（角色-权限点绑定）

> 见第四章 4.2 / 4.4。

---

### 6.3 三张授权绑定核心表（大厂标准）

> ⚠️ **所有授权表统一加有效期 3 字段**：
> | 字段 | 说明 |
> |---|---|
> | `effective_from` | `DateTime NULL` —— NULL = 立即生效 |
> | `expires_at` | `DateTime NULL` —— NULL = 永久有效 |
> | `status` | 枚举：`PENDING`（待审批）/ `ACTIVE`（生效中）/ `EXPIRED`（已过期）/ `REVOKED`（已撤销） |

#### （1）UserRoleRel —— 全局角色绑定表

```
UserRoleRel(user, role_key, tenant_id, effective_from, expires_at, status)
```

> 用于：4 个全局租户角色（super_admin / user_admin / kb_admin / compliance_admin）

#### （2）UserDeptScopeRel —— 部门管辖绑定表

```
UserDeptScopeRel(user, role_key, dept_id, tenant_id, effective_from, expires_at, status)
```

> 用于：给用户授予「指定某部门」的 `dept_manager` 权限

#### （3）UserTeamScopeRel —— 团队管辖绑定表

```
UserTeamScopeRel(user, role_key, team_id, tenant_id, effective_from, expires_at, status)
```

> 用于：给用户授予「指定某团队」的 `team_leader` 权限

> **关键**：取消管理权 = 软删 / 改 `status = REVOKED`，**不改动人事架构**。

---

### 6.4 RAG 文档跨范围访问授权模型

> **RAG 场景核心原则（大厂统一做法）**：
> - **无权限文档直接不召回** —— 用户提问 → 检索 → 鉴权过滤 → 只返回有权限的文档参与生成。用户**感知不到**被过滤的文档，**不提供「申请权限」入口**。
> - **权限由管理端统一配置**（Owner / 管理员主动共享），召回层只做过滤，不做交互式申请。
> - 对标：飞书智能助手 / 钉钉 AI / 字节豆包企业版 / AWS Q / Azure Copilot —— 均为「管理端授权 + 召回层过滤」，无用户端申请流程。
>
> 因此本方案只有 **一类跨范围授权机制**：
> - **主动授权模式（ResourceShare）**：Owner / 管理员直接指定「哪些额外部门 / 团队 / 个人」能看 —— 管理端统一配置，召回层直接生效
>
> ~~被动申请模式（ResourceAccessRequest）~~ —— **RAG 场景不需要**。用户看不到无权限文档，没有"发现→申请"的交互入口。若未来有知识库浏览（非 AI 召回）场景，可作为扩展项再加。

#### （4-a）ResourceShare —— 资源主动共享表（一个表支持部门/团队/个人）

> **选型说明：一个表 + `share_scope_type` 枚举（不是部门一张、团队一张、个人一张）。**
>
> | 维度 | ❌ 3 张分离表（doc_dept_share / doc_team_share / doc_user_share） | ✅ **统一表 + share_scope_type 枚举（大厂标准）** |
> |---|---|---|
> | 查询次数 / round-trip | 必须 `UNION ALL` 3 段子查询 → 3 张表各自独立扫描 | **1 次 SQL**，3 种类型用 `WHERE (... OR ... OR ...)` 组合 |
> | 是否可「命中即停」 | ❌ 不行。3 段 UNION 必须全部执行完才能合并结果 | ✅ **可以**。加 `LIMIT 1` 后，数据库在第一种命中类型（如 USER）就停止扫描（这是实际性能差异最大的点） |
> | 回表次数 | 3 次（3 张表各一次） | **0 次（覆盖索引包含全部需要字段，完全不回表）** |
> | 扩展第 4 种类型（角色组 / 外部用户） | 新建第 4 张表 + 所有 SQL 加第 4 段 UNION + 代码多处修改 | 只加 `share_scope_type` 枚举值，**SQL 零改动** |
> | 代码复杂度 | 每新增一种类型，查询 / 写入 / 撤销 / 有效期扫描逻辑全部 * 2 | 逻辑完全共用，新增类型只改枚举 + 前端选择器 |

> **常见性能顾虑答疑：**
> - 问：共用一个表，数据量大了会不会慢？
>   答：不会。DB 查询速度 ≠ 总行数，≈ 索引命中后实际扫描行数。前 3 列复合索引 `(tenant_id, resource_type, resource_id)` 直接把扫描量收敛到**「该资源下的几十条共享记录」**，10 亿行表扫 50 行 和 10 万行表扫 50 行，耗时同为毫秒级。
> - 问：部门 → 团队 → 个人，不是也要判断 3 次吗？
>   答：**不是 3 次查询，是 1 次 SQL 里 3 个 OR 分支**。DB 优化器会一次性合并 3 段索引区间扫描返回，代码只拿一次结果，和"代码里发 3 次请求串行判断"完全是两码事。加上 `LIMIT 1`，命中 USER 分支时甚至根本不会扫描 DEPT/TEAM 的那部分。
> - 问：现在多了 KNOWLEDGE_BASE / KNOWLEDGE_NODE，表继续变大怎么办？
>   答：同理，复合索引首 3 列直接收敛到目标资源。更关键的是，**节点级授权默认向下继承**：授权到「Java 节点」= 自动授权到 Java 子节点、孙节点和挂在其下所有文档（用 KnowledgeNode.path 前缀匹配一次搞定，不需要递归）。

> **索引设计（性能核心 —— 没有这些索引 = 真的慢，有了 = 碾压三表方案）：**
> ```sql
> -- 方向 A（90% 场景）：判断「某资源 → 当前用户能不能看」—— 这是一个【覆盖索引】
> CREATE INDEX idx_share_resource_lookup ON resource_share (
>     tenant_id,          -- 1. 先切租户（多租户首列必须放）
>     resource_type,      -- 2. KNOWLEDGE_BASE / KNOWLEDGE_NODE / DOCUMENT
>     resource_id,        -- 3. 资源ID（前3列 = 直接收敛到该资源的全部共享）
>     share_scope_type,   -- 4. DEPT / TEAM / USER
>     share_scope_id,     -- 5. 对应ID，配合 IN(...)
>     status,             -- 6. ACTIVE 过滤
>     effective_from,     -- 7~8：有效期过滤
>     expires_at
> );
>
> -- 方向 B（10% 场景）：反向查「某用户能看到哪些被共享的资源（含节点+文档）」
> CREATE INDEX idx_share_user_lookup ON resource_share (
>     tenant_id, share_scope_type, share_scope_id,
>     status, resource_type, resource_id, expires_at
> );
> ```
>
> **对应 SQL 示例（只发一次请求 + LIMIT 1 命中即停）：**
> ```sql
> -- 场景 1：判断某 DOCUMENT 能不能看（同之前）
> SELECT 1 FROM resource_share
>  WHERE tenant_id=1001 AND resource_type='DOCUMENT' AND resource_id=8888
>    AND ((share_scope_type='DEPT' AND share_scope_id IN (1,3,7))
>      OR (share_scope_type='TEAM' AND share_scope_id IN (9,12))
>      OR (share_scope_type='USER' AND share_scope_id=5566))
>    AND status='ACTIVE' AND effective_from<=NOW() AND (expires_at IS NULL OR expires_at>NOW())
>  LIMIT 1;
>
> -- 场景 2：判断某 KNOWLEDGE_NODE 能不能看（节点本身 + 所有祖先节点「向上查权限」）
> -- 已知当前节点 path='|1|5|12|'，祖先链 = 节点 12 + 节点 5 + 节点 1，一次 IN 全搞定
> SELECT 1 FROM resource_share
>  WHERE tenant_id=1001 AND resource_type='KNOWLEDGE_NODE' AND resource_id IN (12, 5, 1)
>    AND ((share_scope_type='DEPT' AND share_scope_id IN (1,3,7)) OR ... )
>    AND status='ACTIVE' AND ...
>  LIMIT 1;
> ```
>
> ❌ 对比三表方案 SQL（无法命中即停，必须 3 段 UNION 全部执行完）：
> ```sql
> SELECT resource_id FROM doc_dept_share WHERE doc_id=8888 AND dept_id IN (1,3,7) AND status='ACTIVE' ...
> UNION ALL
> SELECT resource_id FROM doc_team_share WHERE doc_id=8888 AND team_id IN (9,12) AND status='ACTIVE' ...
> UNION ALL
> SELECT resource_id FROM doc_user_share WHERE doc_id=8888 AND user_id=5566 AND status='ACTIVE' ...;
> ```

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `tenant_id` | 租户 ID |
| `resource_type` | 枚举：**`KNOWLEDGE_BASE` / `KNOWLEDGE_NODE` / `DOCUMENT`**（三选一；节点继承规则见第八章） |
| `resource_id` | 资源 ID：kb_id / node_id / doc_id（逻辑外键，写入前校验存在性） |
| `share_scope_type` | **共享对象类型**：`DEPT` / `TEAM` / `USER` ← 用这个字段统一区分 |
| `share_scope_id` | **共享对象 ID**：dept_id 或 team_id 或 user_id（逻辑外键，写入前应用层校验存在性） |
| `access_level` | 枚举：`READ` / `EDIT`（可按业务只开放 READ） |
| `inherit_mode` | **节点级专属**：枚举 `ALL_DESCENDANTS`（默认 = 授权本节点+所有子节点+子节点下文档）/ `NODE_ONLY`（只授权本节点本身） |
| `granted_by` | 授予人 `user_id`（Owner 或具备 share 权限的管理员） |
| `granted_at` | 授予时间 |
| `effective_from` | `DateTime NULL` —— NULL = 立即生效 |
| `expires_at` | `DateTime NULL` —— NULL = 永久有效（到期自动失效，见定时任务） |
| `status` | 枚举：`ACTIVE` / `EXPIRED` / `REVOKED` |
| `revoked_by` | 撤销人 `user_id` |
| `revoked_at` | 撤销时间 |

> 唯一约束：`UNIQUE(tenant_id, resource_type, resource_id, share_scope_type, share_scope_id)`（配合 `status` 判断活跃，REVOKED 后可重授）  
> 同一个活跃共享对象不能重复授权（撤销后重新授予 = 产生新的历史记录）。
>
> **节点级继承说明**：`resource_type = KNOWLEDGE_NODE` + `inherit_mode = ALL_DESCENDANTS` 时，含义是「该节点 + 该节点所有后代节点（不限深度）+ 后代节点下挂的所有文档」自动获得该共享。鉴权时通过 `KnowledgeNode.path` 前缀匹配一次性搞定（不需要递归 CTE）。

#### （4-b）ResourceBlockList —— 访问黑名单（**仅支持个人，Deny Override 铁律优先级最高**）

> **为什么需要独立的黑名单表（而不是在 ResourceShare 里加 `access_level=DENY`）？**
> - **Deny > Allow 是不可变的铁律**：共享给了全部门 100 人，但有 1 人即将离职/被涉密/从项目剔除 —— 你希望「哪怕他在 5 个白名单里，也一律拒绝」。这类 Deny 必须走独立表、独立缓存、独立鉴权优先级，**绝不和 Allow 混表**，避免 SQL 逻辑判断顺序出错导致 Deny 被覆盖的致命事故。
> - **反坑：黑名单只对个人，不对部门/团队** —— 部门/团队不想给权限，直接从共享列表里移除即可；拉黑是"个人级精准剔除"，避免「拉黑了一个部门=整个部门 50 人都没权限」的灾难性误操作。

| 字段 | 说明 |
|---|---|
| `id` | 主键 |
| `tenant_id` | 租户 ID |
| `resource_type` | 枚举：`KNOWLEDGE_BASE` / `KNOWLEDGE_NODE` / `DOCUMENT`（和 ResourceShare 完全一致） |
| `resource_id` | 资源 ID：kb_id / node_id / doc_id |
| `blocked_user_id` | **被封禁个人 user_id**（唯一支持的作用域 = 个人，无部门/团队） |
| `block_inherit_mode` | **节点级专属**：枚举 `ALL_DESCENDANTS`（默认 = 拉黑该节点 + 所有子节点 + 子节点下所有文档）/ `NODE_ONLY`（只拉黑该节点本身，不影响子节点） |
| `reason` | **拉黑理由（必填，文本审计）** —— 如「已离职」「涉密项目剔除」「权限回收等待流程中临时封禁」 |
| `blocked_by` | 操作人 user_id（Owner 或管理员） |
| `blocked_at` | 封禁时间 |
| `effective_from` | `DateTime NULL` —— NULL = 立即生效 |
| `expires_at` | `DateTime NULL` —— NULL = 永久封禁；可设置临时封禁（如 7 天后自动解封） |
| `status` | 枚举：`ACTIVE`（封禁中）/ `EXPIRED`（到期自动解封）/ `REVOKED`（管理员手动解封） |
| `revoked_by` / `revoked_at` | 解封人 / 解封时间 |

> **索引设计（性能核心）**：
> ```sql
> -- 鉴权 99% 方向：给定"资源（或节点祖先链）+ 用户"→ 有没有命中黑名单？—— 覆盖索引，0 回表
> CREATE INDEX idx_block_check ON resource_block_list (
>     tenant_id, resource_type, resource_id, blocked_user_id,
>     status, effective_from, expires_at, block_inherit_mode
> );
> -- 反查：某用户被哪些资源拉黑了
> CREATE INDEX idx_block_user ON resource_block_list (
>     tenant_id, blocked_user_id, status, resource_type, resource_id
> );
> ```
>
> **唯一约束**：`UNIQUE(tenant_id, resource_type, resource_id, blocked_user_id)` —— 同一人对同一资源不能重复封禁。
>
> **⚠️ 不可动摇的铁律（Deny Override）**：**只要命中 ResourceBlockList，无论在多少个白名单（visibility / Share / AccessRequest / 管理员全局角色）里，一律 403 拒绝**。这是鉴权判定的 **第 0 步**，在所有白名单判定之前执行。

---

### 6.5 审批工单模型（授权变更必经流程）

#### （5）PermissionApprovalTicket —— 权限配置审批工单

| 字段 | 说明 |
|---|---|
| `ticket_no` | 工单唯一号 |
| `tenant_id` | |
| `applicant_id` | 发起人 `user_id` |
| `target_user_id` | 被授权 / 被撤销对象 |
| `change_type` | `GRANT` / `REVOKE` / `SCOPE_CHANGE` / `EXPIRE_EXTEND` |
| `role_key` | 涉及角色 |
| `scope_type` | `TENANT` / `DEPT` / `TEAM` / `NONE` |
| `scope_id` | `dept_id` 或 `team_id`（scope_type 对应） |
| `effective_from` / `expires_at` | 本次申请的有效期 |
| `approval_chain` | `JSON`：`[{approver_id, status, approved_at, comment}, ...]`（审批链快照） |
| `current_step` | 当前审批节点索引（int） |
| `status` | `PENDING` / `APPROVED` / `REJECTED` / `CANCELLED` / `EXECUTED` |
| `approved_at` | 最终通过时间 |
| `executed_at` | **审批通过后真正写入授权表的时间**（异步 worker 执行） |

> 所有授权表的 `status = PENDING` 记录，**只有当工单状态 = EXECUTED 后，才由 worker 改为 ACTIVE**。  
> 审批工单 **永不删除**，只改状态。

---

### 6.6 审计日志模型

#### （6）PermissionAuditLog —— 操作审计（只追加，永不改/删）

| 字段 | 说明 |
|---|---|
| `log_id` | 雪花 ID |
| `tenant_id` | |
| `actor_user_id` | 操作人 |
| `action` | 操作类别：见下方清单 |
| `target_type` | 操作对象类型：`USER` / `ROLE` / `DEPT` / `TEAM` / `KNOWLEDGE_BASE` / **`KNOWLEDGE_NODE`** / `DOCUMENT` / `TICKET` / `LOGIN` |
| `target_id` | 对象 ID |
| `target_user_id` | 若对象是人（如被授权人），记这里便于检索 |
| `role_key` | 涉及角色（有则填） |
| `scope_type` / `scope_id` | 涉及范围（有则填） |
| `before_snapshot` | `JSON`：变更前快照（无则 null） |
| `after_snapshot` | `JSON`：变更后快照（无则 null） |
| `result` | `SUCCESS` / `FAIL` + 失败码 |
| `ip_address` | 客户端 IP |
| `user_agent` | |
| `created_at` | 事件时间（**按此字段分库分表/冷热归档**） |

#### 必须记日志的 action 清单（全覆盖）

| 分类 | action 枚举值 |
|---|---|
| 组织架构 | `DEPT_CREATE` `DEPT_UPDATE` `DEPT_DELETE` `TEAM_CREATE` `TEAM_UPDATE` `TEAM_DELETE` `USER_INVITE` `USER_TRANSFER` `USER_LEAVE` |
| 知识库节点 | `NODE_CREATE` `NODE_MOVE`（改父节点，path 重算）`NODE_RENAME` `NODE_DELETE`（软删） |
| 权限配置 | `ROLE_GRANT` `ROLE_REVOKE` `SCOPE_GRANT` `SCOPE_REVOKE` `EXPIRE_EXTEND` `EXPIRE_AUTO`（定时过期自动标记） |
| 审批流 | `TICKET_CREATE` `TICKET_APPROVE` `TICKET_REJECT` `TICKET_CANCEL` `TICKET_EXECUTE` |
| 资源授权（文档级） | `DOC_SHARE_GRANT` `DOC_SHARE_REVOKE` `DOC_SHARE_EXPIRE` |
| 资源授权（节点级） | `NODE_SHARE_GRANT` `NODE_SHARE_REVOKE` `NODE_SHARE_EXPIRE` |
| 访问黑名单（仅个人，Deny Override） | `DOC_BLOCK_ADD` `DOC_BLOCK_REMOVE` `DOC_BLOCK_EXPIRE` `NODE_BLOCK_ADD` `NODE_BLOCK_REMOVE` `NODE_BLOCK_EXPIRE` |
| 登录安全 | `LOGIN_SUCCESS` `LOGIN_FAIL` `LOGOUT` `PASSWORD_CHANGE` `TOKEN_REFRESH` |

> 审计日志写入要求：
> - **同步写**（业务操作同事务内提交），不能异步丢日志
> - **只允许 INSERT**，表级权限禁止 UPDATE / DELETE（甚至可用独立只读实例 + 定期归档到冷存）
> - 合规最低保留年限：**180 天**，金融/政府类 ≥ 1 年

---

## 七、授权审批流模型

### 7.1 审批矩阵（铁律：同级或上级发起，审批必须跳一级）

| 变更类型 | 发起人资格 | 审批链（顺序） | 备注 |
|---|---|---|---|
| staff → team_leader / dept_manager | **目标组织的直接上级** | ① 上级的上级（隔级审批） → ② tenant_user_admin（备案） | ① 通过即生效，② 留痕 |
| 跨团队 / 跨部门 Scope 扩大 | 被授权人**当前**直接上级 | ① 上级的上级 → ② tenant_user_admin | 跨组织必须有人事管理员背书 |
| 任何人 → 4 个全局租户角色 | **现有 tenant_super_admin** | ① **另一个 tenant_super_admin 批准** | 严格双人复核，无例外 |
| super_admin 新增 / 撤销 | tenant_super_admin 发起 | ① **另一个 tenant_super_admin 批准** | 3 人配置时：1 发起 + 1 批准 = 2/3 通过 |
| 权限**降级 / 撤销**（REVOKE） | 同级或更上级管理员 | **无需多人审批**，直接执行 + 审计 | 降级风险 < 升级 |
| 有效期延长 | 同「变更类型」规则 | 同「变更类型」规则 | 延长 = 事实上的范围扩大 |

### 7.2 审批拒绝 & 回退

- 任一节点 REJECTED → 工单终态 = REJECTED，**不执行任何授权表写入**
- 审批中申请人可主动 CANCELLED
- 审批链顺序执行，前一节点通过才到下一节点（不支持会签并行）

---

## 八、RAG 知识库权限模型

### 8.1 资源层级标记（知识库 / 节点 / 文档表必带字段）

| 表 | 字段 | 说明 |
|---|---|---|
| **KnowledgeNode**（见 6.1.2） | `tenant_id`, `kb_id`, `parent_node_id`, `path`, `owner_user_id`, `depth` | 节点树（版本/语言/技术栈等细分层） |
| **Document**（文档） | `tenant_id` | 租户 ID（跨租户物理隔离） |
| | `dept_id` | 所属部门（团队级 / 部门级必填；租户级 NULL / 0） |
| | `team_id` | 所属团队（团队级必填；部门级 / 租户级 NULL） |
| | `knowledge_node_id` | **归属节点 ID（必填）** FK → KnowledgeNode.node_id；文档必须挂在节点下（根节点也可） |
| | `visibility_level` | 枚举：`TENANT_PUBLIC` / `DEPT_ONLY` / `TEAM_ONLY` 三选一 |
| | `owner_user_id` | 文档 Owner（上传人或指定 Owner，用于审批文档访问申请） |

> **无个人级资源**（visibility_level 不含 PERSONAL）。  
> **节点级继承说明**：ResourceShare 授权给 `resource_type=KNOWLEDGE_NODE` 且 `inherit_mode=ALL_DESCENDANTS` 时，自动包含所有后代节点和后代节点下的文档（通过节点 path 前缀匹配一次搞定，见 8.4 和 8.5）。

### 8.2 资源等级定义

| visibility_level | 可见范围 | 谁有权上传 / 设置为该等级 |
|---|---|---|
| `TEAM_ONLY`（默认） | 仅 `team_id` 对应的团队成员可见 | 团队内任意成员上传时的默认值 |
| `DEPT_ONLY` | 仅 `dept_id` 对应的部门（含所有下属团队）可见 | 团队组长「上推」→ 部门经理批准，或部门经理直接设置 |
| `TENANT_PUBLIC`（公开） | 本租户全员可见 | 部门经理「上推」→ `tenant_kb_admin` / super_admin 批准 |

### 8.3 跨范围访问授权（主动模式）+ 节点级专属

> **RAG 场景只有主动模式**（见 6.4 说明）：Owner / 管理员在管理端统一配置共享，召回层直接过滤。不提供用户端「申请权限」入口。

**场景**：B 团队 Java 节点 Owner 主动把「语言/Java」整个节点树开放给 A 部门 / C 测试团队 / 张三 看

```
① 节点详情页 / 文档详情页 → 「共享设置」 → 「添加可见范围」
   ├─ 文档级：直接操作文档 → 共享给部门/团队/个人
   └─ 节点级：右键节点 → 共享给部门/团队/个人 → 默认 inherit_mode = ALL_DESCENDANTS（向下继承所有后代）
      ↓
② 弹窗支持多选：部门 / 团队 / 个人（混合选也可）
   ├─ 选部门：租户部门树选，支持多个
   ├─ 选团队：跨部门选团队，支持多个
   └─ 选个人：搜索，支持多个
      ↓
③ 选访问等级（READ / EDIT）+ 可选有效期（默认永久）+ 节点级可选 inherit_mode（ALL_DESCENDANTS / NODE_ONLY）
      ↓
④ 校验：share_scope_id 在租户内真实存在（应用层校验逻辑外键）；节点级校验 inherit_mode 合法
      ↓
⑤ 批量写入 ResourceShare（每条一个共享对象；节点级 inherit_mode 入表）
      ↓
⑥ 记审计日志（DOC_SHARE_GRANT / NODE_SHARE_GRANT）
      ↓
⑦ 按第十章规则清 L5 缓存（该节点 / 该文档 + 该节点所有后代节点的 L5 都要失效）
      ↓
⑧ （可选）通知被共享对象：「XX把【节点：语言/Java】共享给你，含 XX 个文档」
      ↓
⑨ 到期 / 撤销：改 status=EXPIRED / REVOKED + 记审计（NODE_SHARE_REVOKE / EXPIRE）+ 清缓存
```

> 节点级共享的核心收益：**一次授权，整个子树（不限深度）+ 所有挂在子节点下的文档都生效**。比如给测试团队开通「版本/v2.0」节点权限，v2.0 下按语言分的 Java/Python/Go 子节点及 500 个文档自动可见，不需要单独授权。

### 8.4 文档访问鉴权判定顺序（命中即短路返回）

> **优先级原则（从快到慢、从自然到额外）**：
> ① **Deny Override**（黑名单，第一优先级，拒绝即终止）
> ② **系统级管理员**（全局角色，直接放行）
> ③ **本组织自然可见范围**（越小范围越先判定：自己团队 → 自己部门 → 租户公开）—— 这是 95% 场景的主要命中路径
> ④ **资源所有权（Owner）**
> ⑤ **跨范围共享白名单**（文档级主动共享 → 节点级继承共享）—— 放最后，仅在前 4 类都不命中时才判定
> ⑥ 兜底：不命中 = 不召回（用户无感知，不提供申请入口）

```
0. 访问黑名单（ResourceBlockList）命中 —— **Deny Override 铁律，优先级最高，不可绕过** → 命中立即 403，不再执行任何后续判定
   —— 哪怕在第 1~8 步任何一层白名单里（包括全局管理员），只要命中黑名单一律拒绝
   0-a 文档级直接拉黑：
       resource_type=DOCUMENT + resource_id=本文档.id + blocked_user_id=我.user_id
       且 status=ACTIVE 且 有效期内 → ❌ 403
   0-b 节点级继承拉黑：
       已知文档挂在 node_id=N 上，N 的 path='|1|5|12|78|'，祖先链 = (1, 5, 12, 78)
       若 ResourceBlockList 中存在：
         resource_type=KNOWLEDGE_NODE + resource_id IN (1,5,12,78)
         + (block_inherit_mode=ALL_DESCENDANTS  或  (resource_id=78 且 block_inherit_mode=NODE_ONLY))
         + blocked_user_id=我 + ACTIVE 且 有效期内
       → ❌ 403（整个节点子树连带文档一起拒绝）

1. 我是 tenant_super_admin / tenant_kb_admin / tenant_compliance_admin → 过

——— ③ 本组织自然可见范围（从小到大，95% 场景命中这里）———

2. 文档 visibility_level = TEAM_ONLY 且 我在文档 team_id 团队内 → 过
   （优先命中最小范围：自己的团队，命中率最高）

3. 文档 visibility_level = DEPT_ONLY 且 我在文档 dept_id 部门树内（含祖先） → 过
   （次命中：自己所在部门）

4. 文档 visibility_level = TENANT_PUBLIC 且 我在同租户 → 过
   （兜底自然可见：整个租户公开文档）

——— ④ 资源所有权 ———

5. 我是文档 owner_user_id → 过

——— ⑤ 跨范围共享白名单（仅前面都不命中才判定，放最后）———

6. 文档级 ResourceShare（主动跨范围共享）命中（任一即可）：
   6-a share_scope_type = USER 且 share_scope_id = 我.user_id 且 ACTIVE 且 有效期内 → 过
   6-b share_scope_type = TEAM 且 share_scope_id = 我.team_id 且 ACTIVE 且 有效期内 → 过
   6-c share_scope_type = DEPT 且 share_scope_id IN 我所在部门树（含祖先）且 ACTIVE 且 有效期内 → 过

7. 节点级 ResourceShare（KNOWLEDGE_NODE + inherit_mode）继承命中：
   —— 核心：已知文档挂在 node_id = N 上，N 的 path = '|1|5|12|78|'
   —— 祖先链 = 节点 1（根）、5、12、78（N本身），即 path 中出现的所有 node_id
   7-a 在 ResourceShare 中 resource_type=KNOWLEDGE_NODE 且 resource_id IN (1,5,12,78) 的所有共享记录
   7-b 过滤 inherit_mode=ALL_DESCENDANTS（或 resource_id=78 时 NODE_ONLY 也命中）
   7-c 对每条命中的记录，按 6-a/6-b/6-c 规则判断 share_scope 是否覆盖我 → 任一覆盖 → 过

——— ⑥ 兜底 ———

8. 以上都不命中 → 不召回（文档从检索结果中静默剔除，用户无感知）
```

### 8.5 业务层 SQL 动态过滤示例（伪代码）

```python
def build_kb_filter(user):
    """所有文档查询必走此过滤器，不允许手写 where。"""
    # ================================================================
    # 【第 0 层】黑名单 Deny Override：命中 = 直接从结果集剔除，优先级最高
    # ================================================================
    # 0-a) 文档级直接拉黑：本人被明确拉黑的 doc_id 集合
    blocked_doc_ids = (
        ResourceBlockList.objects
        .filter(
            tenant_id=user.tenant_id,
            blocked_user_id=user.id,
            status=ACTIVE,
            effective_from__lte=Now(),
            Q(expires_at__isnull=True) | Q(expires_at__gt=Now()),
            resource_type=BLOCK_DOCUMENT,
        )
        .values_list("resource_id", flat=True)
    )
    # 0-b) 节点级拉黑（ALL_DESCENDANTS）：本人被拉黑的节点 → 取这些节点的 path 前缀
    blocked_all_descendant_node_ids = (
        ResourceBlockList.objects
        .filter(
            tenant_id=user.tenant_id,
            blocked_user_id=user.id,
            status=ACTIVE,
            effective_from__lte=Now(),
            Q(expires_at__isnull=True) | Q(expires_at__gt=Now()),
            resource_type=BLOCK_KNOWLEDGE_NODE,
            block_inherit_mode=ALL_DESCENDANTS,  # NODE_ONLY 不继承，不影响文档过滤
        )
        .values_list("resource_id", flat=True)
    )
    blocked_node_paths = KnowledgeNode.objects.filter(
        node_id__in=blocked_all_descendant_node_ids
    ).values_list("path", flat=True)  # e.g. ['|1|5|12|', '|1|33|']

    # 组装 deny 过滤 Q
    deny_q = ~Q(id__in=Subquery(blocked_doc_ids))  # 排除直接拉黑的文档
    for bp in blocked_node_paths:
        deny_q &= ~Q(knowledge_node__path__startswith=bp)  # 排除整棵被拉黑子树下所有文档

    # ================================================================
    # 【第 1 层】角色数据范围（全局管理员直接放开，走缓存 L4）
    # ================================================================
    level = get_user_scope_level(user)  # L4 缓存
    if level == TENANT_LEVEL:
        return Q(tenant_id=user.tenant_id) & deny_q

    # 我可见的部门（+祖先链，反坑）+ 团队
    visible_depts = get_user_managed_depts(user) | get_user_dept_ancestors(user.dept_id) | {user.dept_id}
    visible_teams = get_user_managed_teams(user) | {user.team_id}

    # ---------------- 关键：节点级共享继承 ----------------
    # 先找出"哪些节点被共享给我了（含祖先链命中 ALL_DESCENDANTS）"
    # 返回命中的 node_id 集合（我能看的节点）
    accessible_nodes_subquery = (
        ResourceShare.objects
        .annotate(
            # _match_scope：这条共享记录是否"罩"住我
            _match_scope=(
                (Q(share_scope_type=SHARE_USER) & Q(share_scope_id=user.id))
                | (Q(share_scope_type=SHARE_DEPT) & Q(share_scope_id__in=visible_depts))
                | (Q(share_scope_type=SHARE_TEAM) & Q(share_scope_id__in=visible_teams))
            )
        )
        .filter(
            tenant_id=user.tenant_id,
            status=ACTIVE,
            effective_from__lte=Now(),
            expires_at__isnull=False | Q(expires_at__gt=Now()),
            _match_scope=True,
        )
        # 场景 A：resource_type = DOCUMENT 直接共享给文档（见下方第 5 段）
        # 场景 B：resource_type = KNOWLEDGE_NODE（继承用）
        # 注意：这里查的是 "被共享过的节点 ID"
        .filter(resource_type=SHARE_KNOWLEDGE_NODE)
        .values_list("resource_id", flat=True)
    )
    # accessible_nodes = 直接被共享给我的所有节点ID（后续 JOIN path 前缀匹配）
    #
    # 接着：文档挂在 document.knowledge_node_id 上；文档对应节点 path 以任一 accessible_nodes 的 path 为前缀
    #        OR 文档对应节点本身 IN accessible_nodes（NODE_ONLY 模式）
    # 这一步通过 document -> knowledge_node JOIN + LIKE 前缀匹配：
    #   EXISTS (
    #     SELECT 1 FROM knowledge_node doc_node
    #       JOIN knowledge_node shared_node
    #         ON (shared_node.node_id IN accessible_nodes
    #             AND doc_node.path LIKE CONCAT(shared_node.path, '%'))
    #      WHERE doc_node.node_id = document.knowledge_node_id
    #   )
    # 或者：accessible_nodes 预加载到内存（一般共享给我的节点不会太多）→ 组装成 path 前缀 LIKE 条件
    accessible_node_paths = KnowledgeNode.objects.filter(
        node_id__in=accessible_nodes_subquery
    ).values_list("path", flat=True)  # e.g. ['|1|5|12|', '|1|20|']
    node_inherited_q = Q()
    for p in accessible_node_paths:
        # LIKE '|1|5|12|%' = 包含该节点 + 所有后代节点下挂的文档
        node_inherited_q |= Q(knowledge_node__path__startswith=p)

    # ================================================================
    # 【Allow 层 OR 顺序 = 与 8.4 鉴权优先级一致】
    # 从小到大命中：自己团队(95%场景) → 自己部门 → 租户公开 → Owner
    #                → 最后才是跨范围共享白名单(文档级/节点级)
    # 注意：SQL OR 在逻辑上不区分顺序（优化器自己选执行计划），这里的顺序仅
    #       用于让读代码的人一眼看出"意图上哪类优先命中"，与 8.4 章节一致。
    # ================================================================
    return Q(tenant_id=user.tenant_id) & deny_q & (
        # 1. 团队级（优先命中最小范围：自己团队，95% 场景主要命中）
        (Q(visibility_level=TEAM_ONLY) & Q(team_id__in=visible_teams))
        # 2. 部门级（次命中：自己所在部门）
        | (Q(visibility_level=DEPT_ONLY) & Q(dept_id__in=visible_depts))
        # 3. 租户公开文档（自然可见兜底 = 同租户所有人都能看）
        | Q(visibility_level=TENANT_PUBLIC)
        # 4. 我是文档 Owner（资源所有权）
        | Q(owner_user_id=user.id)
        # ——————————————— 以上 = 本组织自然可见（前 4 类）———————————————
        # ——————————————— 以下 = 跨范围共享白名单（放最后，仅前面都不命中才走）———————————————
        # 5. 文档级 ResourceShare（直接主动共享给该文档的跨范围授权）
        | Q(id__in=Subquery(
            ResourceShare.objects
            .filter(
                Q(share_scope_type=SHARE_USER) & Q(share_scope_id=user.id)
                | Q(share_scope_type=SHARE_TEAM) & Q(share_scope_id__in=visible_teams)
                | Q(share_scope_type=SHARE_DEPT) & Q(share_scope_id__in=visible_depts),
                status=ACTIVE, effective_from__lte=Now(),
                expires_at__isnull=True | Q(expires_at__gt=Now()),
                resource_type=SHARE_DOCUMENT,
            )
            .values_list("resource_id", flat=True)
        ))
        # 6. 节点级 ResourceShare 继承（跨范围授权到节点，子树+文档自动继承）
        | node_inherited_q
    )
```

> **节点继承为什么走 `path LIKE CONCAT(shared_path, '%')`？**
> 例子：共享给节点 Java（path=`|1|5|12|`，ALL_DESCENDANTS）
> 某文档挂在 Java/SpringBoot 节点下（path=`|1|5|12|78|`）→ `'|1|5|12|78|' LIKE '|1|5|12|%'` → TRUE，直接可见。
> 不需要递归 CTE，一次前缀索引扫描搞定。深度 10 层的树也一样快。

---

## 九、权限计算核心逻辑

### 9.1 用户权限来源（加载顺序）

1. **全局角色的权限点并集**（通过 UserRoleRel JOIN RolePermissionRel）
2. **所有绑定的属地角色（DeptScope + TeamScope）的权限点并集**
3. **本人默认 staff_user 角色的权限点**（兜底）
4. **最终功能权限 = 1 ∪ 2 ∪ 3**（去重）

### 9.2 数据权限：可见范围计算

| 项目 | 计算方式 |
|---|---|
| 可管理部门集合 | `UserDeptScopeRel(status=ACTIVE+有效期内).dept_id` |
| 可管理团队集合 | `UserTeamScopeRel(status=ACTIVE+有效期内).team_id` |
| 默认可见团队 | `user.team_id`（人事归属） |
| 默认可见部门 | `user.dept_id`（人事归属） |
| 最终可见团队 | 可管理团队 ∪ {默认可见团队} |
| 最终可见部门 | 可管理部门 ∪ {默认可见部门} |
| **最高数据范围等级** | `MAX(全局角色data_scope, 授权dept对应等级, 授权team对应等级, 默认team等级)` |

### 9.3 动态数据过滤规则（鉴权中间件）

| 最高数据范围等级 | 过滤行为 |
|---|---|
| 租户级（≥ TENANT） | 只加 `tenant_id = ?`，不加 dept / team 过滤 |
| 授权部门级（≥ DEPT_SCOPE） | 加 `dept_id IN (最终可见部门列表)` |
| 授权团队级 / 默认（TEAM_SCOPE / NORMAL） | 加 `team_id IN (最终可见团队列表)` |

### 9.4 支持的复杂场景

| 场景 | 解决方案 |
|---|---|
| **人在 A 组，管理 B 组** | 人事归属不变，新增一条 `UserTeamScopeRel`（B 组 + team_leader）即可，走审批流 |
| **一人多组长** | 多条 `UserTeamScopeRel`，查询 `IN(多个 team_id)` |
| **跨部门管理** | 多条 `UserDeptScopeRel` |
| **最小权限原则** | 不需要升级 super_admin，单条 Scope 授权即可跨域 |
| **临时协助** | 授权时设置 `expires_at = 7 天后`，到期自动失效 |

---

## 十、缓存设计（生产必备 —— 分层 Key + 延迟双删）

### 10.1 缓存 Key 分层（5 类独立失效）

| 层级 | Key 模板 | 内容 | TTL |
|---|---|---|---|
| L1 | `perm:fn:{tid}:{uid}` | `Set<String>` —— 该用户最终功能权限点集合（并集结果） | 1 小时 |
| L2 | `perm:scope:dept:{tid}:{uid}` | `List<Long>` —— 最终可见 dept_id 列表（含默认） | 1 小时 |
| L3 | `perm:scope:team:{tid}:{uid}` | `List<Long>` —— 最终可见 team_id 列表（含默认） | 1 小时 |
| L4 | `perm:scope:level:{tid}:{uid}` | `Int` —— 该用户最高数据范围等级枚举值 | 1 小时 |
| L5 | `perm:doc:{res_type}:{res_id}` | `List<{uid, access_level, expires_at}>` —— 某文档的临时授权用户清单 | 1 小时 |

> `{tid}` = tenant_id，`{uid}` = user_id。  
> 二级缓存：本地内存 Caffeine / Guava（10 ~ 30 秒短 TTL）放在 Redis 前面，抗热点。

### 10.2 变更 → 失效映射表（精准失效，不乱整 Key）

| 变更场景 | 需要失效的 Key（对应层级） |
|---|---|
| `UserRoleRel` 增 / 改 / 撤销 / 过期 | L1, L4 |
| `UserDeptScopeRel` 增 / 改 / 撤销 / 过期 | L1, L2, L4 |
| `UserTeamScopeRel` 增 / 改 / 撤销 / 过期 | L1, L3, L4 |
| `RolePermissionRel` 变化（角色-权限点绑定改了） | **所有持有该 role_key 的用户**的 L1（按角色反查用户，批量失效） |
| `ResourceShare` 新增 / 撤销 / 过期（主动共享变了） | L5（对应 resource 那条）；如果 share_scope_type=USER，还要清该 user 的 L5 关联 |
| `ResourceBlockList` 新增 / 解封 / 到期（黑名单变了） | L5（对应 resource 那条 + 被拉黑人个人缓存 + 节点级时 ALL_DESCENDANTS 后代节点 L5）；**Deny 优先级最高，必须比 Allow 更早失效** |
| 部门/团队树调整（父级变了）/ 部门软删 / 团队软删 | L2, L3, L4；且**所有包含该部门/团队的 ResourceShare 关联的资源 L5 都要失效**（共享范围语义变了） |
| 知识库节点操作（NODE_CREATE / NODE_MOVE 改父级=path 重算 / NODE_DELETE 软删 / NODE_RENAME） | 受影响节点及其所有后代节点的 L5（`perm:doc:KNOWLEDGE_NODE:*`）全部失效；若 `NODE_MOVE`（path 变化），额外触发所有挂在该节点树上文档的查询缓存失效（前缀匹配结果变了） |
| 用户调岗（user.dept_id / team_id 改了） | L1, L2, L3, L4 全部（人事归属变了） |

### 10.3 延迟双删策略（防并发脏写回填）

```
步骤：
  ① 业务写 DB（在事务内提交）
  ② 事务提交后 —— 第 1 次删除（前置删）对应 Key
  ③ sleep 500 ~ 1000 ms（异步线程池，不阻塞主流程）
  ④ 第 2 次删除同批 Key

伪代码：
def invalidate_after_change(keys_to_del: List[str]):
    redis.mdelete(*keys_to_del)                    # 第 1 次删
    schedule_delayed(
        delay_ms=800,
        task=lambda: redis.mdelete(*keys_to_del)  # 第 2 次删（延迟）
    )
```

> **为什么要第二次删？**  
> ① 和 ② 之间可能有并发读线程把 **旧 DB 值** 读出来塞回缓存（经典缓存一致性问题）；第二次删就是干掉这个间隙被回填的脏数据。  
> 800ms 经验值：> DB 主从同步延迟 + 网络 RTT。

---

## 十一、删除策略

| 数据类别 | 删除方式 | 具体操作 | 原因 |
|---|---|---|---|
| 组织架构（Tenant / Dept / Team / User） | **软删除** | `is_deleted = true`, `deleted_at = now()` | User 离职后，历史文档 Owner、审批记录要能追溯 |
| 内置 7 个 Role | **禁止删除** | 强制校验 `is_built_in` 直接拦截 | 系统基础能力 |
| 自定义 Role（未来扩展） | **软删除** | 同上 | 保留历史授权链 |
| Permission 权限点（内置） | **禁止删除** | 同上 | 代码依赖 permission_key |
| **KnowledgeNode（知识库节点树）** | **软删除** | `is_deleted=true, deleted_at=now()`；NODE_MOVE 时同步重算 `path` 和子节点所有 `path`（事务内批量更新） | 节点挂了文档/节点下有共享授权；软删保证历史共享追溯不丢 + 文档仍可通过其他路径访问 |
| 3 张授权绑定表（UserRoleRel / DeptScope / TeamScope） | **软删除（=撤销）** | 首选改 `status = REVOKED` + `is_deleted=true`；不物理删 | 审计 & 权限历史必须可查 |
| ResourceShare（主动共享） | **软删除 / 只改 status** | Owner 撤销 → `status=REVOKED`；到期 → `status=EXPIRED`；绝不物理删 | 共享历史追溯；尤其共享给部门/团队时，未来审计"XX时候哪个部门对文档有权限" |
| ResourceBlockList（访问黑名单，仅个人） | **软删除 / 只改 status** | 手动解封 → `status=REVOKED`；到期自动解封 → `status=EXPIRED`；绝不物理删 | 拉黑/解封历史是安全审计强留痕项（尤其临时封禁、离职人员权限复核） |
| PermissionApprovalTicket（审批工单） | **永不删除** | 只改 status（APPROVED / REJECTED / EXECUTED 等）；建议按 created_at 归档到冷存 | 合规红线 |
| PermissionAuditLog（审计日志） | **永不删除** | 只允许 INSERT；按法规保留 ≥ 180 天；到期归档冷存，禁止物理删 | 安全合规底线 |
| 知识库 / 文档本体（业务数据） | **业务自定义**（推荐软删 + 回收站） | 回收站保留 30 天后物理删；Owner 主动彻底删除 = 走单独审批 | 非权限层范畴，遵循数据分级策略 |

> **原则**：权限体系内的所有配置、审批、日志 —— **一律不物理删**。  
> 只有纯业务内容本体（文档、文件）按业务规则在必要时物理删。

---

## 十二、UI 授权交互规范（大厂统一体验）

### 12.1 授予权限流程

```
1. 选择「被授权用户」
      ↓
2. 选择「角色」
   ├─ 全局角色（4 个 GLOBAL_TENANT）→ 直接跳步骤 4（无 Scope 选择）
   └─ 部门 / 团队角色（DEPT_SCOPE / TEAM_SCOPE）→ 进入步骤 3
      ↓
3. 选择「管辖范围」
   ├─ 默认填充 = 用户本人所属部门 / 团队
   └─ 支持手动改为「租户内任意部门 / 团队」（支持多选 = 一人管多个）
      ↓
4. （可选）设置「有效期」
   ├─ 默认 = 永久（expires_at = NULL）
   └─ 支持选生效日期 + 到期日期
      ↓
5. 提交 → 生成 PermissionApprovalTicket
   ├─ 不需要审批的场景（降级撤销）→ 直接写入授权表 → 清缓存 → 记日志
   └─ 需要审批的场景 → 推送给审批链第一人 → 逐级批准 → worker 执行授权表写入
      ↓
6. 执行端：写入授权表（status=ACTIVE）→ 按第十章规则失效缓存 → 记审计日志
```

### 12.2 撤销权限流程

- 找到对应的授权记录（UserRoleRel / DeptScope / TeamScope / ResourceShare / ResourceBlockList）
- 点击「撤销」→ 填理由 → 若需要审批按第七章矩阵走；不需要则直接执行
- 执行：`status = REVOKED` + `is_deleted = true` + 按第十章清缓存 + 记审计日志

### 12.3 文档主动共享流程（新增部门/团队/个人）

```
1. 文档详情页 → 顶部「共享」按钮 / 「可见范围管理」Tab
      ↓
2. 展示「当前可见范围」面板：
   ├─ 基础可见范围：visibility_level（团队级 / 部门级 / 租户公开）
   ├─ 共享给部门：当前列表，可移除 / 修改有效期 / 修改访问等级
   ├─ 共享给团队：当前列表，同上
   └─ 共享给个人：当前列表，同上
      ↓
3. 点击「+ 添加可见范围」
   ├─ 选择类型：Tab 切换「部门 / 团队 / 个人」（三种类型一个弹窗，支持混合添加）
   ├─ 部门：树形选择，支持多选；团队：选择后可跨部门选团队
   └─ 个人：搜索框支持按姓名/邮箱搜索，支持多选
      ↓
4. 统一设置「访问等级」（READ / EDIT）+「有效期」（默认永久，可选 1/7/30 天/自定义）
      ↓
5. 提交
   ├─ 批量写入 ResourceShare（N 条记录，N=部门数+团队数+个人数）
   ├─ 按第十章 10.3 延迟双删清 L5 缓存
   └─ 每条记录写一条 DOC_SHARE_GRANT 审计日志
      ↓
6. 批量移除 / 批量撤销
   ├─ 多选后「撤销选中」→ 改 status=REVOKED + 写 DOC_SHARE_REVOKE 审计 + 清 L5
   └─ 支持「一键收回所有临时共享」（快速应急）
```

### 12.4 知识库节点管理 & 节点级共享流程

```
——— 节点管理（仅 Owner / 管理员）———
1. 知识库左侧面板：节点树视图（可拖拽、折叠、右键菜单）
   ├─ 右键：新建子节点（Java / Python / v1.0 / v2.0）
   ├─ 右键：重命名 / 移动到其他父节点（NODE_MOVE → 事务内批量重算 path）
   ├─ 右键：删除（软删，提示该节点下有 XX 文档和 XX 个共享）
   └─ 拖拽：节点拖到另一个父节点下（= NODE_MOVE，同重算 path）

——— 节点级共享（Owner / 管理员）———
2. 右键节点 → 「共享此节点（含子树）」/ 或节点详情 Tab
      ↓
3. 当前面板展示：
   ├─ inherit_mode：当前节点的共享默认继承策略（ALL_DESCENDANTS = 子树全部继承）
   ├─ 节点级共享列表：共享给了哪些部门/团队/个人 + 有效期 + 访问等级
   └─ 数量提示：「共 XX 个部门、XX 个团队、XX 人可见，影响 XX 个文档」
      ↓
4. 添加可见范围：
   ├─ 多选部门/团队/个人（混合选）
   ├─ inherit_mode 选择（默认 ALL_DESCENDANTS；选 NODE_ONLY = 仅本节点，一般用于特殊权限）
   └─ 设置访问等级 + 有效期
      ↓
5. 提交
   ├─ 批量写入 ResourceShare(resource_type=KNOWLEDGE_NODE, inherit_mode=...)
   ├─ 清 L5：该节点 + 所有后代节点的缓存（path 前缀匹配）
   └─ 每条记一条 NODE_SHARE_GRANT 审计
```

> **节点级共享反坑 UI 提示**：当共享的是一个 NODE_ONLY（不继承）节点时，UI 要醒目提示「此授权仅包含本节点自身，不含子节点和文档，是否确认？」防止误配漏授权。

### 12.5 访问黑名单管理（仅个人，Deny Override）

> ⚠️ **安全提示**：黑名单优先级高于 **所有** 白名单（含 super_admin 全局角色）—— 仅 Owner / tenant_super_admin / tenant_compliance_admin 可操作，普通管理员无权限。

```
——— 入口（两种，作用等效）———
A. 文档详情页 / 节点详情页 → 「共享设置」 → 「访问黑名单」Tab
B. 文档/节点右键 → 「封禁某人访问」

——— 添加黑名单（封禁某人）———
1. 点击「+ 添加被封禁用户」
      ↓
2. 选用户：搜索框按姓名/邮箱搜索（**严格禁止**多选部门/团队/批量导入，防止误封 50 人灾难）
   反坑：UI 限制每次最多选 5 人，批量封禁需合规审批
      ↓
3. 选择范围：
   ├─ 文档级：仅本页文档
   └─ 节点级（若从节点进入）：
       ├─ 🔘 默认：ALL_DESCENDANTS = 该节点 + 所有子节点 + 子节点下所有文档（强烈推荐）
       └─ ⚠️ NODE_ONLY = 仅本节点自身（极少使用，一般是特殊隔离场景）
      ↓
4. 设置有效期：
   ├─ 默认：永久封禁
   ├─ 临时：7天 / 30天 / 自定义（例如："该用户离职交接期间，临时封禁 30 天，30 天后自动解封"）
      ↓
5. **必填拉黑理由**（文本，强制 ≥ 10 字）：
   示例：「已离职 T-20251030-XXX」「涉密项目剔除-合规审批单号 C-2025-088」「权限回收流程处理中临时封禁」
   反坑：理由不能填「测试」「随便」等无效理由，合规审计会追溯
      ↓
6. 提交：
   ├─ 写入 ResourceBlockList（每条一个人）
   ├─ 按第十章延迟双删：清 L5（该资源/节点后代所有 L5 + 被拉黑人个人 L5）
   ├─ 每条记一条 DOC_BLOCK_ADD / NODE_BLOCK_ADD 审计日志
   └─ 合规管理员/Owner 同步收到通知（留痕）

——— 解封 / 撤销 ———
黑名单列表 → 选中 → 「解除封禁」
    → status=REVOKED + 填解除理由（必填）+ 记 DOC/NODE_BLOCK_REMOVE 审计 + 清 L5

——— 到期自动解封 ———
定时任务每 5 min 扫描 expires_at ≤ NOW() 且 status=ACTIVE → 改 EXPIRED
    → 记 DOC/NODE_BLOCK_EXPIRE 审计 + 清 L5（无人工通知，自动安静生效）
```

> **反坑：黑名单不能"隐式"到期解除** —— 比如拉黑"已离职的张三"时选了 7 天临时封禁，7 天后会自动解封。对永久封禁场景 **必须** 选「永久」，不能留空或选错有效期（UI 默认永久就是为了防这个）。

---

## 十三、本方案等级总结

### 对标结果

- 完全等同于 **飞书 / 钉钉** 企业组织权限体系
- 完全遵循 **阿里云 RAM、腾讯云 CAM** 授权模型 + **双人审批** 安全规范
- 属于 **中型 SaaS / AI 知识库** 行业最优标准方案

### 优势清单（彻底规避新手坑）

✅ 角色不绑定组织，无角色泛滥  
✅ 人事与权限完全解耦，调岗不影响权限配置  
✅ 支持跨组织代管，权限精细可控  
✅ **代码判断权限点、不硬编码角色**，新增角色零代码改动  
✅ **授权提升防护矩阵**，杜绝普通用户自升超级管理员  
✅ **审批流模型**，超级管理员变更严格双人复核  
✅ **审计日志全覆盖 + 永不删除**，合规达标  
✅ **所有授权支持有效期**，到期自动失效  
✅ **RAG 场景适配**：无权限文档直接不召回，用户无感知；权限管理端统一配置，对标飞书/钉钉/AWS Q 大厂做法  
✅ **文档主动共享（ResourceShare，统一 target_type 表）**：单表支持部门/团队/个人，扩展零表改动  
✅ **知识库节点树（KnowledgeNode 路径枚举）**：版本/语言/技术栈任意分层，一次节点级授权自动继承所有子树 + 文档  
✅ **访问黑名单（ResourceBlockList，仅个人）Deny Override**：优先级最高，在所有白名单之前判定；共享给全部门 100 人 → 精准剔除 1 个涉密/离职人员，**哪怕在 5 个白名单里也一律拒绝**，零误操作风险  
✅ **部门树祖先链匹配**：避免共享给一级部门、三级部门看不到的经典坑  
✅ **节点 path 前缀继承 + LIKE 匹配**：共享给父节点 = 所有后代文档自动可见，无需递归；黑名单同样走前缀路径，整棵子树一次拉黑即可  
✅ **缓存分层 Key + 延迟双删**，高并发下权限一致性保证  
✅ **删除策略分级**，权限数据一律软删，合规可追溯  
✅ 结构清晰、可维护、可扩展、无歧义  
✅ 完全支持生产环境高并发 & 权限审计
