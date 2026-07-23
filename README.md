# RAG-Agent 企业级知识库平台

> Django 5 + DRF + Celery + PostgreSQL(pgvector) + Redis + DeepSeek 全栈落地，一个面向企业内部的、可解释、可审计、可运维的 RAG 问答系统。

---

## 一、快速启动

```bash
# 1. 克隆仓库并进入根目录
cd rag

# 2. 复制并按需修改环境变量（LLM_API_KEY 至少要填一个真 key，否则走 stub）
cp .env.example .env && vim .env

# 3. 一键启动 web + celery
docker compose up -d --build

# 首次启动后建议：
docker compose exec django python manage.py createsuperuser
docker compose exec django python scripts/init_system.py   # 初始化系统数据
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
│   ├── users/                  # 自定义 SysUser + RBAC（角色/权限/部门/团队）
│   ├── knowledge/              # 节点树 + 文档 + 分块 + 多格式解析器
│   │   ├── parsers/            # pdf/docx/code 三套解析器
│   │   ├── storage.py          # 文档存储抽象层（本地/OSS，按节点路径存储）
│   │   └── tasks.py            # Celery 任务（解析、向量化、批量导入）
│   ├── retrieval/              # 向量库 + BM25 + 混合检索（RRF 融合）+ Rerank
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
├── scripts/
│   ├── init_system.py          # 系统初始化（角色/权限/部门/用户）
│   ├── initial_data.yaml       # 初始化数据配置
│   ├── batch_import_docs.py    # 批量导入文档（双阶段异步导入）
│   └── upload/                 # 批量导入的文件存放目录
├── static/                     # 前端静态资源（开发源码）
│   ├── css/                    # 公共 common.css + 各页面 page.css
│   ├── fonts/                  # 字体文件（验证码使用 DejaVuSans-Bold.ttf）
│   ├── js/                     # API_SERVICE 通用请求 + 各页面模块
│   │   ├── common.js           # 通用请求封装（含 token / 401 处理 / 流式）
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
│   ├── chat.html               # 问答页
│   ├── upload.html             # 文档上传页
│   ├── login.html              # 登录页
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

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/v1/auth/login/` | 登录（含验证码），返回 `{access, refresh, user}` |
| POST | `/api/v1/auth/logout/` | 登出（refresh 加黑名单） |
| GET/PATCH | `/api/v1/auth/profile/` | 当前用户资料（支持更新 real_name / avatar_url / phone） |
| POST | `/api/v1/auth/reset-password/` | 修改密码 |
| CRUD | `/api/v1/auth/users/` `/roles/` `/permissions/` `/departments/` `/teams/` | RBAC 管理 |
| POST | `/api/v1/auth/users/{id}/toggle_status/` | 启用/禁用用户 |
| GET | `/api/v1/auth/permissions/me/` | 当前用户权限 |
| GET | `/api/v1/auth/permissions/approvers/` | 权限审批人列表 |
| CRUD | `/api/v1/auth/permissions/applications/` | 权限申请 |
| GET | `/api/v1/security/captcha/` | 获取验证码图片（Canvas 绘制，140×41，6 干扰线） |
| GET | `/api/v1/knowledge/nodes/tree/?root_type=` | 节点树 |
| GET | `/api/v1/knowledge/nodes/root_types/` | 动态获取根类型列表 |
| CRUD | `/api/v1/knowledge/nodes/` | 节点管理 |
| POST | `/api/v1/knowledge/documents/upload/` | 文档上传（multipart，sha256 去重，异步解析，支持恢复/新建） |
| GET | `/api/v1/knowledge/documents/` | 文档列表 |
| GET | `/api/v1/knowledge/documents/{id}/` | 文档详情 |
| GET | `/api/v1/knowledge/documents/pending/` | 待处理文档列表（轮询状态） |
| GET | `/api/v1/knowledge/documents/{id}/chunks/` | 文档分块列表 |
| POST | `/api/v1/knowledge/documents/{id}/reparse/` | 重新解析 |
| DELETE | `/api/v1/knowledge/documents/{id}/` | 删除文档 |
| GET | `/api/v1/knowledge/celery/status/` | Celery 状态检查 |
| POST | `/api/v1/chat/ask/` | **核心问答接口** `{session_id, question, root_types, use_cache, do_task_split}` |
| POST | `/api/v1/chat/feedback/` | 提交问答反馈 |
| CRUD | `/api/v1/chat/sessions/` | 会话管理 |
| GET | `/api/v1/chat/records/?session_id=` | 问答历史 |
| POST | `/api/v1/agent/task/plan/` | 复杂任务拆分预览 |
| POST | `/api/v1/agent/task/run/` | 拆分并执行 |
| POST | `/api/v1/retrieval/search/` | 检索调试 |
| GET | `/api/v1/memory/context/` | 记忆上下文调试 |
| GET | `/api/v1/audit/logs/` | 审计日志列表 |
| POST | `/api/v1/audit/verify-chain/` | 哈希链完整性校验 |
| GET | `/api/v1/analytics/overview/` | 概览统计 |
| GET | `/api/v1/analytics/trend/?days=` | 趋势报表 |
| GET | `/api/v1/analytics/keywords/` | 关键词权重 Top |
| GET | `/api/v1/analytics/bad-feedbacks/` | 差评列表 |
| GET | `/api/v1/security/ip-whitelist/` | IP 白名单 |
| GET | `/api/v1/security/ip-blacklist/` | IP 黑名单 |
| GET | `/api/v1/security/login-attempts/` | 登录尝试记录 |
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
| 缓存 | Redis 7 | 短时记忆 / 会话 / 验证码 / Celery broker |
| LLM | DeepSeek Chat/Reasoner | 国内合规 + 成本可控；Provider 抽象保留切换 GPT/Claude 空间 |
| 嵌入 | BGE-M3 (SiliconFlow) | 高性能中文嵌入模型，通过环境变量配置 |
| Rerank | BGE-Reranker-v2 (SiliconFlow) | 轻量级重排序模型 |
| 前端 | 原生 JS + Hash 路由（极简 SPA） | 演示够用；生产可换 Vue/React |

---

## 五、文档存储架构

### 5.1 存储模式

通过环境变量 `DOCUMENT_STORAGE_MODE` 切换：
- `local`：本地文件系统（默认）
- `oss`：云 OSS / MinIO

### 5.2 按节点路径存储

文件按知识库节点结构存储，便于运维管理：

```
media/documents/
└── node-{id}_{name}/          # 根节点目录
    └── node-{id}_{name}/      # 子节点目录
        └── {uuid}_{filename}  # 文档文件
```

### 5.3 文档恢复机制

当上传已删除的文件时，系统提供三选项对话框：
- **恢复**：恢复旧记录，更新文件位置、上传者、团队，记录 `restored_at` 和 `restored_by` 审计字段
- **新建记录**：创建独立新记录，不关联旧记录
- **取消**：放弃上传

---

## 六、批量导入

### 6.1 使用方法

```bash
# 默认：私有可见，超级管理员上传
docker compose exec django python scripts/batch_import_docs.py

# 部门可见
docker compose exec django python scripts/batch_import_docs.py --visibility department --department-code R&D

# 部门可见，指定上传者
docker compose exec django python scripts/batch_import_docs.py --visibility department --department-code R&D --owner user1

# 团队可见
docker compose exec django python scripts/batch_import_docs.py --visibility team --team-code RAG-PROJ

# 所有人可见
docker compose exec django python scripts/batch_import_docs.py --visibility public
```

### 6.2 工作原理（双阶段导入）

```
阶段一（脚本）：扫描目录 → 创建临时文件 → 发送到 Celery 队列
阶段二（Celery）：验证 → 保存文件到目标位置 → 创建记录 → 触发解析 → 删除临时文件
```

- 文件大小限制：100MB
- 支持的文件类型：.txt, .md, .docx, .pdf, .json, .xml, .csv, .xlsx
- 失败日志：`logs/batch_import_failed.log`
- 失败时保留临时文件（`temp/batch_import/`），方便手动处理

### 6.3 目录结构映射

```
scripts/upload/
├── 研发技术/
│   ├── Django/
│   │   └── tutorial.md
│   └── Python/
│       └── basics.md
└── 行政办公/
    └── employee_handbook.md

→ 自动创建/复用节点：研发技术 → Django/Python，行政办公
```

### 6.4 辅助命令

```bash
# 列出所有可用节点
docker compose exec django python scripts/batch_import_docs.py --list-nodes

# 列出所有可用部门
docker compose exec django python scripts/batch_import_docs.py --list-departments
```

---

## 七、环境变量配置

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
PG_DB_DATABASE=rag_agent
PG_DB_USER=rag_user
PG_DB_PASSWORD=rag_pass_2026

# --- Redis ---
REDIS_DB_HOST=localhost
REDIS_DB_PORT=6379
REDIS_DB_PASSWORD=
REDIS_DB_DB=0

# --- LLM 配置（支持双模型）---
LLM_API_KEY=sk-your-llm-api-key-here
LLM_BASE_URL=https://api.deepseek.com
LLM_BASE_MODEL=deepseek-v4-flash         # 基础模型
LLM_ADVANCED_MODEL=deepseek-v4-pro       # 高级模型
LLM_TIMEOUT=60

# --- Embedding & Rerank ---
EMBEDDING_API_KEY=sk-your-embedding-api-key-here
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_DIM=1024
RERANK_MODEL=BAAI/bge-reranker-v2-m3

# --- Embedding Provider 切换开关 ---
# docker: 优先使用 Docker Embedding 服务, API 兜底
# api:    优先使用云 API, 本地 docker 兜底
EMBEDDING_PROVIDER=docker

# --- Docker Embedding 配置（本地部署时使用）---
# EMBEDDING_DOCKER_URL=http://localhost:8080/embed
# EMBEDDING_DOCKER_TIMEOUT=30

# --- 文档存储配置 ---
DOCUMENT_STORAGE_MODE=local          # local / oss
DOCUMENT_RETENTION_ENABLED=1         # 是否保留原始文件（1保留/0删除）
DOCUMENT_MAX_SIZE_MB=100             # 单个文件最大大小（MB）

# --- OSS 配置（DOCUMENT_STORAGE_MODE=oss 时必填）---
# OSS_ENDPOINT=https://oss-cn-hangzhou.aliyuncs.com
# OSS_ACCESS_KEY_ID=your-access-key-id
# OSS_ACCESS_KEY_SECRET=your-access-key-secret
# OSS_BUCKET_NAME=your-bucket-name
# OSS_REGION=oss-cn-hangzhou
```

---

## 八、权限系统规则

### 8.1 角色体系

| 角色编码 | 角色名称 | 核心权限 |
|---------|---------|---------|
| super_admin | 超级管理员 | 所有权限，含系统配置、人员管理、文档管理 |
| kb_admin | 知识库管理员 | 所有文档的管理权限，无人员管理 |
| audit_admin | 审计管理员 | 审计日志查看权限 |
| user_admin | 用户管理员 | 用户管理权限 |
| dept_manager | 部门经理 | 部门内人员管理，部门级文档管理 |
| team_leader | 组长 | 团队内人员管理，团队级文档管理 |
| employee | 普通员工 | 个人文档CRUD+上传，部门/团队文档只读 |
| readonly | 只读用户 | 仅文档只读权限 |

### 8.2 文档可见性

| 可见性 | 级别 | 说明 | 默认访问范围 |
|--------|------|------|-------------|
| 私有 (private) | 1 | 个人文档 | 仅所有者、文档管理员、超级管理员 |
| 部门 (department) | 2 | 部门文档 | 部门所有成员、部门经理、文档管理员、超级管理员 |
| 团队 (team) | 3 | 团队文档 | 团队所有成员、组长、文档管理员、超级管理员 |
| 公开 (public) | 4 | 全局文档 | 所有用户可读 |

### 8.3 权限判定规则

1. **超级管理员**：直接放行所有权限（除了迁移他人 personal 文档）
2. **文档管理员**：全部文档权限（除了迁移他人 personal 文档）
3. **文档所有者**：对自己上传的文档拥有完整操作权限（无论可见范围）
4. **文档可见性匹配**：按可见性范围授权，无权限继承关系
5. **临时授权**：检查 document_permission 表

### 8.4 上传权限

- 除 `readonly` 外，所有角色都有上传权限
- 上传时可选择可见范围：private / department / team / public
- 上传者始终是文档的所有者

### 8.5 文档迁移规则

1. **归属链**：文档的归属链由上传者决定，不可改变。归属链格式：部门 → 团队 → 上传者
2. **迁移方向**：只能沿归属链向下迁移：public → department → team → private
3. **跨域限制**：不能将文档迁移到上传者归属链之外的部门/团队
4. **所有者不变**：文档的 owner 始终是上传者，迁移时不可改变
5. **迁移权限**：
   - super_admin/kb_admin：可迁移到上传者归属链内的任意级别
   - dept_manager：可迁移到本部门内的团队或个人级别
   - team_leader：可迁移到本组内的个人级别
   - owner：可迁移到归属链内的任意级别

### 8.6 public 文档管理权限

- public 文档的管理权仅限于上传者的归属链内
- 部门经理只能管理本部门员工上传的 public 文档
- 组长只能管理本组员工上传的 public 文档
- 其他部门/团队的人只有 read 权限

### 8.7 private 文档保护

- 管理员可以 read 和 delete private 文档（安全管理）
- 管理员不能 edit、migrate、share private 文档（保护个人隐私）

---

## 九、测试用例

```bash
# 运行 API 测试（需先启动 Django 服务器）
docker compose exec django python tests/test_api_simple.py
```

---

## 十、二次开发建议

1. **接入生产级向量库**：当前 `apps.retrieval.vector_store` 走 pgvector，可切换 Milvus/Qdrant，只改 `upsert_vector` / `vector_search` 两个函数。
2. **接入正式 Rerank**：`apps.retrieval.rerank.rerank_docs` 已抽象签名，可换 BGE-Reranker-v2 或 Cohere API。
3. **多租户**：`SysUser` 已有 `department`，`Document` 已有 `visibility` + `owner_team_id` 快照，扩展 tenant_id 即可。
4. **前端替换**：`static/` 目录是极简演示，正式项目建议直接 Vue3 + Element Plus 重写。
