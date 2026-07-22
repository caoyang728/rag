# CLAUDE.md

> **AI 编码助手上下文文件**（Vibe Coding 专用）。放在项目根目录，Cursor / Claude Code / Cody 等工具会自动读取。
> 目的：让 AI 在没看完整个仓库前就能理解**项目定位、边界、约定和亮点**，写出与本项目一致的代码。

---

## 0. 一句话看懂这个项目

**企业私有化多场景智能 RAG-Agent 统一知识库平台**——面向企业内部，把散落在 PDF / Word / Markdown / 代码仓 / 配置文件中的知识，统一入库、按权限检索、由 LLM 生成可溯源的答案，并全链路可审计。

> ⚠️ 这是**技术展示项目**。所有代码在追求"能跑通闭环"的同时，还要保留**十大亮点**（见 §5）可演示、可溯源；不要因为重构方便就把亮点简化掉。

---

## 1. 技术栈（严格锁定，不要引入替代方案）

| 层 | 选型 | 版本 | 备注 |
|---|---|---|---|
| 后端框架 | Django + DRF | 5.2 / 3.15 | 自定义用户模型 `users.SysUser` |
| 数据库 | PostgreSQL + pgvector | pg16 | 向量维度 **1024**，HNSW 索引 |
| 缓存 & Broker | Redis | 7 | 短时记忆、热点缓存、Celery broker |
| 异步任务 | Celery | 5.4 | 队列分层：`parse` / `default` / `beat` |
| LLM | **DeepSeek**（默认） | OpenAI 兼容 | 走 `apps.llm.providers.LLMProvider` 抽象，切 qwen/glm/vllm/ollama 只改 provider |
| Embedding & Rerank | SiliconFlow BGE | m3 / v2-m3 | 免费额度够用，本地 sentence-transformers 备份 |
| 前端 | 原生 HTML + JS + Hash 路由 | — | **禁止引入 React/Vue/Tailwind**，保持零构建 |
| 部署 | Docker Compose | — | web + celery + celery-beat + postgres + redis + nginx |
| 认证 | djangorestframework-simplejwt | 5.3 | JWT，access 30 分钟，refresh 7 天 |
| 密码哈希 | argon2-cffi | — | 不要退回 pbkdf2 |
| 分词 | jieba | — | BM25 用，繁体转简体后分词 |

---

## 2. 目录结构（记住这个心智模型）

```
rag-agent-platform/
├── backend/
│   ├── rag_project/              # 项目级配置
│   │   ├── settings.py         # ⭐ 唯一 settings，所有环境变量在此收敛
│   │   ├── urls.py             # 挂载所有子路由（/api/v1/...）
│   │   ├── celery.py           # Celery app 定义
│   │   └── {asgi,wsgi}.py
│   ├── apps/                   # 12 个业务 app，各司其职（详见 §3）
│   ├── templates/pages/        # Django 模板（SPA 入口 index.html）
│   ├── static/                  # 前端静态资源（开发源码）
│   │   ├── css/                 #   公共 base.css + 各页面 page.css
│   │   └── js/
│   │       ├── lib/             #   基础库：utils.js / api.js / router.js
│   │       └── pages/           #   页面模块：{chat,documents,audit,settings,login}.js
│   ├── staticfiles/             # collectstatic 收集目标（生产 Nginx 直出）
│   ├── scripts/seed_demo.py    # 演示数据（admin/admin12345 + 节点 + 角色）
│   ├── manage.py
│   ├── Dockerfile
│   └── entrypoint.sh
├── nginx/                      # Nginx 反代（/ → Django，/static/ & /media/ 直出）
├── scripts/init_db.sql         # 建库 + create extension vector
├── docker-compose.yml          # 6 服务：web / celery / celery-beat / postgres / redis / nginx
├── .env.example                # ⭐ 所有环境变量在此说明
├── requirements.txt
└── README.md
```

**记忆口诀**：`rag_project` = 配置中心 / `apps/*` = 业务 / `templates` + `static` = 前端 / `scripts` = 一次性脚本 / 顶层 = 部署。

---

## 3. 12 个 App 各自的边界

严格按边界改代码，**不要跨 app 直接 import ORM 模型时绕过外键**，也不要把业务逻辑写到 views 里去。

| App | 职责 | 关键模型 | 关键文件 |
|---|---|---|---|
| **users** | 身份 + RBAC | SysUser / Department / Team / Role / Permission / UserRole / UserTeam | `models.py` / `permissions.py`（含 `has_permission` 判断） |
| **knowledge** | 节点树 + 文档 + 切片 + 解析 | KnowledgeNode / Document / DocumentChunk / CodeChunk | `parsers/*.py` / `chunker.py` / `desensitizer.py` / `tasks.py`（异步链） |
| **retrieval** | 向量库 + BM25 + 混合检索 + Rerank | VectorEntry | `vector_store.py` / `bm25.py` / `hybrid.py` / `rerank.py` / `permission.py` |
| **llm** | LLM & Embedding 抽象层 | LLMCallLog | `providers/{base,deepseek,stubs}.py` / `factory.py` / `embedding.py` / `prompts/*.py` |
| **memory** | 四层记忆 | SessionMemory / UserMemory / GlobalMemory | `manager.py`（预算裁剪） / `short_term.py`（Redis LIST） / `tasks.py` |
| **agent** | 问答编排 + 任务拆分 + 流式 | AgentTask | `executor.py` / `task_splitter.py` / `streamer.py` |
| **chat** | 会话 / QA 记录 / 反馈 / 热点 | ChatSession / QaRecord / Feedback / HotQuery | `views.py` 里的 **ChatAskView 是核心闭环入口** |
| **audit** | 审计日志（哈希链） | AuditLog | `middleware.py`（拦截所有写操作） / `views.py` 里 **VerifyChainView** |
| **security** | IP 风控 + 登录失败锁定 | IpWhitelist / IpBlacklist / LoginAttempt / SecurityIncident | `middleware.py` / `tasks.py`（自动解封） |
| **analytics** | 关键词权重 + 准确率日报 | KeywordWeight / DailyStat | 目前主要是 stat 接口，可视化未做 |
| **notification** | 邮件订阅 + 发送日志 | EmailSubscription / EmailSendLog | v1 stub，走 stdout |
| **system** | 系统配置 + 任务日志 | SystemConfig / CeleryTaskLog | `views.py` 里 HealthView / StatsView |

---

## 4. 核心数据流（写代码前先对齐）

### 4.1 文档上传解析（异步）

```
用户 POST /api/v1/documents/upload/  (multipart)
  ↓ DocumentUploadView：sha256 去重 → 存 media/ → 建 Document(status=pending)
  ↓ 触发 Celery: knowledge.parse_document(doc_id)   ── queue=parse
  ↓
[Celery worker]  parse_document
  ├─ status=parsing    → get_parser(file_type).parse() 出 blocks
  ├─ status=desensitizing → desensitize()（脱手机/身份证/邮箱/银行卡）
  ├─ status=chunking   → chunk_blocks() 语义切片
  ├─ status=embedding  → get_embedding_client().embed() → upsert_vector()
  └─ status=done       ✅
```

**改这条链要小心**：状态机字段是 `Document.status`，前端和 QA 都会读；扩展新状态要同步改 `apps/knowledge/models.py` 里的 choices 和前端 `documents.js`。

### 4.2 用户问答闭环（同步 + 流式）

```
POST /api/v1/chat/ask/  { session_id, question, mode, node_ids }
  ↓ ChatAskView
  ├─ 1. permission.filter_node_ids(user, node_ids)     ── 权限过滤
  ├─ 2. HotQuery 缓存查询 → 命中直接返回
  ├─ 3. memory_manager.assemble_context(user, session) ── 组装四层记忆
  ├─ 4. hybrid_search(question, node_ids)              ── BM25 + Vector + RRF
  ├─ 5. rerank(top_20 → top_5)                         ── BGE Rerank
  ├─ 6. build_prompt(qa_template, context, memory)
  ├─ 7. LLMProvider.chat() → DeepSeek                  ── ⭐ 可切私有化
  ├─ 8. 落库 QaRecord + AuditLog（哈希链）
  ├─ 9. 更新 SessionMemory + short_term (Redis)
  └─ 返回 { answer, citations, message_id }
```

**记住这个 9 步**，任何"给答案加点什么"的需求都能落到某一步上。

---

## 5. 十大技术亮点（⚠️ 代码要保得住这些）

**改代码时优先保留这些位置的可演示性。**AI 若想重构以下文件，请先在 diff 说明中标注是否影响关键亮点逻辑。

| # | 亮点 | 代码位置 | 关键技术点 |
|---|---|---|---|
| 1 | **LLM Provider 适配层** | `apps/llm/providers/base.py` + `deepseek.py` + `factory.py` | 抽象基类 → 一行 env 切换公有/私有；stub 保底 |
| 2 | **四层记忆管理** | `apps/memory/manager.py` + `short_term.py` | short/session/user/global + token 预算反向裁剪（先砍短时→会话→用户→全局） |
| 3 | **BM25 + Vector 混合召回** | `apps/retrieval/hybrid.py` + `bm25.py` | RRF 融合公式 `1/(k+rank)`，k=60；jieba 中文分词 |
| 4 | **BGE Rerank** | `apps/retrieval/rerank.py` | 20 召回 → 5 精排；SiliconFlow API + 本地兜底 |
| 5 | **pgvector 权限过滤 SQL** | `apps/retrieval/permission.py` + `vector_store.py` | 先按 `readable_node_ids` 缩窗再算 cosine，避免全表扫 |
| 6 | **审计哈希链** | `apps/audit/middleware.py` + `models.py`（`AuditLog.save`） + `views.VerifyChainView` | `hash = sha256(prev_hash + payload)`，任一条被改则 chain 断 |
| 7 | **数据脱敏** | `apps/knowledge/desensitizer.py` | 4 类正则（手机/身份证/邮箱/银行卡），入库前脱敏，可讲误伤率 |
| 8 | **文档解析异步链** | `apps/knowledge/tasks.py` + `parsers/*.py` | 状态机 6 态，autoretry_for=Exception，失败落 error_message |
| 9 | **复杂任务拆分 Agent** | `apps/agent/task_splitter.py` + `executor.py` | LLM 判定是否需要拆分 → 子任务列表 → 顺序执行合成 |
| 10 | **IP 风控 & 登录锁定** | `apps/security/middleware.py` + `tasks.py` | 白名单优先 → 黑名单实时拦 → 连续失败 N 次封 M 分钟自动解封 |

---

## 6. 关键代码约定（AI 写代码时必须遵守）

### 6.1 命名
- **App 名**：全小写复数或功能名（`users`、`knowledge`、`retrieval`），已固定，禁改
- **模型类**：CamelCase 单数（`KnowledgeNode`、`AuditLog`）
- **DB 表名**：默认 `{app}_{model_lower}`，不用 `Meta.db_table` 自定义
- **URL 路径**：`/api/v1/{资源复数}/` kebab-case（如 `/api/v1/knowledge-nodes/`）
- **权限 code**：`{module}.{action}`（如 `document.upload`、`user.admin`）
- **Celery 任务名**：`{app}.{verb}_{object}`（如 `knowledge.parse_document`）

### 6.2 API 响应格式（统一）
```json
// 成功
{ "code": 0, "message": "ok", "data": {...} }

// 失败
{ "code": 40001, "message": "参数校验失败", "details": {...} }
```
错误码规则：`4xxxx` 客户端错，`5xxxx` 服务端错。走 `apps.users.exceptions.custom_exception_handler`，**不要**在 view 里手拼 error 响应。

### 6.3 权限装饰
```python
from apps.users.permissions import perm_class, IsAdmin

class DocumentUploadView(APIView):
    permission_classes = [perm_class('document.upload')]
```
**不要**用原生 `@permission_required`，也不要在 view 内手写 `if user.is_xxx`。

### 6.4 异步任务
- 所有耗时 >500ms 或涉及外部 API 的操作**必须**走 Celery
- 任务必须 `autoretry_for=(Exception,)` + `retry_backoff=True` + `max_retries=2`
- 用 `queue='parse'` 走解析队列，`queue='default'` 走通用队列
- 任务返回值必须是可 JSON 序列化的 dict，便于 `django-celery-results` 落库

### 6.5 LLM 调用
**永远**通过 `apps.llm.factory.get_llm_client()` 拿 client，禁止直接 `openai.OpenAI()`：
```python
from apps.llm.factory import get_llm_client
client = get_llm_client()  # 自动读 env LLM_PROVIDER
answer = client.chat(messages=[...], temperature=0.3, stream=False)
```

### 6.6 Prompt 模板
所有 prompt 集中在 `apps/llm/prompts/*.py`，用 `Template` 或 f-string 拼接，**不要**把长 prompt 硬编码在 view 里。

### 6.7 数据库迁移
- 改 model 后必须 `python manage.py makemigrations {app}` 生成 migration 文件
- migration 文件**必须提交**到仓库
- 加字段用 `null=True + default=...`，不写强制 not null 破坏已有数据

### 6.8 审计
以下动作**自动**走 `AuditMiddleware` 落审计日志：`POST/PUT/PATCH/DELETE` 到 `/api/v1/*`。想加新的 action 分类，改 `apps/audit/middleware.py` 的 `_ACTION_MAP`。

### 6.9 前端
- **零构建**：不要 `npm init`，不要引入 webpack/vite
- 前端 SPA 入口是 Django 模板 `backend/templates/pages/index.html`，由 `rag_project/urls.py` 的 `TemplateView` 渲染
- CSS 按功能拆分：`static/css/base.css`（公共组件样式）+ 各页面独立 CSS 文件
- JS 按职责拆分：
  - `static/js/lib/utils.js` — 全局工具函数（esc / fmt / toast / debounce / fileSize）
  - `static/js/lib/api.js` — HTTP 客户端（一次性请求 + SSE 流式 + Fetch ReadableStream 流式）
  - `static/js/lib/router.js` — 轻量 Hash 路由（on / guard / go / dispatch）
  - `static/js/pages/{name}.js` — 页面渲染模块，用 `Router.on(hash, renderFn)` 注册路由
- 生产部署时 `collectstatic` 将所有静态文件收集到 `STATIC_ROOT`（`staticfiles/`），由 Nginx 直接托管 `/static/` 路径
- 公共 API 调用走 `window.API`，公共工具走 `window.Utils`，路由走 `window.Router`

---

## 7. 数据库关键表（36 张，按域看）

**用户与权限域**：`users_sysuser` / `users_department` / `users_team` / `users_role` / `users_permission` / `users_userrole` / `users_userteam` / `users_rolepermission`

**知识域**：`knowledge_knowledgenode`（树状，`path` 字段闭包） / `knowledge_document` / `knowledge_documentchunk` / `knowledge_codechunk` / `knowledge_nodepermission`

**检索域**：`retrieval_vectorentry`（**vector(1024)** + HNSW 索引，`m=16, ef_construction=64, ef_search=40`）

**记忆域**：`memory_sessionmemory` / `memory_usermemory` / `memory_globalmemory`（短时在 Redis LIST，TTL 3600s）

**对话域**：`chat_chatsession` / `chat_qarecord` / `chat_feedback` / `chat_hotquery`

**审计域**：`audit_auditlog`（`prev_hash` + `hash` 组成链）

**安全域**：`security_ipwhitelist` / `security_ipblacklist` / `security_loginattempt` / `security_securityincident`

**其他**：`llm_llmcalllog` / `agent_agenttask` / `system_systemconfig` / `system_celerytasklog` / `analytics_keywordweight` / `analytics_dailystat` / `notification_emailsubscription` / `notification_emailsendlog`

---

## 8. 关键常量（改前想清楚）

| 常量 | 值 | 位置 | 改动影响 |
|---|---|---|---|
| VECTOR_DIM | 1024 | pg 建表 + `.env` EMBEDDING_MODEL | 改 = 全库重跑向量化 |
| HNSW m / ef_construction / ef_search | 16 / 64 / 40 | `init_db.sql` + settings | 建表参数需重建索引 |
| RRF_K | 60 | `apps/retrieval/hybrid.py` | 融合权重曲线 |
| DEFAULT_CHUNK_SIZE / OVERLAP | 500 / 50 | `apps/knowledge/chunker.py` | 改 = 全库重切 |
| SHORT_TERM_TTL | 3600 | `apps/memory/short_term.py` | Redis TTL |
| MEMORY_TOKEN_BUDGET | 8000 | settings | 超出反向截断 |
| MAX_LOGIN_FAIL / BAN_DURATION_MIN | 5 / 15 | settings | 风控灵敏度 |
| JWT ACCESS_TOKEN_LIFETIME / REFRESH | 30min / 7d | settings.SIMPLE_JWT | 前端也要同步 |

---

## 9. 环境变量（速查）

```bash
# 一定要填
LLM_API_KEY=sk-xxx        # 不填走 stub provider（能启动，答案是假的）
SILICONFLOW_API_KEY=sk-xxx     # 不填走本地 sentence-transformers（第一次要下模型）

# 可切
LLM_PROVIDER=deepseek          # deepseek / qwen / glm / vllm / ollama
IMAGE_STORAGE_MODE=base64      # base64（开发）/ oss（生产 stub）
DEBUG=1                        # 生产设 0
```
完整列表见 `.env.example`。**新增 env 必须同步更新 `.env.example` 和 `settings.py`**。

---

## 10. 启动与常用运维命令

```bash
# 首次启动
docker compose up -d --build
docker compose exec web python manage.py migrate
docker compose exec web python scripts/seed_demo.py   # 默认账号 admin / admin12345

# 日常
docker compose logs -f web              # 后端日志
docker compose logs -f celery           # 异步任务日志
docker compose exec web python manage.py shell     # Django shell
docker compose exec web python manage.py createsuperuser
docker compose exec postgres psql -U rag_user -d rag_agent   # 直连库

# 重建索引（改了 HNSW 参数后）
docker compose exec postgres psql -U rag_user -d rag_agent -c "REINDEX INDEX retrieval_vectorentry_embedding_idx;"

# 清 celery
docker compose restart celery celery-beat
```

---

## 11. 常见开发任务的操作模式（How-to）

### 加一个新的 REST API
1. 在对应 app 加/改 `serializers.py`
2. 在 `views.py` 加 View（继承 `APIView` / `ViewSet` / 泛型视图）
3. 在 app 的 `urls.py` 注册路由（走 DefaultRouter 更好）
4. **检查 `config/urls.py` 是否已 include 了这个 app 的 urls**
5. 加 `permission_classes = [perm_class('xxx.xxx')]`
6. 如果是写操作，确认 URL 匹配到 `apps/audit/middleware.py` 的 `_ACTION_MAP`
7. 前端在 `frontend/js/pages/{page}.js` 调 `apiGet/apiPost`

### 加一个新的文档格式解析器
1. 在 `apps/knowledge/parsers/` 新建 `xxx_parser.py`，继承 `BaseParser`，实现 `parse()`
2. 输出格式：`[{type, content, section_path, page_number, extra}]`
3. 在 `parsers/__init__.py` 或 `base.py` 的 `get_parser()` 里注册后缀
4. 无需改 `tasks.py`，异步链会自动 pick up

### 加一个新的 LLM Provider
1. 在 `apps/llm/providers/` 新建 `xxx.py`，继承 `BaseLLMProvider`，实现 `chat/embed/rerank`
2. 在 `factory.py` 的 dispatcher 里注册
3. `.env` 加对应 `XXX_API_KEY` / `XXX_BASE_URL`
4. **测试**：`docker compose exec web python -c "from apps.llm.factory import get_llm_client; print(get_llm_client().chat([{'role':'user','content':'hi'}]))"`

### 加一个 Celery 定时任务
1. 在对应 app 的 `tasks.py` 用 `@shared_task` 定义
2. 在 `config/celery.py` 的 `beat_schedule` 里加调度
3. 重启 celery-beat：`docker compose restart celery-beat`

### 加一个前端页面
1. 在 `backend/static/js/pages/` 新建 `xxx.js`，用 IIFE 封装
2. 在模块末尾调用 `Router.on('xxx', renderFn, { title: '标题' })` 注册路由
3. 如需独立 CSS，在 `backend/static/css/xxx.css` 创建
4. 在 `backend/templates/pages/index.html` 中按需添加 `<link>` 和 `<script>` 引用

### 加一个模型字段
1. 改 `models.py`，**新增字段一律 `null=True, blank=True` 或给 default**
2. `python manage.py makemigrations {app}`
3. 提交 migration 文件
4. 改对应 `serializers.py` 加字段
5. 如需可搜索/过滤，改 `views.py` 的 filterset

---

## 12. 已知限制 & TODO（AI 若被要求"补全"某项，参考这里）

- [ ] **图片 OSS 存储**：`IMAGE_STORAGE_MODE=oss` 分支未实现，当前仅 base64
- [ ] **报表可视化**：`analytics` 只有 stats 接口，未做 ECharts 前端
- [ ] **邮件推送**：`notification` 是 stdout stub，未接真 SMTP
- [ ] **pytest 用例**：`backend/tests/` 目录存在但未落用例，可补 smoke test
- [ ] **前端流式**：`ChatAskView` 目前同步返回，SSE 流式路径 `/api/v1/chat/stream/` 未接
- [ ] **多租户**：v1 单租户，Organization 模型已建但未启用租户隔离中间件
- [ ] **知识图谱**：PRD 有提到，v1 未实现
- [ ] **模型评估**：LLM-as-Judge 评估流水线未搭
- [ ] **CI/CD**：无 GitHub Actions

---

## 13. 代码设计原则（AI 编写/重构时的分寸感）

这项目的**设计理念**是：
> "从零设计并实现一个企业级 RAG 平台。技术上涵盖 LLM Provider 抽象适配、四层记忆管理、BM25+向量混合召回+BGE 精排、pgvector 权限过滤 SQL、审计哈希链、异步解析状态机等 10 个亮点；工程上采用 Django + Celery + PG(pgvector) + Redis + Docker Compose 一键部署，前端极简 SPA 零构建。"

**AI 编写/修改代码时**：
- ✅ 保持代码"能讲清为什么这么写"，比如 RRF 融合就写 RRF 不要换成"分数平均"
- ✅ 复杂算法注释里写"⭐ 技术亮点：..."
- ❌ 不要引入不必要的中重型依赖（如 Airflow、Kafka、Elasticsearch），除非明确要求
- ❌ 不要把亮点代码抽象成"调用某个 lib 的一行"，保持关键逻辑在项目源码内可翻阅

---

## 14. 修改这些文件时特别注意

| 文件 | 陷阱 |
|---|---|
| `rag_project/settings.py` | 改 MIDDLEWARE 顺序会破坏审计/IP 风控。**IpFilterMiddleware 必须在 AuditMiddleware 前** |
| `backend/apps/users/models.py` | `SysUser` 是 `AUTH_USER_MODEL`，改字段需要 migration 并重启所有服务 |
| `backend/apps/audit/models.py` | `AuditLog.save()` 内有哈希链逻辑，**不要**bulk_create 绕过 save |
| `backend/apps/retrieval/vector_store.py` | `upsert_vector` 用了 pgvector 特定语法，改 ORM 会失效 |
| `static/js/lib/api.js` | 401 拦截会自动跳登录，改这里注意别死循环 |
| `docker-compose.yml` | postgres 用了 `pgvector/pgvector:pg16` 镜像，改镜像要重装扩展 |
| `scripts/init_db.sql` | 建库同时 `CREATE EXTENSION vector`，第一次起容器时执行 |

---

## 15. AI 编码时的边界（Rule of Thumb）

1. **看 §3 找归属**：新功能先想"这属于哪个 app？"，不清楚就问，不要新建 app
2. **看 §4 找断点**：调 bug 时按 9 步流程逐步 print/log 定位到具体环节
3. **看 §5 保亮点**：重构前先看是不是十大亮点之一，是的话保留可讲性
4. **看 §6 守约定**：命名、响应格式、权限、异步、Prompt 全部照 §6
5. **看 §11 学模式**：加 API/模型/解析器/Provider/任务，都有既定路径
6. **看 §12 别越界**：TODO 里的东西，只有我明确要求才做，不要自作主张补齐

**遇到不确定的**：直接问兜风，或者留 `# TODO(claude): 待确认...` 注释，不要自己拍板决策。

---

*Last updated: 2026-07-15*
*Maintainer: 兜风 & 追风*

---

> 本内容由 Coze AI 生成，请遵循相关法律法规及《人工智能生成合成内容标识办法》使用与传播。
