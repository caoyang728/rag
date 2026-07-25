# RAG-Agent 企业级知识库平台

> Django 5 + DRF + Celery + PostgreSQL(pgvector) + Redis + DeepSeek 全栈落地，一个面向企业内部的、可解释、可审计、可运维的 RAG 问答系统。

---

## 一、快速启动

```bash
cd rag

# 1. 配置环境变量
cp .env.example .env && vim .env

# 2. 一键启动
docker compose up -d --build

# 3. 执行数据库迁移（首次部署）
docker compose exec django python manage.py migrate

# 4. 初始化系统数据（角色、权限、部门、团队、用户）
docker compose exec django python scripts/init_system.py
```

启动完成后：
- Web UI（极简 SPA）：<http://localhost:8000>
- API：<http://localhost:8000/api/v1/>
- Django Admin：<http://localhost:8000/admin/>
- Healthz：<http://localhost:8000/healthz>

---

## 二、目录结构

```
rag/
├── apps/
│   ├── users/                  # 用户系统（User + RBAC 权限 + 部门/团队 + 文档级权限）
│   │   ├── models.py           # User(AbstractBaseUser), Department, Team, Role, Permission + DocDenyUser/DocAllowUser/DocCrossTeam/AccessApplication
│   │   ├── signals.py          # 部门/团队变更 → 自动同步 KnowledgeNode 树 + 缓存失效
│   │   └── views.py            # 部门/团队 CRUD（含删除保护：有成员/有文档→禁止删除）
│   ├── knowledge/              # 知识节点 + 文档 + 切片 + 多格式解析器
│   │   ├── models.py           # KnowledgeNode(固定4层树) + Document + DocumentChunk + CodeChunk + ImageResource + DocOperationLog
│   │   ├── access.py           # resolve_doc_access() — 7 步优先级权限判定
│   │   ├── node_sync.py        # 部门/团队 ↔ KnowledgeNode 双向同步 + 子树文档计数
│   │   ├── parsers/            # pdf/docx/code 三套解析器
│   │   ├── storage.py          # 文档存储抽象层（local/OSS，按节点路径存储）
│   │   └── tasks.py            # Celery 任务（解析、向量化、批量导入）
│   ├── retrieval/              # pgvector 向量检索 + BM25 关键词 + 混合检索（RRF 融合）+ Rerank
│   │   ├── models.py           # DocumentVector（pgvector HNSW 索引 + 权限冗余字段）
│   │   ├── vector_store.py     # 向量检索封装（cosine 距离 + hnsw.ef_search 会话级调节）
│   │   ├── bm25.py             # BM25 关键词检索（jieba 分词 + keyword_weight 加权）
│   │   └── permission.py       # build_permission_q() — 检索级权限过滤
│   ├── llm/                    # DeepSeek Provider + 抽象工厂 + Prompt 模板
│   ├── memory/                 # 四层记忆（short/session/user/global）
│   ├── agent/                  # 问答编排 + 任务拆分 + 流式回答
│   ├── chat/                   # 会话 / QA 记录 / 反馈 / 热点缓存
│   ├── audit/                  # 审计日志（sha256 哈希链防篡改）
│   ├── security/               # 验证码 / IP 黑白名单 / 登录失败锁定 / 敏感词
│   ├── analytics/              # 关键词权重 / 准确率日报 / 趋势统计
│   ├── notification/           # 邮件订阅 / 发送日志
│   └── system/                 # 系统配置 / Celery 任务日志 / LLM 调用日志
├── rag_project/                # Django 项目配置、根 URL、Celery、ASGI/WSGI
├── docs/
│   └── permission-design.md    # 权限体系设计文档（最终方案）
├── scripts/
│   ├── init_system.py          # 系统初始化（角色/权限/部门/团队/用户）
│   ├── initial_data.yaml       # 初始化数据配置
│   └── batch_import_docs.py    # 批量导入文档（双阶段异步导入）
├── static/                     # 前端静态资源（开发源码）
│   ├── css/                    # 公共 common.css + 各页面 page.css
│   ├── fonts/                  # 字体文件（验证码使用 DejaVuSans-Bold.ttf）
│   ├── js/                     # API_SERVICE 通用请求 + 各页面模块
│   │   ├── common.js           # 通用请求封装（含 token / 401 处理 / 流式 / 侧边栏渲染）
│   │   ├── chat.js             # 会话问答
│   │   ├── upload.js           # 文档上传（含恢复/新建三选项对话框）
│   │   ├── login.js            # 登录（含验证码）
│   │   ├── profile.js          # 个人资料
│   │   ├── reset-password.js   # 修改密码
│   │   ├── admin-users.js      # 用户管理
│   │   ├── admin-rbac.js       # 角色权限管理
│   │   ├── admin-nodes.js      # 知识节点管理（动态加载根类型）
│   │   ├── admin-analytics.js  # 统计分析
│   │   └── admin-audit.js      # 审计日志
│   ├── index.html              # 首页
│   ├── login.html              # 登录页
│   ├── chat.html               # 问答页
│   ├── upload.html             # 文档上传页
│   ├── profile.html            # 个人资料页
│   ├── reset-password.html     # 修改密码页
│   ├── admin-users.html        # 用户管理页
│   ├── admin-rbac.html         # 角色权限管理页
│   ├── admin-nodes.html        # 知识节点管理页
│   ├── admin-analytics.html    # 统计分析页
│   └── admin-audit.html        # 审计日志页
├── tests/                      # API 测试用例
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh               # 容器启动脚本（等 DB → 迁移 → 收集静态 → 启动）
├── requirements.txt
├── .env.example
└── README.md
```

---

## 三、核心 API 概览

### 认证 & 用户

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/v1/auth/login/` | 登录（含验证码），返回 `{access, refresh, user}` |
| POST | `/api/v1/auth/logout/` | 登出（refresh 加黑名单） |
| GET/PATCH | `/api/v1/auth/profile/` | 当前用户资料 |
| POST | `/api/v1/auth/reset-password/` | 修改密码 |
| CRUD | `/api/v1/users/` | 用户管理 |
| POST | `/api/v1/users/{id}/toggle_status/` | 启用/禁用用户 |
| CRUD | `/api/v1/departments/` | 部门管理（自动同步知识节点树） |
| CRUD | `/api/v1/teams/` | 团队管理（自动同步知识节点树） |
| CRUD | `/api/v1/roles/` `/permissions/` | 角色/权限管理 |
| GET | `/api/v1/permissions/me/` | 当前用户权限 |
| CRUD | `/api/v1/access-applications/` | 文档访问权限申请 |

### 知识库

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/v1/knowledge/nodes/tree/?root_type=` | 节点树（含各节点文档数） |
| GET | `/api/v1/knowledge/nodes/root_types/` | 动态获取根类型列表 |
| CRUD | `/api/v1/knowledge/nodes/` | 节点管理（Level 1-3 写保护，Level 4+ 自由管理） |
| POST | `/api/v1/knowledge/documents/upload/` | 文档上传（sha256 去重，异步解析） |
| GET | `/api/v1/knowledge/documents/` | 文档列表（支持 discover 模式） |
| GET | `/api/v1/knowledge/documents/{id}/` | 文档详情 |
| GET | `/api/v1/knowledge/documents/pending/` | 待处理文档列表（轮询状态） |
| GET | `/api/v1/knowledge/documents/{id}/chunks/` | 文档分块列表 |
| GET/PATCH | `/api/v1/knowledge/documents/{id}/visibility/` | 查看/修改文档可见范围 |
| POST | `/api/v1/knowledge/documents/{id}/reparse/` | 重新解析 |
| DELETE | `/api/v1/knowledge/documents/{id}/` | 删除文档 |
| GET | `/api/v1/knowledge/documents/{id}/raw/` | 文档原文预览（50MB 限制，分页） |

### 检索 & 问答

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/v1/chat/ask/` | **核心问答接口**（含流式） |
| POST | `/api/v1/chat/feedback/` | 提交问答反馈 |
| CRUD | `/api/v1/chat/sessions/` | 会话管理 |
| GET | `/api/v1/chat/sessions/{id}/qa/` | 问答历史（按 turn_index 升序） |
| POST | `/api/v1/agent/task/plan/` | 复杂任务拆分预览 |
| POST | `/api/v1/agent/task/run/` | 拆分并执行 |
| POST | `/api/v1/retrieval/search/` | 检索调试接口 |

### 审计 & 安全 & 系统

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/v1/security/captcha/` | 验证码图片（140×41，6 干扰线，30次/分钟限流） |
| GET | `/api/v1/audit/logs/` | 审计日志列表 |
| POST | `/api/v1/audit/verify-chain/` | 哈希链完整性校验 |
| GET | `/api/v1/analytics/overview/` | 概览统计 |
| GET | `/api/v1/analytics/trend/?days=` | 趋势报表 |
| GET | `/api/v1/security/ip-whitelist/` `/ip-blacklist/` | IP 黑白名单 |
| GET | `/api/v1/system/health/` | 健康检查 |
| GET | `/api/v1/system/stats/` | 首页看板 |

---

## 四、技术栈

| 层 | 选型 | 理由 |
|-----|------|------|
| Web | Django 5.2 + DRF | 企业级 Web 框架，Admin/ORM 加速开发 |
| 鉴权 | rest_framework_simplejwt | JWT 无状态，配合 refresh + blacklist |
| 异步 | Celery 5.4 + Redis | 文档解析、记忆提炼、日报、批量导入都走队列 |
| DB | PostgreSQL 16 + pgvector | 结构化 + 向量一站式，避免额外维护 Milvus |
| 缓存 | Redis 7 | 短时记忆 / 会话 / 验证码 / Celery broker / 权限缓存 |
| LLM | DeepSeek Chat/Reasoner | 国内合规 + 成本可控；Provider 抽象保留切换空间 |
| 嵌入 | BGE-M3 (SiliconFlow) | 高性能中文嵌入模型，1024 维 |
| Rerank | BGE-Reranker-v2 (SiliconFlow) | 轻量级重排序模型 |
| 前端 | 原生 JS + Hash 路由（极简 SPA） | 演示够用；生产可换 Vue/React |

---

## 五、数据库表概览

### 用户与权限（users）

| 模型 | 表名 | 说明 |
|------|------|------|
| User | `user_account` | 用户实体（AUTH_USER_MODEL） |
| Department | `user_department` | 部门（自引用树） |
| Team | `user_team` | 团队（FK → Department） |
| Role | `user_role_list` | 角色清单（6 种内置角色） |
| Permission | `user_permission_list` | 权限项清单（`module:action:scope` 格式） |
| RolePermission | `user_role_permission_rel` | 角色↔权限关联 |
| UserRole | `user_account_role_rel` | 用户↔角色关联 |
| UserTeam | `user_account_team_rel` | 用户↔团队关联 |
| DocDenyUser | `doc_deny_user` | 文档黑名单（物理删除） |
| DocAllowUser | `doc_allow_user` | 文档个人白名单（含 expire_time） |
| DocCrossTeam | `doc_cross_team` | 跨团队授权（含 expire_time） |
| AccessApplication | `access_application` | 统一权限申请单（双轨：申请拉 + 授权推） |

### 知识库（knowledge）

| 模型 | 表名 | 说明 |
|------|------|------|
| KnowledgeNode | `knowledge_node` | 固定 4 层树（KB→部门→团队→分类），Level 4+ 无限层级 |
| Document | `knowledge_document` | 文档元数据（visible_scope 三档，audit_status 双审状态） |
| DocumentChunk | `knowledge_document_chunk` | 文档切片 |
| CodeChunk | `knowledge_code_chunk` | 代码切片（AST 解析） |
| ImageResource | `knowledge_image` | 图片资源（base64/OSS 双存储） |
| DocOperationLog | `knowledge_doc_operation_log` | 文档操作审计日志（20 种 action） |

### 检索（retrieval）

| 模型 | 表名 | 说明 |
|------|------|------|
| DocumentVector | `retrieval_doc_vector` | pgvector HNSW 索引 + 权限冗余字段 |

---

## 六、权限系统规则

### 6.1 角色体系（6 种）

| 角色编码 | 角色名称 | 核心职责 |
|---------|---------|---------|
| super_admin | 超级管理员 | 全部配置权限、绕过双审直接发布、可物理销毁文档 |
| kb_admin | 知识库管理员 | 全部文档管理权限（CRUD/审核/授权/删除），无用户管理 |
| user_admin | 用户管理员 | 全部用户管理（CRUD/角色/部门/团队），系统配置 |
| dept_manager | 部门负责人 | 管辖本部门全部团队、审批扩大可见范围申请 |
| team_leader | 团队组长 | 本团队文档一审、管理文档、调整可见范围、收回对外权限 |
| compliance_reviewer | 文档审核员 | 专职合规风控、敏感内容二审、无日常检索问答权限 |
| employee | 普通员工 | 检索权限内已审核文档、上传发起双审工单、发起权限申请 |
| readonly | 只读员工 | 检索已发布文档、禁止上传 |

### 6.2 节点结构（固定 4 层 + 自定义分类）

```
一级：知识库根节点 (kb root)
  └─ 二级：部门节点 (dept)           ← 由 Department 生命周期自动管理
      └─ 三级：团队节点 (team)       ← 由 Team 生命周期自动管理
          └─ 四级+：业务分类 (category) ← 由团队组长手动管理，无层级上限
```

- Level 1-3（KB/部门/团队）：不可通过节点 API 直接 CRUD，由部门/团队生命周期自动同步
- Level 4+（业务分类）：通过节点 API 自由创建/修改/删除（删除时需节点下无文档）

### 6.3 文档可见范围（三档）

| 可见范围 | 含义 | 默认访问者 |
|---------|------|-----------|
| team | 仅归属团队 | 同团队成员 + 所有者 + 管理员 |
| dept | 归属全部门 | 同部门所有成员 + 所有者 + 管理员 |
| public | 全公司公开 | 所有登录用户 |

### 6.4 文档双层审核流程

```
上传 → 系统预检 → 待团队组长一审 (pending_team)
                      ↓
           待合规审核员二审 (pending_compliance)
                      ↓
              双审通过 (passed) → 可被检索
```

super_admin 可绕过双审直接发布。

### 6.5 访问权限判定（7 步优先级，命中即停）

1. 黑名单拦截 → 全部拒绝（最高优先级）
2. visible_scope='public' → 放行
3. visible_scope='dept' 且同部门 → 放行
4. visible_scope='team' 且同团队 → 放行
5. 跨团队授权命中（DocCrossTeam，未过期）→ 放行
6. 个人白名单命中（DocAllowUser，未过期）→ 放行
7. 否则拒绝

所有者（owner）和管理员（super_admin/kb_admin）始终拥有全部权限。

### 6.6 权限申请双轨制

- **轨道 1（申请拉取）**：用户提交 AccessApplication → 审批 → 插入白名单/跨团队记录
- **轨道 2（授权推送）**：组长/管理员直接操作 DocAllowUser/DocCrossTeam 表

### 6.7 部门/团队删除保护

- **删除团队**：团队下无成员 AND 团队节点及子树下无文档 → 允许；否则提示具体数字
- **删除部门**：部门下无用户 AND 无团队 → 允许；否则提示具体数字
- **删除分类节点（Level 4+）**：该节点及全部子孙节点下无文档 → 允许

---

## 七、文档存储架构

### 7.1 存储模式

通过环境变量 `DOCUMENT_STORAGE_MODE` 切换：
- `local`：本地文件系统（默认）
- `oss`：云 OSS / MinIO

### 7.2 按节点路径存储

```
media/documents/
└── node-{id}_{name}/          # 根节点目录
    └── node-{id}_{name}/      # 子节点目录
        └── {uuid}_{filename}  # 文档文件
```

### 7.3 安全措施

- MIME 类型验证（python-magic 库，防止文件类型绕过）
- 文件名净化（django.utils.text.get_valid_filename，去除路径分隔符和控制字符）
- sha256 文件哈希去重，防止重复上传
- raw_content 预览 50MB 限制 + 分页

---

## 八、批量导入

```bash
# 默认：team 可见，超级管理员上传
docker compose exec django python scripts/batch_import_docs.py

# 指定可见范围
docker compose exec django python scripts/batch_import_docs.py --visibility public

# 列出可用节点/部门
docker compose exec django python scripts/batch_import_docs.py --list-nodes
docker compose exec django python scripts/batch_import_docs.py --list-departments
```

**工作原理（双阶段导入）**：
- 阶段一（脚本）：扫描目录 → 创建临时文件 → 发送到 Celery 队列
- 阶段二（Celery）：验证 → 保存到目标位置 → 创建记录 → 触发解析 → 删除临时文件
- 100MB 文件大小限制，失败时保留临时文件便于手动处理

---

## 九、环境变量配置

```ini
# --- Django 基础 ---
DEBUG=1
SECRET_KEY=change-me-please-in-production
ALLOWED_HOSTS=*

# --- 数据库（PostgreSQL 16 + pgvector）---
POSTGRES_DB=rag_agent
POSTGRES_USER=rag_user
POSTGRES_PASSWORD=rag_pass_2026
PG_DB_HOST=localhost
PG_DB_PORT=5432

# --- Redis ---
REDIS_DB_HOST=localhost
REDIS_DB_PORT=6379

# --- LLM ---
LLM_API_KEY=sk-your-api-key
LLM_BASE_URL=https://api.deepseek.com
LLM_BASE_MODEL=deepseek-v4-flash
LLM_ADVANCED_MODEL=deepseek-v4-pro

# --- Embedding & Rerank ---
EMBEDDING_API_KEY=sk-your-embedding-key
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIM=1024
RERANK_MODEL=BAAI/bge-reranker-v2-m3

# --- Embedding Provider ---
EMBEDDING_PROVIDER=docker     # docker / api

# --- 文档存储 ---
DOCUMENT_STORAGE_MODE=local   # local / oss
DOCUMENT_MAX_SIZE_MB=100
```

完整变量列表见 `.env.example`。

---

## 十、测试

```bash
docker compose exec django python tests/test_api_simple.py
```

---

## 十一、二次开发建议

1. **检索向量库**：当前走 pgvector，可切换 Milvus/Qdrant，只改 `upsert_vector` / `vector_search`
2. **Rerank**：已抽象签名 `rerank_docs`，可换 Cohere API
3. **多租户**：`Document` 已有 `dept_node_id`/`team_node_id`，扩展 tenant_id 即可
4. **前端替换**：`static/` 为极简演示，正式项目建议 Vue3 + Element Plus
5. **双路审批（TODO）**：kb_admin + user_admin 联合审批关键操作，避免单人权限过大
