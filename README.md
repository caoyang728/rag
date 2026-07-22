# RAG-Agent 企业级知识库平台

> Django 5 + DRF + Celery + PostgreSQL(pgvector) + Redis + DeepSeek 全栈落地，一个面向企业内部的、可解释、可审计、可运维的 RAG 问答系统。

---

## 一、快速启动（3 行命令）

```bash
# 1. 克隆仓库并进入根目录
cd RAGDemo

# 2. 复制并按需修改环境变量（LLM_API_KEY 至少要填一个真 key，否则走 stub）
cp .env.example .env && vim .env

# 3. 一键启动 web + celery + postgres + redis + nginx
docker compose up -d --build

# 首次启动后建议：
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python scripts/seed_demo.py   # 载入演示数据
```

启动完成后：
- Web UI（极简 SPA）：<http://localhost>
- API：<http://localhost/api/v1/>
- Django Admin：<http://localhost/admin/>
- Healthz：<http://localhost/healthz>

---

## 二、目录结构

```
RAGDemo/
├── apps/
│   ├── users/                  # 自定义 SysUser + RBAC（角色/权限/部门/团队）
│   ├── knowledge/              # 节点树 + 文档 + 分块 + 多格式解析器
│   │   └── parsers/            # pdf/docx/code 三套解析器
│   ├── retrieval/              # 向量库 + BM25 + 混合检索（RRF 融合）+ Rerank
│   ├── llm/                    # DeepSeek Provider + 抽象工厂 + Prompt 模板
│   ├── memory/                 # 四层记忆（short/session/user/global）
│   ├── agent/                  # 问答编排 + 任务拆分 + 流式回答
│   ├── chat/                   # 会话 / QA 记录 / 反馈 / 热点缓存
│   ├── audit/                  # 审计日志（sha256 哈希链防篡改）
│   ├── security/               # IP 黑白名单 / 登录失败锁定 / 敏感词
│   ├── analytics/              # 关键词权重 / 准确率日报 / 趋势统计
│   ├── notification/           # 邮件订阅 / 发送日志
│   └── system/                 # 系统配置 / Celery 任务日志 / LLM 调用日志
├── rag_project/                # Django 项目配置、根 URL、Celery、ASGI/WSGI
├── scripts/
│   ├── seed_demo.py            # 演示数据初始化
│   ├── init_db.sql             # pgvector 扩展 + 初始 schema
│   └── init_db_remote.py       # 远程数据库初始化
├── static/                     # 前端静态资源（开发源码）
│   ├── css/                    # 公共 common.css + 各页面 page.css
│   ├── js/                     # 页面模块：chat / upload / admin-analytics / admin-audit
│   └── *.html                  # 各页面 HTML 入口
├── tests/                      # API 测试用例
├── nginx/                      # Nginx 反代
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```

---

## 三、十大技术亮点（代码位置一览）

| # | 亮点 | 关键文件 |
|---|------|----------|
| 1 | **四类知识库根节点 + 三级可见性** —— 企业文档 / 代码知识库 / 通用推理 / 运维故障；私有/团队/公开/系统 | `apps/knowledge/models.py`（KnowledgeNode, Document.visibility） |
| 2 | **多格式解析器 + 语义感知切片** —— PDF/DOCX/Code(AST)，切片保 section_path 溯源 | `apps/knowledge/parsers/*.py`、`apps/knowledge/chunker.py` |
| 3 | **敏感信息脱敏** —— 身份证/手机/邮箱/AK 正则组合，命中写 `desensitized_hits` 元数据 | `apps/knowledge/desensitizer.py` |
| 4 | **RRF 混合检索** —— 向量召回30 + BM25召回30 → RRF 融合 → Rerank Top5，两路并发执行 | `apps/retrieval/hybrid.py` |
| 5 | **pgvector 向量库 + HNSW 索引** —— HNSW/IVFFlat 双索引选型建议在注释中 | `apps/retrieval/vector_store.py`、`scripts/init_db.sql` |
| 6 | **四层记忆架构** —— Redis 短时 + 会话摘要 + 用户偏好 + 全局知识；定时提炼由 Celery 触发 | `apps/memory/manager.py`、`apps/memory/tasks.py` |
| 7 | **复杂任务拆分（Agent）** —— LLM 输出 JSON 子任务列表，逐个检索+回答，最终合并 | `apps/agent/task_splitter.py`、`executor.py` |
| 8 | **审计日志哈希链** —— sha256(prev_hash + row_payload) 链式存储 + 校验接口 | `apps/audit/models.py`、`apps/audit/views.py` |
| 9 | **热点缓存 + 关键词权重** —— (question_hash, root_type, visibility_scope) 三键；BM25 按 weight_score 加权 | `apps/chat/models.py::HotQaCache`、`apps/analytics/models.py::KeywordWeight` |
| 10 | **全链路可观测** —— 每条 QA 记录检索命中 chunk_id、分阶段耗时、Token、成本；LlmCallLog 独立表 | `apps/chat/models.py::QaRecord`、`apps/system/models.py::LlmCallLog` |

---

## 四、核心 API 概览

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/v1/auth/login/` | 登录，返回 `{access, refresh, user}` |
| POST | `/api/v1/auth/logout/` | 登出（refresh 加黑名单） |
| GET/PATCH | `/api/v1/auth/profile/` | 当前用户资料 |
| POST | `/api/v1/auth/change-password/` | 修改密码 |
| CRUD | `/api/v1/auth/users/` `/roles/` `/permissions/` `/departments/` `/teams/` | RBAC 管理 |
| GET | `/api/v1/knowledge/nodes/tree/?root_type=` | 节点树 |
| CRUD | `/api/v1/knowledge/nodes/` | 节点管理 |
| POST | `/api/v1/knowledge/documents/upload/` | 文档上传（multipart，sha256 去重，异步解析） |
| GET | `/api/v1/knowledge/documents/` | 文档列表 |
| GET | `/api/v1/knowledge/documents/{id}/` | 文档详情 |
| POST | `/api/v1/knowledge/documents/{id}/reparse/` | 重新解析 |
| DELETE | `/api/v1/knowledge/documents/{id}/` | 删除文档 |
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

## 五、技术栈

| 层 | 选型 | 理由 |
|-----|------|------|
| Web | Django 5.2 + DRF | 企业级 Web 框架，Admin/ORM 加速开发 |
| 鉴权 | rest_framework_simplejwt | JWT 无状态，配合 refresh + blacklist |
| 异步 | Celery 5.4 + Redis | 文档解析、记忆提炼、日报都走队列 |
| DB | PostgreSQL 16 + pgvector | 结构化 + 向量一站式，避免额外维护 Milvus |
| 缓存 | Redis 7 | 短时记忆 / 会话 / Celery broker |
| LLM | DeepSeek Chat/Reasoner | 国内合规 + 成本可控；Provider 抽象保留切换 GPT/Claude 空间 |
| 嵌入 | BGE-M3 (SiliconFlow) | 高性能中文嵌入模型，通过环境变量配置 |
| Rerank | BGE-Reranker-v2 (SiliconFlow) | 轻量级重排序模型 |
| 前端 | 原生 JS + Hash 路由（极简 SPA） | 演示够用；生产可换 Vue/React |

---

## 六、测试用例

```bash
# 运行 API 测试（需先启动 Django 服务器）
python tests/test_api_simple.py
```

---

## 七、二次开发建议

1. **接入生产级向量库**：当前 `apps.retrieval.vector_store` 走 pgvector，可切换 Milvus/Qdrant，只改 `upsert_vector` / `vector_search` 两个函数。
2. **接入正式 Rerank**：`apps.retrieval.rerank.rerank_docs` 已抽象签名，可换 BGE-Reranker-v2 或 Cohere API。
3. **多租户**：`SysUser` 已有 `department`，`Document` 已有 `visibility` + `owner_team_id` 快照，扩展 tenant_id 即可。
4. **前端替换**：`static/` 目录是极简演示，正式项目建议直接 Vue3 + Element Plus 重写。

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

| 可见性 | 说明 | 默认访问范围 |
|--------|------|-------------|
| personal | 个人文档 | 仅所有者、文档管理员、超级管理员 |
| team | 团队文档 | 团队所有成员、组长、文档管理员、超级管理员 |
| department | 部门文档 | 部门所有成员、部门经理、文档管理员、超级管理员 |
| all | 全局文档 | 所有用户可读 |

### 8.3 权限判定规则

1. **超级管理员**：直接放行所有权限（除了迁移他人 personal 文档）
2. **文档管理员**：全部文档权限（除了迁移他人 personal 文档）
3. **文档所有者**：对自己上传的文档拥有完整操作权限（无论可见范围）
4. **文档可见性匹配**：按可见性范围授权，无权限继承关系
5. **临时授权**：检查 document_permission 表

### 8.4 上传权限

- 除 `readonly` 外，所有角色都有上传权限
- 上传时可选择可见范围：all / department / team / personal
- 上传者始终是文档的所有者

### 8.5 文档迁移规则

1. **归属链**：文档的归属链由上传者决定，不可改变。归属链格式：部门 → 团队 → 上传者
2. **迁移方向**：只能沿归属链向下迁移：all → department → team → personal
3. **跨域限制**：不能将文档迁移到上传者归属链之外的部门/团队
4. **所有者不变**：文档的 owner 始终是上传者，迁移时不可改变
5. **迁移权限**：
   - super_admin/kb_admin：可迁移到上传者归属链内的任意级别
   - dept_manager：可迁移到本部门内的团队或个人级别
   - team_leader：可迁移到本组内的个人级别
   - owner：可迁移到归属链内的任意级别

### 8.6 all 文档管理权限

- all 文档的管理权仅限于上传者的归属链内
- 部门经理只能管理本部门员工上传的 all 文档
- 组长只能管理本组员工上传的 all 文档
- 其他部门/团队的人只有 read 权限

### 8.7 personal 文档保护

- 管理员可以 read 和 delete personal 文档（安全管理）
- 管理员不能 edit、migrate、share personal 文档（保护个人隐私）

---

## 九、目录里都有啥？

- 12 个 Django app，覆盖用户/权限/知识库/检索/LLM/记忆/Agent/审计/安全/看板/通知/系统。
- 50+ 条 REST API，覆盖登录、RBAC、节点树、上传、问答、反馈、拆分、审计、日报、健康检查。
- 3 套文件解析器：PDF / DOCX / 代码（AST 抽取符号）。
- 4 层记忆架构 + 混合检索 + 哈希链审计，构成本仓库的三大核心技术支柱。

---

**Enjoy hacking. PR & Star welcome.**