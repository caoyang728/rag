# RAG-Agent 企业级知识库平台

> Django 5 + DRF + Celery + PostgreSQL(pgvector) + Redis + DeepSeek 全栈落地，一个面向企业内部的、可审计、可运维、可评估的 RAG 问答系统。

---

## 一、项目特性

- **混合检索 + Rerank**：pgvector 向量检索 + BM25 关键词检索 + RRF 融合 + BGE-Reranker 重排序，二次权限过滤保证安全
- **四层权限模型**：可见范围（team/dept/public）+ 黑名单/白名单/跨团队授权 + 双审流程 + 权限申请双轨制
- **流式问答**：SSE 流式响应，TTFB/总延迟展示，AbortController 终止，断线自动保存部分回答
- **文档全生命周期**：多格式解析（PDF/DOCX/Markdown/代码/表格/PPT）→ 智能切分 → 向量化 → 版本管理 + 软删除
- **PDF 深度解析**：PyMuPDF `find_tables()` 表格提取 + 跨页表格合并 + 图片提取（base64 + OSS 双存储）
- **RAG 质量评估中心**：黄金测试集 + 6 维度 LLM-as-Judge + 离线检索评估（Recall@K/MRR/NDCG）+ 文档质量量化 + 知识库覆盖率 + 反馈闭环
- **系统监控**：P50/P95/P99 延迟百分位（预计算 + 直方图）、Celery 队列深度（5 分钟快照）、组织使用报表、实时指标（Redis 5 分钟刷新）
- **审计可追溯**：哈希链防篡改审计日志、文档操作日志（20 种 action）、敏感词过滤、IP 黑白名单、登录锁定
- **四层记忆**：short/session/user/global 记忆，每晚提炼稳定用户偏好

---

## 二、快速启动

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

启动完成后，默认通过 Django 直接对外提供服务（端口 8000）：

| 服务 | 开发/调试地址（直连 Django） | 生产环境地址（经 Nginx 反代） |
|------|------------------------------|-------------------------------|
| Web UI（极简 SPA） | <http://localhost:8000> | <http://your-domain/> |
| API | <http://localhost:8000/api/v1/> | <http://your-domain/api/v1/> |
| Django Admin | <http://localhost:8000/admin/> | <http://your-domain/admin/> |
| Healthz | <http://localhost:8000/healthz> | <http://your-domain/healthz> |

> 说明：上表中带 `:8000` 的地址是直连 Django 容器、不经过 Nginx，仅用于本地开发与调试；生产环境应通过 Nginx 反向代理统一对外暴露（默认 80/443），不要在对外地址中保留 `:8000` 端口。

---

## 三、目录结构

```
rag/
├── apps/
│   ├── users/                  # 用户系统（User + RBAC 权限 + 部门/团队 + 文档级权限）
│   │   ├── models.py           # User(AbstractBaseUser), Department, Team, Role, Permission + DocDenyUser/DocAllowUser/DocCrossTeam/AccessApplication
│   │   ├── signals.py          # 部门/团队变更 → 自动同步 KnowledgeNode 树 + 缓存失效
│   │   └── views.py            # 部门/团队 CRUD（含删除保护：有成员/有文档→禁止删除）
│   ├── knowledge/              # 知识节点 + 文档 + 切片 + 多格式解析器
│   │   ├── models.py           # KnowledgeNode(固定4层树) + Document(版本管理+软删除) + DocumentChunk + CodeChunk + ImageResource + DocOperationLog
│   │   ├── access.py           # resolve_doc_access() — 7 步优先级权限判定（支持多团队）
│   │   ├── node_sync.py        # 部门/团队 ↔ KnowledgeNode 双向同步 + 子树文档计数
│   │   ├── parsers/            # pdf / docx / markdown / code / spreadsheet / presentation / config 七套解析器
│   │   │   └── pdf_parser.py   # PyMuPDF find_tables() 表格提取 + 跨页合并 + 图片 base64
│   │   ├── storage.py          # 文档存储抽象层（local/OSS，按节点路径存储）
│   │   └── tasks.py            # Celery 任务（解析、向量化、批量导入、图片保存）
│   ├── retrieval/              # pgvector 向量检索 + BM25 关键词 + 混合检索（RRF 融合）+ Rerank
│   │   ├── models.py           # DocumentVector（pgvector HNSW 索引 + 权限冗余字段）
│   │   ├── vector_store.py     # 向量检索封装（cosine 距离 + hnsw.ef_search 会话级调节）
│   │   ├── bm25.py             # BM25 关键词检索（jieba 分词 + keyword_weight 加权）
│   │   ├── permission.py       # build_permission_q() — 检索级权限过滤（黑/白名单）
│   │   └── hybrid.py           # 混合检索 + 图片 base64 注入 chunk.extra
│   ├── llm/                    # DeepSeek Provider + 抽象工厂 + Prompt 模板
│   ├── memory/                 # 四层记忆（short/session/user/global），每晚提炼用户偏好
│   ├── agent/                  # 问答编排 + 任务拆分 + 流式回答 + 引用合并
│   ├── chat/                   # 会话 / QA 记录（含 TTFB/Token 速率/错误类型） / 反馈 / 热点缓存
│   ├── audit/                  # 审计日志（sha256 哈希链防篡改）
│   ├── security/               # 验证码 / IP 黑白名单 / 登录失败锁定 / 敏感词
│   ├── analytics/              # 关键词权重 / 准确率日报 / 趋势统计 / 系统监控 / RAG 质量评估
│   │   ├── models.py           # 13 个模型：日报/系统指标/组织报表/队列深度/忠实度 + 8 个质量评估模型
│   │   ├── evaluation_engine.py # 6 维度 LLM-as-Judge 评估引擎（Faithfulness/Relevance/Completeness/Correctness/Harmlessness/ContextRecall）
│   │   ├── offline_eval.py     # 黄金测试集 + 离线检索评估（Recall@K/MRR/NDCG + 各阶段增益）
│   │   ├── doc_quality.py      # 文档入库质量量化（解析/切分/向量化质量 + 综合评分 0-100）
│   │   ├── coverage.py         # 知识库覆盖率 + 反馈闭环自动化（差评自动关联 chunk）
│   │   ├── realtime.py         # Redis 实时指标（5 分钟刷新，独立 DB 避免干扰）
│   │   ├── tasks.py            # Celery 任务（9 个定时任务：指标聚合/评估/清理）
│   │   └── views.py            # 16+ 个 View 类，覆盖监控 + 质量评估全套接口
│   ├── notification/           # 邮件订阅 / 发送日志
│   └── system/                 # 系统配置 / Celery 任务日志 / LLM 调用日志
├── rag_project/                # Django 项目配置、根 URL、Celery、ASGI/WSGI
│   └── celery.py               # 6 队列 + 13 个 Beat 定时任务
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
│   │   ├── common.js           # 通用请求封装（token / 401 / 流式 / 侧边栏 / 模态框）
│   │   ├── multi-select.js     # 多选下拉组件（搜索 + 全选 + 部门→团队联动）
│   │   ├── chat.js             # 会话问答（流式 + AbortController + TTFB 展示）
│   │   ├── upload.js           # 文档上传（版本管理 + 三选项对话框 + 历史轮询）
│   │   ├── login.js            # 登录（含验证码）
│   │   ├── profile.js          # 个人资料
│   │   ├── reset-password.js   # 修改密码
│   │   ├── admin-users.js      # 用户管理
│   │   ├── admin-org.js        # 组织架构管理（部门/团队）
│   │   ├── admin-rbac.js       # 角色权限管理
│   │   ├── admin-nodes.js      # 知识节点管理（动态加载根类型）
│   │   ├── admin-analytics.js  # 统计分析
│   │   ├── admin-eval.js       # 质量评估中心（6 Tab：黄金集/检索/回答/文档/覆盖率/反馈）
│   │   └── admin-audit.js      # 审计日志
│   ├── index.html              # 首页
│   ├── login.html              # 登录页
│   ├── chat.html               # 问答页
│   ├── upload.html             # 文档上传页
│   ├── profile.html            # 个人资料页
│   ├── reset-password.html     # 修改密码页
│   ├── admin-users.html        # 用户管理页
│   ├── admin-org.html          # 组织架构管理页
│   ├── admin-rbac.html         # 角色权限管理页
│   ├── admin-nodes.html        # 知识节点管理页
│   ├── admin-analytics.html    # 统计分析页
│   ├── admin-eval.html         # 质量评估中心页
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

## 四、核心 API 概览

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
| POST | `/api/v1/knowledge/documents/upload/` | 文档上传（sha256 去重 + 版本管理 + 异步解析） |
| GET | `/api/v1/knowledge/documents/` | 文档列表（支持 discover 模式） |
| GET | `/api/v1/knowledge/documents/{id}/` | 文档详情 |
| GET | `/api/v1/knowledge/documents/pending/` | 待处理文档列表（轮询状态） |
| GET | `/api/v1/knowledge/documents/{id}/chunks/` | 文档分块列表 |
| GET/PATCH | `/api/v1/knowledge/documents/{id}/visibility/` | 查看/修改文档可见范围 |
| POST | `/api/v1/knowledge/documents/{id}/reparse/` | 重新解析 |
| DELETE | `/api/v1/knowledge/documents/{id}/` | 删除文档（软删除） |
| GET | `/api/v1/knowledge/documents/{id}/raw/` | 文档原文预览（50MB 限制，分页 5000 字符/页） |

### 检索 & 问答

| Method | Path | 说明 |
|--------|------|------|
| POST | `/api/v1/chat/ask/` | **核心问答接口**（SSE 流式 + TTFB + 终止控制） |
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
| GET | `/api/v1/security/ip-whitelist/` `/ip-blacklist/` | IP 黑白名单 |
| GET | `/api/v1/system/health/` | 健康检查 |
| GET | `/api/v1/system/stats/` | 首页看板 |

### 系统监控（analytics）

| Method | Path | 说明 |
|--------|------|------|
| GET | `/api/v1/analytics/overview/` | 概览统计 |
| GET | `/api/v1/analytics/trend/?days=` | 趋势报表 |
| GET | `/api/v1/analytics/qa-records/` | 问答记录（需 `analytics:system:read` 权限） |
| GET | `/api/v1/analytics/system-metrics/` | 系统指标日报（P50/P95/P99 + 直方图 + 错误分布） |
| GET | `/api/v1/analytics/org-usage/` | 组织使用报表（部门/团队维度） |
| GET | `/api/v1/analytics/queue-depth/` | Celery 队列深度（实时 + 历史 7 天趋势） |
| GET | `/api/v1/analytics/realtime/` | 实时指标快照（5 分钟刷新 + last_flush_at） |
| GET | `/api/v1/analytics/quality-reports/` | 忠实度评估报告 |
| GET | `/api/v1/analytics/bad-feedbacks/` | 差评反馈列表 |

### RAG 质量评估（analytics）

| Method | Path | 说明 |
|--------|------|------|
| CRUD | `/api/v1/analytics/golden-datasets/` | 黄金测试集管理（含版本管理） |
| POST | `/api/v1/analytics/golden-datasets/{id}/import/` | 批量导入测试问题（JSON） |
| GET | `/api/v1/analytics/golden-datasets/{id}/export/` | 导出测试集 |
| CRUD | `/api/v1/analytics/golden-datasets/{id}/questions/` | 测试问题管理（含相关文档 + 参考答案） |
| POST | `/api/v1/analytics/eval/retrieval/` | 触发离线检索评估（Recall@K/MRR/NDCG） |
| POST | `/api/v1/analytics/eval/answer/` | 触发回答质量评估 |
| GET | `/api/v1/analytics/eval/retrieval-reports/` | 检索评估报告列表 |
| POST | `/api/v1/analytics/doc-quality/evaluate/` | 触发文档质量评估 |
| GET | `/api/v1/analytics/doc-quality/reports/` | 文档质量报告列表 |
| POST | `/api/v1/analytics/multi-dim-eval/` | 触发 6 维度回答评估（LLM-as-Judge） |
| GET | `/api/v1/analytics/multi-dim-scores/` | 多维度评估得分 |
| POST | `/api/v1/analytics/coverage/generate/` | 生成知识库覆盖率报告 |
| GET | `/api/v1/analytics/coverage/reports/` | 覆盖率报告列表 |
| GET | `/api/v1/analytics/coverage/reports/{id}/export/` | 导出覆盖率报告 |
| GET | `/api/v1/analytics/feedback-loop/` | 反馈闭环（差评自动关联 chunk） |

---

## 五、技术栈

| 层 | 选型 | 理由 |
|-----|------|------|
| Web | Django 5.2 + DRF | 企业级 Web 框架，Admin/ORM 加速开发 |
| 鉴权 | rest_framework_simplejwt | JWT 无状态，配合 refresh + blacklist |
| 异步 | Celery 5.4 + Redis | 文档解析、记忆提炼、日报、批量导入、质量评估都走队列 |
| DB | PostgreSQL 16 + pgvector | 结构化 + 向量一站式，避免额外维护 Milvus |
| 缓存 | Redis 7 | 短时记忆 / 会话 / 验证码 / Celery broker / 权限缓存 / 实时指标（独立 DB） |
| LLM | DeepSeek Chat/Reasoner | 国内合规 + 成本可控；Provider 抽象保留切换空间 |
| 嵌入 | BGE-M3 (SiliconFlow) | 高性能中文嵌入模型，1024 维 |
| Rerank | BGE-Reranker-v2 (SiliconFlow) | 轻量级重排序模型 |
| PDF 解析 | PyMuPDF | 原生支持 `find_tables()` 表格提取 + 跨页合并 + 图片提取 |
| 前端 | 原生 JS + Hash 路由（极简 SPA） | 演示够用；生产可换 Vue/React |

---

## 六、数据库表概览

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
| UserTeam | `user_account_team_rel` | 用户↔团队关联（支持多团队） |
| DocDenyUser | `doc_deny_user` | 文档黑名单（最高优先级，对超管也生效） |
| DocAllowUser | `doc_allow_user` | 文档个人白名单（含 expire_time） |
| DocCrossTeam | `doc_cross_team` | 跨团队授权（含 expire_time） |
| AccessApplication | `access_application` | 统一权限申请单（双轨：申请拉 + 授权推） |

### 知识库（knowledge）

| 模型 | 表名 | 说明 |
|------|------|------|
| KnowledgeNode | `knowledge_node` | 固定 4 层树（KB→部门→团队→分类），Level 4+ 无限层级 |
| Document | `knowledge_document` | 文档元数据（visible_scope 三档 + 版本管理 + 软删除） |
| DocumentChunk | `knowledge_document_chunk` | 文档切片（表格类型不二次切分） |
| CodeChunk | `knowledge_code_chunk` | 代码切片（AST 解析） |
| ImageResource | `knowledge_image` | 图片资源（base64/OSS 双存储） |
| DocOperationLog | `knowledge_doc_operation_log` | 文档操作审计日志（20 种 action） |

### 检索（retrieval）

| 模型 | 表名 | 说明 |
|------|------|------|
| DocumentVector | `retrieval_doc_vector` | pgvector HNSW 索引 + 权限冗余字段 |

### 监控与质量评估（analytics）

| 模型 | 表名 | 说明 |
|------|------|------|
| KeywordWeight | `analytics_keyword_weight` | BM25 关键词动态权重（好评 +0.1 / 差评 -0.1） |
| AccuracyReport | `analytics_accuracy_report` | 准确率日报 |
| SystemMetricsReport | `analytics_system_metrics_report` | 系统指标日报（P50/P95/P99 + 直方图 + 错误分布） |
| OrgUsageReport | `analytics_org_usage_report` | 组织使用报表（部门/团队维度，UPSERT） |
| QueueDepthLog | `analytics_queue_depth_log` | Celery 队列深度快照（5 分钟） |
| AnswerQualityReport | `analytics_answer_quality_report` | 忠实度评估报告（便宜模型 + 成本控制） |
| GoldenDataset | `analytics_golden_dataset` | 黄金测试集（含版本管理） |
| GoldenQuestion | `analytics_golden_question` | 黄金测试问题（含相关文档 + 参考答案） |
| GoldenRelevantDoc | `analytics_golden_relevant_doc` | 测试问题相关文档标注（high/medium/low） |
| GoldenReferenceAnswer | `analytics_golden_reference_answer` | 测试问题参考答案 + 关键点 |
| MultiDimensionScore | `analytics_multi_dimension_score` | 6 维度回答评估（Faithfulness/Relevance/Completeness/Correctness/Harmlessness/ContextRecall） |
| DocumentQualityReport | `analytics_document_quality_report` | 文档入库质量报告（解析/切分/向量化 + 综合评分 0-100） |
| RetrievalQualityReport | `analytics_retrieval_quality_report` | 检索质量报告（Recall@K/MRR/NDCG + 各阶段增益） |
| CoverageReport | `analytics_coverage_report` | 知识库覆盖率报告（热门覆盖 + 知识空白 + 重复检测） |

---

## 七、权限系统规则

### 7.1 角色体系（6 种）

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

> 内置角色 code 不可修改：前端编辑时 readOnly，后端 update 接口拦截。

### 7.2 节点结构（固定 4 层 + 自定义分类）

```
一级：知识库根节点 (kb root)
  └─ 二级：部门节点 (dept)           ← 由 Department 生命周期自动管理
      └─ 三级：团队节点 (team)       ← 由 Team 生命周期自动管理
          └─ 四级+：业务分类 (category) ← 由团队组长手动管理，无层级上限
```

- Level 1-3（KB/部门/团队）：不可通过节点 API 直接 CRUD，由部门/团队生命周期自动同步
- Level 4+（业务分类）：通过节点 API 自由创建/修改/删除（删除时需节点下无文档）
- 文档挂载在业务分类节点下；部门/团队归属由 `dept_node_id`/`team_node_id` 自动填充

### 7.3 文档可见范围（三档）

| 可见范围 | 含义 | 默认访问者 |
|---------|------|-----------|
| team | 仅归属团队 | 同团队成员 + 所有者 + 管理员 |
| dept | 归属全部门 | 同部门所有成员 + 所有者 + 管理员 |
| public | 全公司公开 | 所有登录用户 |

### 7.4 文档双层审核流程

```
上传 → 系统预检 → 待团队组长一审 (pending_team)
                      ↓
           待合规审核员二审 (pending_compliance)
                      ↓
              双审通过 (passed) → 可被检索
```

super_admin 可绕过双审直接发布。

### 7.5 访问权限判定（7 步优先级，命中即停）

1. 黑名单拦截（DocDenyUser）→ 全部拒绝（最高优先级，对超管也生效）
2. 所有者 / super_admin / kb_admin → 放行
3. visible_scope='public' → 放行
4. visible_scope='dept' 且同部门 → 放行
5. visible_scope='team' 且同团队（支持多团队） → 放行
6. 跨团队授权命中（DocCrossTeam，未过期）→ 放行
7. 个人白名单命中（DocAllowUser，未过期）→ 放行

否则拒绝。检索层与文档层均做权限过滤，Agent 在混合检索后做二次权限校验。

### 7.6 权限申请双轨制

- **轨道 1（申请拉取）**：用户提交 AccessApplication → 审批 → 插入白名单/跨团队记录
- **轨道 2（授权推送）**：组长/管理员直接操作 DocAllowUser/DocCrossTeam 表

### 7.7 部门/团队删除保护

- **删除团队**：团队下无成员 AND 团队节点及子树下无文档 → 允许；否则提示具体数字
- **删除部门**：部门下无用户 AND 无团队 → 允许；否则提示具体数字
- **删除分类节点（Level 4+）**：该节点及全部子孙节点下无文档 → 允许

---

## 八、文档存储架构

### 8.1 存储模式

通过环境变量 `DOCUMENT_STORAGE_MODE` 切换：
- `local`：本地文件系统（默认）
- `oss`：云 OSS / MinIO

### 8.2 按节点路径存储

```
media/documents/
└── node-{id}_{name}/          # 根节点目录
    └── node-{id}_{name}/      # 子节点目录
        └── {uuid}_{filename}  # 文档文件
```

### 8.3 安全措施

- MIME 类型验证（python-magic 库，防止文件类型绕过）
- 文件名净化（django.utils.text.get_valid_filename，去除路径分隔符和控制字符）
- sha256 文件哈希去重，防止重复上传
- raw_content 预览 50MB 限制 + 分页（默认 5000 字符/页，可在 1000-20000 调整）

### 8.4 文档版本管理

- 软删除（`is_deleted=True`）替代物理删除
- `(node, file_name, version_tag)` 组合对未删除记录唯一
- 上传支持 `version_tag` 参数，未指定时自动生成 v1/v2/...
- 同用户同版本冲突时：返回冲突或 `force_upload` 软删原记录后新传

---

## 九、Celery 定时任务（Beat）

| 任务 | 调度 | 说明 |
|------|------|------|
| `system-metrics-daily` | 每日 02:00 | 聚合前一天 P50/P95/P99、缓存命中率、错误率 |
| `org-usage-daily` | 每日 02:10 | 部门/团队对话、Token、费用聚合（UPSERT） |
| `refine-user-memory` | 每日 02:30 | 提炼稳定的用户偏好到长期记忆 |
| `cleanup-old-analytics-data` | 每日 03:30 | 清理过期监控数据（低峰期） |
| `doc-quality-daily` | 每日 04:00 | 批量评估文档质量（解析/切分/向量化） |
| `coverage-report-daily` | 每日 04:30 | 生成知识库覆盖率报告 |
| `faithfulness-evaluation` | 每小时整点 | 忠实度评估（成本受 .env 控制） |
| `multi-dim-evaluation` | 每 2 小时（30 分） | 6 维度回答质量评估（与忠实度错开） |
| `handle-feedback` | 每小时（15 分） | 处理未处理差评反馈 |
| `periodic-retrieval-eval` | 每周一 05:00 | 离线检索回归测试（黄金测试集） |
| `queue-depth-snapshot` | 每 5 分钟 | Celery 队列深度快照（PG 历史 + Redis 实时） |
| `realtime-metrics-flush` | 每 5 分钟 | 刷新实时指标时间戳 |
| `expire-ip-blacklist` | 每 5 分钟 | 清理过期临时 IP 封禁 |

队列划分：`default / parse / memory / email / analytics`，analytics 独立队列避免监控任务与业务问答争抢 Worker。

```bash
# 单 Worker（全队列）
celery -A rag_project worker -l info

# 独立监控 Worker
celery -A rag_project worker -l info -Q analytics
```

---

## 十、RAG 质量评估中心

> 入口：侧边栏「质量评估」→ `admin-eval.html`。6 个 Tab 页 + KPI 卡片 + 数据可视化 + 报告表格，支持手动触发与定时自动执行。

### 10.1 黄金测试集
- 创建/编辑/删除测试集（含版本管理）
- 批量导入/导出测试问题（JSON 格式）
- 每个问题标注：相关文档（high/medium/low）+ 参考答案 + 关键点
- 推荐规模：200-500 个典型业务问题

### 10.2 离线检索评估
- 指标：Recall@5/10/20、MRR、NDCG@5/10
- 各阶段增益分析：纯向量 / 纯 BM25 / 混合 RRF / Rerank
- 评估时配置快照（top_k / rrf_k / chunk_size 等）
- 检索漏召分析：未命中任何相关文档的问题统计

### 10.3 多维度回答质量评估
LLM-as-Judge，6 维度评分（0-1）：

| 维度 | 含义 |
|------|------|
| Faithfulness | 忠实度（回答是否基于 context，无幻觉） |
| Relevance | 相关性（回答是否切中问题要害） |
| Completeness | 完整性（是否覆盖 context 关键点） |
| Correctness | 正确性（是否存在事实错误） |
| Harmlessness | 无害性（是否安全合规） |
| Context Recall | 上下文召回率（context 是否包含所需信息） |

- 使用便宜模型（如 deepseek-chat）控制成本
- 支持原子级事实核查（atomic facts 逐一验证）
- 自一致性：多次评估取平均降低随机性

### 10.4 文档质量评估
- 解析质量：文本提取率、表格保留率、图片提取率
- 切分质量：chunk 数量、平均大小、标准差、分布均匀性
- 向量化质量：embedding 成功率、失败 chunk 数
- 综合评分 0-100（解析 0.4 + 切分 0.3 + 向量化 0.3）
- 问题诊断：自动列出 warning 级别问题清单

### 10.5 知识库覆盖率
- 热门问题覆盖率（Top 100 查询命中率）
- 知识空白检测（某领域查询长期无命中）
- 重复切片检测
- 领域覆盖分析：按部门 → 团队层级分组，统计文档数 / 切片数 / 占比 / 查询命中率

### 10.6 反馈闭环自动化
- 差评自动关联到命中的 chunk
- 智能生成处理建议（重新切分 / 重新入库 / 补充文档）
- 反馈处理追踪（resolved 状态）
- 反馈有效性验证（有反馈 vs 无反馈请求的质量差异）

---

## 十一、系统监控

### 11.1 性能指标
- 延迟百分位：P50/P95/P99（端到端 / LLM / 检索 / TTFB）
- 缓存命中请求与正常请求分别统计，避免亚毫秒延迟稀释百分位
- 延迟直方图（按 100ms 分桶）
- Token 生成速率（tokens_per_second）
- 错误分布（timeout / rate_limit / network 等分类）

### 11.2 实时指标
- Redis 原子 INCR + 5 分钟刷新
- 独立 DB（默认 DB 3）避免与 broker/result_backend 冲突
- 包含：总 QA / 缓存命中 / LLM 错误 / 成本估算 / last_flush_at

### 11.3 队列深度
- Redis LLEN 实时查询（O(1)）+ PostgreSQL 历史存储（保留 7 天）
- 同时记录 Worker 数量与任务类型
- 同一队列同一分钟唯一约束，防止 Beat 重入产生重复数据

### 11.4 组织使用报表
- 部门级汇总（team_id=-1 哨兵值）+ 团队明细两种粒度
- UPSERT 保证重复执行不产生重复数据
- 指标：QA 次数 / 活跃用户 / Token / 费用 / 平均延迟 / P95 / 好评率 / 缓存命中率

---

## 十二、批量导入

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

## 十三、环境变量配置

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

# --- 质量评估（成本控制）---
EVAL_MODEL=deepseek-chat
EVAL_BATCH_SIZE=20
EVAL_DAILY_LIMIT=200
EVAL_DAILY_COST_LIMIT=10.0
EVAL_ENABLED=true
```

完整变量列表见 `.env.example`。

---

## 十四、测试

```bash
docker compose exec django python tests/test_api_simple.py
```

---

## 十五、二次开发建议

1. **检索向量库**：当前走 pgvector，可切换 Milvus/Qdrant，只改 `upsert_vector` / `vector_search`
2. **Rerank**：已抽象签名 `rerank_docs`，可换 Cohere API
3. **多租户**：`Document` 已有 `dept_node_id`/`team_node_id`，扩展 tenant_id 即可
4. **前端替换**：`static/` 为极简演示，正式项目建议 Vue3 + Element Plus
5. **质量评估模块拆分**：当前在 analytics app 内通过 4 个独立 Python 文件实现隔离；当模型数增长到 30+ 或代码量超过 3000 行时，可拆分为独立 app（只需 move 文件 + 改 INSTALLED_APPS，成本极低）
6. **A/B 测试框架**：top_k / rrf_k / chunk_size 等参数可扩展为生产流量灰度对比
