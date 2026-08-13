/* ==========================================================
   知库 Agent · 系统配置页面 (admin-system-config.js)
   功能：加载 KV 配置项、按分类分组展示、行内编辑保存
   依赖：common.js（STATE/$/$$/toast/escapeHtml）、api.js、layout.js
   ========================================================== */

// 配置分类中文名 + 图标映射（与后端 SystemConfig.CATEGORY_CHOICES 对应）
const CATEGORY_MAP = {
	llm: { label: 'LLM 模型', icon: '🤖' },
	embedding: { label: 'Embedding/Rerank', icon: '🧮' },
	retrieval: { label: '检索参数', icon: '🔍' },
	storage: { label: '存储', icon: '💾' },
	email: { label: '邮件 SMTP', icon: '📧' },
	agent: { label: 'Agent', icon: '🧩' },
	security: { label: '安全', icon: '🛡️' },
	memory: { label: '记忆', icon: '🧠' },
	analytics: { label: 'Analytics', icon: '📊' },
	eval: { label: '评估', icon: '🎯' },
	knowledge: { label: '知识构建', icon: '📚' },
};

// 配置项元数据映射：前端硬编码 label/description/unit/sortOrder
// DB 中仅存储 key + value + value_type + is_secret + is_readonly + risk_level
// 前端通过此映射获取展示信息，减少 API 响应体积
const CONFIG_METADATA = {
	// ===== LLM =====
	LLM_BASE_MODEL: { label: '基础模型', description: '用于简单任务，节约 token，速度快', sortOrder: 1 },
	LLM_ADVANCED_MODEL: { label: '高级模型', description: '用于复杂任务，推理能力强', sortOrder: 2 },
	LLM_TIMEOUT: { label: 'LLM 调用超时', description: '调用 LLM 接口的超时时间，超时后请求中断并返回错误', unit: '秒', sortOrder: 3 },

	// ===== 知识构建 =====
	GRAPH_ENABLED: { label: '图谱抽取', description: '文档解析完成后是否自动抽取知识图谱（实体/关系），关闭时标记未启用并跳过', sortOrder: 1 },
	WIKI_ENABLED: { label: 'Wiki 生成', description: '文档解析完成后是否自动生成节点 Wiki 页面，关闭时标记未启用并跳过', sortOrder: 2 },

	// ===== Embedding / Rerank =====
	EMBEDDING_MODEL: { label: 'Embedding 模型名', description: '用于文档向量化的模型标识，需与模型管理中的 model_name 一致', sortOrder: 1 },
	EMBEDDING_DIM: { label: '向量维度', description: 'Embedding 向量维度，修改后需重建向量索引，仅限 .env 修改', sortOrder: 2 },
	RERANK_MODEL: { label: 'Rerank 模型名', description: '用于检索结果重排序的模型标识，需与模型管理中的 model_name 一致', sortOrder: 3 },
	EMBEDDING_PROVIDER: { label: 'Embedding Provider', description: '选择向量模型的服务方式：本地 Docker 或云 API', sortOrder: 4 },
	EMBEDDING_DOCKER_URL: { label: 'Docker 服务地址', description: 'EMBEDDING_PROVIDER=docker 时使用的本地服务地址', sortOrder: 5 },
	EMBEDDING_DOCKER_TIMEOUT: { label: 'Docker 调用超时', description: '本地 Docker 服务调用超时时间', unit: '秒', sortOrder: 6 },

	// ===== 检索参数 =====
	RETRIEVAL_TOP_K: { label: '混合检索召回 top K', description: '向量+BM25 混合检索后合并去重的候选文档数量，再进入 Rerank 阶段', sortOrder: 1 },
	RETRIEVAL_RERANK_TOP_K: { label: 'Rerank 后保留 top K', description: 'Rerank 重排序后最终返回给 LLM 的文档数量，过大可能引入噪声，过小可能遗漏信息', sortOrder: 2 },
	HNSW_EF_SEARCH: { label: 'HNSW 向量搜索 ef 参数', description: 'HNSW 索引搜索时的 ef 参数，值越大召回越准但速度越慢，需权衡平衡', sortOrder: 3 },
	BM25_TOP_K: { label: 'BM25 召回 top K', description: 'BM25 关键词检索召回的候选文档数量，与向量召回合并后进入 Rerank', sortOrder: 4 },
	VECTOR_TOP_K: { label: '向量召回 top K', description: '向量相似度检索召回的候选文档数量，与 BM25 召回合并后进入 Rerank', sortOrder: 5 },
	RETRIEVAL_MIN_RERANK_SCORE: { label: 'Rerank 相关性阈值', description: 'Rerank 重排序后，分数低于该值（0-1）的片段视为与问题无关直接丢弃，防止无关文档作为引用返回；0=不过滤。向量与 BM25 召回的最终相关性统一由该阈值把关', sortOrder: 6 },
	QUERY_TRANSFORM_ENABLED: { label: '查询改写/分解开关', description: '开启后，检索前先对用户 Query 做 LLM 改写/同义词扩展，改写后置信度不足时再拆分为多个子查询分别召回后合并；关闭时行为与现状一致', sortOrder: 7 },
	QUERY_DECOMPOSE_THRESHOLD: { label: '改写后置信度阈值', description: '改写后检索结果的置信度低于该值时触发查询分解（0-1），越低越容易触发分解；0.35 大致对应改写后无命中片段', sortOrder: 8 },
	QUERY_DECOMPOSE_MAX_SUB: { label: '最大子查询数', description: '查询分解时最多生成的子查询数量（1-5），防止过度拆分导致检索延迟过高', sortOrder: 9 },

	// ===== 存储 =====
	IMAGE_STORAGE_MODE: { label: '图片存储模式', description: '图片的存储方式：转换 base64 存入数据库或对象存储', sortOrder: 1 },
	DOCUMENT_STORAGE_MODE: { label: '文档存储模式', description: '文档的存储方式：本地文件系统或对象存储', sortOrder: 2 },
	DOCUMENT_RETENTION_ENABLED: { label: '保留原始文件', description: 'true=解析后保留原始文件；false=解析后删除以节省空间', sortOrder: 3 },
	DOCUMENT_MAX_SIZE_MB: { label: '文件大小上限', description: '单个文档上传的最大文件大小限制，超过此大小将被拒绝上传', unit: 'MB', sortOrder: 4 },
	OSS_ENDPOINT: { label: 'OSS 服务端点', description: 'DOCUMENT_STORAGE_MODE=oss 时必填', sortOrder: 5 },
	OSS_BUCKET_NAME: { label: 'OSS Bucket 名', description: 'DOCUMENT_STORAGE_MODE=oss 时必填', sortOrder: 6 },
	OSS_REGION: { label: 'OSS Region', description: 'DOCUMENT_STORAGE_MODE=oss 时必填', sortOrder: 7 },

	// ===== 邮件 SMTP =====
	EMAIL_ENABLED: { label: 'SMTP 发信', description: '是否启用 SMTP 邮件发送，false=输出到控制台', sortOrder: 1 },
	EMAIL_HOST: { label: 'SMTP 服务器地址', description: '邮件服务器的域名或 IP 地址', sortOrder: 2 },
	EMAIL_PORT: { label: 'SMTP 端口', description: '邮件服务器端口，SSL 通常用 465，TLS 通常用 587', sortOrder: 3 },
	EMAIL_USE_SSL: { label: '是否使用 SSL', description: '通过 SSL/TLS 加密连接 SMTP 服务器（端口 465）。与 TLS 二选一，优先使用 SSL', sortOrder: 4 },
	EMAIL_USE_TLS: { label: '是否使用 TLS', description: '使用 STARTTLS 升级加密连接（端口 587）。若 SMTP 服务器支持，也可开启替代 SSL', sortOrder: 5 },
	EMAIL_HOST_USER: { label: 'SMTP 发信账号', description: '用于 SMTP 认证的用户名（通常为邮箱地址）', sortOrder: 6 },
	EMAIL_FROM: { label: '发件人地址', description: '邮件显示的发件人地址', sortOrder: 7 },
	PASSWORD_RESET_TIMEOUT: { label: '密码重置有效期', description: '密码重置验证码或链接的有效时长，过期后需重新发起重置请求', unit: '秒', sortOrder: 8 },
	FRONTEND_BASE_URL: { label: '前端基础地址', description: '密码重置链接的域名前缀（如 https://rag.example.com）。使用验证码重置时不需要此配置', sortOrder: 9 },

	// ===== Agent =====
	AGENT_DEFAULT_MODE: { label: '默认问答模式', description: '新会话的默认问答模式：Agent 自主决策、传统 RAG 或强制 Agent', sortOrder: 1 },
	// 多选组件的展示文案通过 multiSelect 字段定制，避免复用 Text2SQL 白名单的硬编码文案
	BUSINESS_DB_TABLES: {
		label: 'Text2SQL 白名单', description: '多选，空=不允许任何表查询（Text2SQL 不生效）；需主动勾选表后才生效', sortOrder: 2,
		multiSelect: { emptyText: '未选择任何表（Text2SQL 不生效）', searchPlaceholder: '搜索表名...' },
	},
	// 聊天数据来源：多选且来源只有 4 项，无需搜索框（showSearch: false 隐藏）
	CHAT_SOURCE_ENABLED: {
		label: '聊天数据来源', description: '多选，聊天页「知识来源」下拉框仅展示勾选的来源；全不选时回退为全部开启', sortOrder: 3,
		multiSelect: { emptyText: '未选择任何来源（聊天页回退为全部开启）', showSearch: false },
	},

	// ===== 安全 =====
	SENSITIVE_FILTER_ENABLED: { label: '敏感词审查', description: '是否启用 LLM 输出侧的敏感词审查', sortOrder: 1 },
	SENSITIVE_FILTER_CHUNK_SIZE: { label: '审查累积字符数', description: 'LLM 流式输出时，累积多少字符后进行一次敏感词审查。过小增加开销，过大延迟违规检测', unit: '字符', sortOrder: 2 },
	SENSITIVE_FILTER_WINDOW_SIZE: { label: '滑动窗口大小', description: '敏感词审查的尾部重叠字符数，防止关键词被切割分块后漏检（如「敏感|词」被截断）', unit: '字符', sortOrder: 3 },
	SENSITIVE_FILTER_MASK_STR: { label: '脱敏替换字符串', description: '敏感词命中后替换显示的字符串', sortOrder: 4 },
	SENSITIVE_FILTER_RELOAD_TTL: { label: '词库缓存 TTL', description: '敏感词库在内存中的缓存时长，超过后自动从 DB 刷新', unit: '秒', sortOrder: 5 },
	MAX_LOGIN_FAIL: { label: '登录失败次数', description: '连续登录失败达到此次数后触发账号锁定', sortOrder: 6 },
	BAN_DURATION_MIN: { label: '登录锁定时长', description: '连续登录失败达到 MAX_LOGIN_FAIL 次后，账号被锁定的时长。超时后自动解锁', unit: '分钟', sortOrder: 7 },

	// ===== 记忆 =====
	MEMORY_TOKEN_BUDGET: { label: '记忆 Token 预算', description: '会话中可注入的记忆（含用户画像、历史对话、长期记忆等）最大 Token 总量，超过时按优先级裁剪', unit: 'tokens', sortOrder: 1 },
	SHORT_TERM_TTL: { label: '短时记忆 TTL', description: '短时记忆（最近对话轮次）的保留时长，超时后不再参与上下文拼接', unit: '秒', sortOrder: 2 },
	SHORT_TERM_MAX_TURNS: { label: '短时记忆最大保留轮数', description: '最多保留最近 N 轮对话作为短时记忆注入上下文，超出的旧轮次自动丢弃', sortOrder: 3 },

	// ===== Analytics =====
	ANALYTICS_REDIS_DB: { label: '统计专用 Redis', description: 'Analytics 专用 Redis DB 编号，避免与 Celery broker/result backend 冲突', sortOrder: 1 },
	QUEUE_MONITOR_ENABLED: { label: '是否启用队列深度监控', description: '是否启用 Celery 队列深度监控，生产环境故障时可临时关闭以减压', sortOrder: 2 },
	// 检索反馈闭环：权重全局共享，两个开关标记为高风险（变更走工单+超管复核），阈值参数可直接调整
	FEEDBACK_LOOP_ENABLED: { label: '检索反馈闭环', description: '每日聚合点击/反馈并自动调整关键词权重；关闭后定时任务跳过，不影响现有排序', sortOrder: 3 },
	FEEDBACK_LOOP_AUTO_APPLY: { label: '自动应用权重调整', description: '开启则聚合后直接改权重；关闭则只记录待复核，需在运营工具逐条应用（人工复核开关）', sortOrder: 4 },
	FEEDBACK_LOOP_ADOPT_THRESHOLD: { label: '采纳率降权阈值', description: '关键词命中 chunk 的采纳率低于该值时触发基础降权', sortOrder: 5 },
	FEEDBACK_LOOP_BAD_THRESHOLD: { label: '负反馈降权阈值', description: '当日含该关键词的差评对话数达到该值时追加降权', unit: '次', sortOrder: 6 },
	FEEDBACK_LOOP_MIN_SHOW_COUNT: { label: '最小展示样本数', description: '关键词当日展示次数低于该值不调整，避免少量噪声干扰全局排序', unit: '次', sortOrder: 7 },
	FEEDBACK_LOOP_BASE_DELTA: { label: '单次调整步长', description: '采纳率低/负反馈各触发一次基础降权；点击未采纳为半降权', sortOrder: 8 },
	FEEDBACK_LOOP_MAX_DELTA: { label: '单日调整幅度上限', description: '无论命中多少条降权规则，单日单关键词实际调整幅度不超过该值（保护机制）', sortOrder: 9 },

	// ===== 评估 =====
	// 分组排序：总开关/模型 → 生产采样开关/采样率 → 分层限速（分钟/小时/日） → 成本 → 批量回扫 → 指标组 → 低分回归
	EVAL_ENABLED: { label: '启用评估', description: '控制是否允许发起评估任务（含手动和定时），关闭可节省成本', sortOrder: 1 },
	EVAL_MODEL: { label: '评估所用模型', description: '用于评估的 LLM 模型标识，需在模型管理中配置', sortOrder: 2 },
	PRODUCTION_EVAL_ENABLED: { label: '生产对话采样评估', description: '是否启用生产对话自动采样评估，默认关闭，按需开启', sortOrder: 3 },
	PRODUCTION_EVAL_SAMPLE_RATE: { label: '采样率', description: '随机对未评估的对话进行自动评估的比例，0=不评估，1=全量评估', sortOrder: 4 },
	PRODUCTION_EVAL_RATE_PER_MIN: { label: '每分钟评估上限', description: '仅限当前分钟内已发起对话的评估并发数，主要防止突发请求打爆 LLM 评估接口。', unit: '次/分', sortOrder: 5 },
	PRODUCTION_EVAL_RATE_PER_HOUR: { label: '每小时评估上限', description: '仅限当前小时内已发起对话的评估总量，将当天的评估对象分散到不同小时，避免集中在某一时段。', unit: '次/时', sortOrder: 6 },
	EVAL_DAILY_LIMIT: { label: '每日评估上限', description: '仅限当天已发起对话的评估总量，主要用于控制成本。', unit: '次', sortOrder: 7 },
	EVAL_COST_LIMIT: { label: '每日评估成本上限', description: '每日评估 LLM 调用成本上限，超出后停止评估', unit: '元', sortOrder: 8 },
	PRODUCTION_EVAL_BATCH_SIZE: { label: '2h 批量回扫每次评估条数', description: '每 2 小时回扫未评估的历史对话进行批量评估，每次处理的条数', unit: '条', sortOrder: 9 },
	// 评估维度：评估=展示强绑定，勾选的维度既参与 LLM 评估也在看板展示，未勾选的维度不评估也不展示
	EVAL_DISPLAY_DIMENSIONS: {
		label: '评估维度',
		description: '多选，评估=展示强绑定：勾选的维度既参与 LLM 评估也在 admin-eval「回答质量」页展示，未勾选的维度不评估也不展示。默认全选 12 维。降本场景可只勾选核心维度（如 faithfulness + answer_relevancy）',
		sortOrder: 10,
		multiSelect: { emptyText: '未选择任何维度（不评估也不展示，相当于关闭评估）', searchPlaceholder: '搜索维度...' },
	},
	LOW_SCORE_REGRESSION_ENABLED: { label: '低分回归', description: '是否启用低分回归测试集（沉淀+定时评估），关闭后定时任务跳过，手动触发仍可用', sortOrder: 11 },
	LOW_SCORE_REGRESSION_TOP_N: { label: '低分沉淀数量', description: '每次从历史评估中取低分 N 条沉淀到回归测试集', unit: '条', sortOrder: 12 },
	LOW_SCORE_REGRESSION_PASS_THRESHOLD: { label: '回归通过阈值', description: '回归评估 12 维均分 ≥ 此值视为通过', sortOrder: 13 },
	LOW_SCORE_REGRESSION_CAPACITY: { label: '回归测试集容量', description: '低分回归测试集的最大保留条数，超出时自动淘汰', unit: '条', sortOrder: 14 },
	LOW_SCORE_REGRESSION_SUGGEST_REMOVE_PASSES: { label: '建议移除通过数', description: '连续通过次数达到该值时前端提示建议人工 review 移除', sortOrder: 15 },
};

// 分类显示顺序（非字母序，按业务逻辑排序）
const CATEGORY_ORDER = ['llm', 'embedding', 'retrieval', 'storage', 'email', 'agent', 'security', 'memory', 'analytics', 'eval', 'knowledge'];

// 当前页面状态
let _allConfigs = {};       // 全部配置，按 category 分组：{ llm: [...], embedding: [...], ... }
let _currentCategory = '';  // 当前选中的 category
let _originalValues = {};   // 各配置项的原始值（用于对比变化和重置）：{ KEY: 'value', ... }
let _saving = false;        // 防止重复提交

/* ============ 初始化 ============ */
document.addEventListener('DOMContentLoaded', async () => {
	// 权限检查：仅超级管理员 / 维护管理员可访问
	if (!isSystemMaintainer()) {
		const tmpl = document.getElementById('tmpl-no-permission');
		document.querySelector('.layout').innerHTML = tmpl.innerHTML;
		return;
	}
	await loadConfigs();
});

/* ============ 加载配置列表 ============ */
async function loadConfigs() {
	try {
		const data = await api.getJson('/api/v1/system/configs/');
		_allConfigs = data.groups || {};
		// 缓存原始值用于对比变化和重置
		_originalValues = {};
		Object.values(_allConfigs).flat().forEach(c => {
			_originalValues[c.key] = c.value;
		});
		renderCategoryNav();
		// 默认选中第一个有数据的分类
		const firstCat = Object.keys(_allConfigs).find(k => _allConfigs[k].length > 0);
		if (firstCat) {
			selectCategory(firstCat);
		} else {
			$('#configList').innerHTML = '<div class="empty"><div class="empty-icon">📭</div><div class="empty-text">暂无配置项</div></div>';
		}
	} catch (e) {
		console.error('load configs failed:', e);
		$('#configList').innerHTML = `<div class="empty"><div class="empty-icon">❌</div><div class="empty-text">加载失败：${escapeHtml(e.message)}</div></div>`;
	}
}

/* ============ 渲染分类导航 ============ */
function renderCategoryNav() {
	const navEl = $('#categoryNav');
	// 按 CATEGORY_ORDER 预定义顺序排序，而非字母序
	const allCats = Object.keys(_allConfigs);
	const cats = allCats.sort((a, b) => {
		const ia = CATEGORY_ORDER.indexOf(a);
		const ib = CATEGORY_ORDER.indexOf(b);
		// 未在 CATEGORY_ORDER 中的分类排到最后，按字母序
		if (ia === -1 && ib === -1) return a.localeCompare(b);
		if (ia === -1) return 1;
		if (ib === -1) return -1;
		return ia - ib;
	});
	if (cats.length === 0) {
		navEl.innerHTML = '<div class="text-sub text-sm" style="padding:12px">暂无分类</div>';
		return;
	}
	navEl.innerHTML = cats.map(cat => {
		const info = CATEGORY_MAP[cat] || { label: cat, icon: '📁' };
		const count = _allConfigs[cat].length;
		const active = cat === _currentCategory ? 'active' : '';
		const itemTmpl = document.getElementById('tmpl-category-item').innerHTML;
		return itemTmpl
			.replace('__ACTIVE__', active)
			.replace(/__KEY__/g, cat)
			.replace('__ICON__', info.icon)
			.replace('__LABEL__', info.label)
			.replace('__COUNT__', count);
	}).join('');
}

/* ============ 选择分类 ============ */
function selectCategory(cat) {
	_currentCategory = cat;
	renderCategoryNav();
	renderConfigList(cat);
}

/* ============ 渲染配置项列表 ============ */
function renderConfigList(cat) {
	const listEl = $('#configList');
	const configs = (_allConfigs[cat] || []).slice(); // copy for sorting
	const info = CATEGORY_MAP[cat] || { label: cat };

	$('#currentCategoryName').textContent = info.label;
	$('#configCount').textContent = `（${configs.length} 项）`;

	if (configs.length === 0) {
		listEl.innerHTML = '<div class="empty"><div class="empty-icon">📭</div><div class="empty-text">该分类下暂无配置项</div></div>';
		return;
	}

	// 按 CONFIG_METADATA 中的 sortOrder 排序，未定义的排到最后
	configs.sort((a, b) => {
		const sa = (CONFIG_METADATA[a.key] || {}).sortOrder || 999;
		const sb = (CONFIG_METADATA[b.key] || {}).sortOrder || 999;
		return sa - sb;
	});

	const itemTmpl = document.getElementById('tmpl-config-item').innerHTML;
	listEl.innerHTML = configs.map(c => {
		// 优先使用前端 CONFIG_METADATA，兜底 API 返回值
		const meta = CONFIG_METADATA[c.key] || {};
		const label = meta.label || c.label || c.key;
		const description = meta.description || c.description || '';
		const unit = meta.unit || c.unit || '';

		// 只读 / 敏感 / 高风险标签直接构建，避免冗余的数组 + filter
		const badgeReadonly = c.is_readonly
			? '<span class="config-badge config-badge-readonly" title="修改需重建索引或影响路由，仅限 .env 修改">🔒 只读</span>'
			: '';
		const badgeSecret = c.is_secret
			? '<span class="config-badge config-badge-secret" title="敏感项，值已掩码">🔐 敏感</span>'
			: '';
		// 高风险项标识：变更需超管复核
		const badgeRisk = c.risk_level === 'high'
			? '<span class="config-badge config-badge-risk" title="高风险项，工单需复核">⚠️ 高风险</span>'
			: '';

		// __KEY_ESC__ 用于 HTML 文本/属性值，__KEY_ATTR__ 用于 id/data-key 属性
		// 统一经 escapeHtml 处理，防止属性被引号闭合或注入脚本
		const keyEscaped = escapeHtml(c.key);
		return itemTmpl
			.replace(/__KEY_ESC__/g, keyEscaped)
			.replace(/__KEY_ATTR__/g, keyEscaped)
			.replace('__LABEL__', escapeHtml(label))
			.replace('__DESC__', escapeHtml(description))
			.replace('__READONLY_CLASS__', c.is_readonly ? 'readonly' : '')
			.replace('__BADGE_READONLY__', badgeReadonly)
			.replace('__BADGE_SECRET__', badgeSecret)
			.replace('__BADGE_RISK__', badgeRisk)
			.replace('__CONTROL__', renderControl(c, unit));
	}).join('');

	// 填充最近更新时间
	configs.forEach(c => {
		if (c.updated_at) {
			const el = $(`#cfg-updated-${c.key}`);
			if (el) {
				el.textContent = `最近更新：${formatDate(c.updated_at)}`;
			}
		}
	});
}

/* ============ 多行文本处理：转义 HTML + 合并连续空白行 + 保留换行 ============ */
function formatMultiline(text) {
	if (!text) return '-';
	const escaped = escapeHtml(text);
	// 合并 3 个及以上连续换行为 2 个（即多空行压缩为单空行）
	return escaped.replace(/\n{3,}/g, '\n\n');
}

/* ============ 渲染编辑控件（按 value_type）============ */
function renderControl(c, unit) {
	const val = c.value || '';
	const disabled = c.is_readonly ? 'disabled' : '';
	// 有单位时在控件后面追加灰色标签
	const unitSuffix = unit ? `<span class="config-unit">${escapeHtml(unit)}</span>` : '';

	let control = '';
	if (c.value_type === 'bool') {
		const checked = val === 'true' ? 'checked' : '';
		control = `<label class="switch"><input type="checkbox" id="cfg-${c.key}" ${checked} ${disabled} onchange="onConfigChange('${c.key}')"><span class="slider"></span></label>`;
	} else if (c.value_type === 'int') {
		// 整数类型：限制为非负整数，防止键入小数或负数
		control = `<input type="number" class="input" id="cfg-${c.key}" value="${escapeHtml(val)}" min="0" step="1" inputmode="numeric" pattern="[0-9]*" ${disabled} oninput="onConfigChange('${c.key}')" style="max-width:200px">${unitSuffix}`;
	} else if (c.value_type === 'float') {
		control = `<input type="number" class="input" id="cfg-${c.key}" value="${escapeHtml(val)}" step="0.01" ${disabled} oninput="onConfigChange('${c.key}')" style="max-width:200px">${unitSuffix}`;
	} else if (c.value_type === 'json') {
		control = `<textarea class="input" id="cfg-${c.key}" rows="3" ${disabled} oninput="onConfigChange('${c.key}')">${escapeHtml(val)}</textarea>`;
	} else if (c.options && c.options.length > 0) {
		// 有可选值列表时渲染 select；description 含"多选"时渲染自定义多选组件
		const isMulti = c.description && c.description.includes('多选');
		if (isMulti) {
			// 自定义多选组件：紧凑显示 + 点击展开 + 搜索过滤 + 复选框
			control = renderMultiSelect(c);
		} else {
			const opts = c.options.map(o =>
				`<option value="${escapeHtml(o.value)}" ${o.value === val ? 'selected' : ''}>${escapeHtml(o.label)}</option>`
			).join('');
			control = `<select class="input" id="cfg-${c.key}" ${disabled} onchange="onConfigChange('${c.key}')" style="max-width:360px">${opts}</select>`;
		}
	} else {
		// string 或其他
		if (c.is_secret) {
			// 敏感项：显示掩码，需点击"修改"才能输入
			control = `
				<div id="cfg-secret-wrap-${c.key}">
					<div class="secret-edit-toggle" onclick="enableSecretEdit('${c.key}')">✏️ 点击修改</div>
					<input type="text" class="input" id="cfg-${c.key}" value="***" disabled style="max-width:480px">
				</div>`;
		} else {
			control = `<input type="text" class="input" id="cfg-${c.key}" value="${escapeHtml(val)}" ${disabled} oninput="onConfigChange('${c.key}')" style="max-width:480px">${unitSuffix}`;
		}
	}

	return control;
}

/* ============ 敏感项启用编辑 ============ */
function enableSecretEdit(key) {
	const wrap = $(`#cfg-secret-wrap-${key}`);
	if (!wrap) return;
	const origVal = _originalValues[key] || '';
	wrap.innerHTML = `
		<input type="text" class="input" id="cfg-${key}" value="${escapeHtml(origVal)}" oninput="onConfigChange('${key}')" style="max-width:480px" placeholder="输入新值">`;
	// 聚焦并选中
	const input = $(`#cfg-${key}`);
	input.focus();
	input.select();
	// 标记为已修改
	onConfigChange(key);
}

/* ============ 监听配置项变化 ============ */
function onConfigChange(key) {
	const input = $(`#cfg-${key}`);
	if (!input) return;
	const saveBtn = document.querySelector(`#cfg-row-${key} .btn-save`);
	const resetBtn = document.querySelector(`#cfg-row-${key} .btn-reset`);
	if (!saveBtn) return;

	// 对比当前值与原始值，决定是否启用保存按钮和显示重置按钮
	const currentVal = getControlValue(key);
	const origVal = _originalValues[key] || '';
	const changed = currentVal !== origVal;
	saveBtn.disabled = !changed;

	// 重置按钮：仅当值被修改时显示
	if (resetBtn) {
		resetBtn.classList.toggle('hidden', !changed);
	}
}

/* ============ 自定义多选组件（紧凑显示 + 搜索过滤 + 复选框）============
 * 通过 CONFIG_METADATA[key].multiSelect 定制空态文案与搜索占位符，
 * 未配置时回退到通用文案，避免不同业务场景复用硬编码文案。
 */
function renderMultiSelect(c) {
	const val = c.value || '';
	const disabled = c.is_readonly ? 'disabled' : '';
	const selectedVals = val ? val.split(',').map(v => v.trim()).filter(Boolean) : [];
	const totalOptions = c.options.length;
	// 从 CONFIG_METADATA 读取定制文案，未配置时回退到通用文案
	const meta = (CONFIG_METADATA[c.key] || {}).multiSelect || {};
	const emptyText = meta.emptyText || '未选择任何项';
	const searchPlaceholder = meta.searchPlaceholder || '搜索...';
	// showSearch: false 时隐藏搜索框（如选项数固定且较少的多选场景）
	const showSearch = meta.showSearch !== false;
	// 紧凑显示：选中数量或提示文案
	const displayText = selectedVals.length === 0
		? emptyText
		: `已选 ${selectedVals.length} 项 / ${totalOptions}`;

	return `
		<div class="multi-select-wrap" id="ms-wrap-${c.key}">
			<input type="hidden" id="cfg-${c.key}" value="${escapeHtml(val)}" />
			<div class="multi-select-display" id="ms-display-${c.key}" ${disabled}
				 onclick="toggleMultiSelect('${c.key}')">
				<span class="multi-select-text">${escapeHtml(displayText)}</span>
				<span class="multi-select-arrow">▾</span>
			</div>
			<div class="multi-select-dropdown" id="ms-dropdown-${c.key}" style="display:none">
				${showSearch ? `
				<div class="multi-select-search">
					<input type="text" class="input" placeholder="${escapeHtml(searchPlaceholder)}"
						   id="ms-search-${c.key}" oninput="filterMultiSelect('${c.key}')">
				</div>` : ''}
				<div class="multi-select-actions">
					<button type="button" onclick="selectAllMulti('${c.key}', true)">全选</button>
					<button type="button" onclick="selectAllMulti('${c.key}', false)">清空</button>
				</div>
				<div class="multi-select-list" id="ms-list-${c.key}">
					${c.options.map(o => {
						const checked = selectedVals.includes(o.value) ? 'checked' : '';
						return `<label class="ms-item"><input type="checkbox" value="${escapeHtml(o.value)}" ${checked} onchange="onMultiSelectChange('${c.key}')"><span>${escapeHtml(o.label)}</span></label>`;
					}).join('')}
				</div>
			</div>
		</div>`;
}

function toggleMultiSelect(key) {
	const dropdown = $(`#ms-dropdown-${key}`);
	if (!dropdown) return;
	if (dropdown.style.display === 'none') {
		dropdown.style.display = 'block';
		// 聚焦搜索框（无搜索框的配置项跳过）
		setTimeout(() => {
			const search = $(`#ms-search-${key}`);
			if (search) search.focus();
		}, 0);
	} else {
		dropdown.style.display = 'none';
	}
}

function filterMultiSelect(key) {
	const keyword = $(`#ms-search-${key}`).value.toLowerCase();
	const items = document.querySelectorAll(`#ms-list-${key} .ms-item`);
	items.forEach(item => {
		const text = item.textContent.toLowerCase();
		item.style.display = text.includes(keyword) ? '' : 'none';
	});
}

function selectAllMulti(key, selectAll) {
	const checkboxes = document.querySelectorAll(`#ms-list-${key} input[type="checkbox"]`);
	checkboxes.forEach(cb => {
		// 过滤掉搜索隐藏的项
		const item = cb.closest('.ms-item');
		if (item.style.display !== 'none') {
			cb.checked = selectAll;
		}
	});
	onMultiSelectChange(key);
}

function onMultiSelectChange(key) {
	// 收集选中的值
	const checkboxes = document.querySelectorAll(`#ms-list-${key} input[type="checkbox"]`);
	const selected = Array.from(checkboxes).filter(cb => cb.checked).map(cb => cb.value);
	// 更新 hidden input 的值
	const hidden = $(`#cfg-${key}`);
	if (hidden) hidden.value = selected.join(',');
	// 更新显示文本：从 CONFIG_METADATA 读取定制空态文案，保持与 renderMultiSelect 一致
	const displayText = $(`#ms-display-${key} .multi-select-text`);
	if (displayText) {
		const total = checkboxes.length;
		const meta = (CONFIG_METADATA[key] || {}).multiSelect || {};
		const emptyText = meta.emptyText || '未选择任何项';
		displayText.textContent = selected.length === 0
			? emptyText
			: `已选 ${selected.length} 项 / ${total}`;
	}
	onConfigChange(key);
}

// 点击外部关闭多选下拉框
document.addEventListener('click', (e) => {
	const wrap = e.target.closest('.multi-select-wrap');
	if (wrap) return;
	document.querySelectorAll('.multi-select-dropdown').forEach(dd => {
		dd.style.display = 'none';
	});
});

/* ============ 获取控件当前值 ============ */
function getControlValue(key) {
	const input = $(`#cfg-${key}`);
	if (!input) return '';
	if (input.type === 'checkbox') {
		return input.checked ? 'true' : 'false';
	}
	return input.value;
}

/* ============ 提交变更工单（替代原 saveConfig 直改）============
 * 配置修改不再直接落库，而是创建一份 ConfigChangeTicket 等待审批：
 * - 普通项：审核通过后生效
 * - 高风险项：审核 + 超管复核通过后生效
 * 提交时需填写变更原因，便于审批人判断是否通过。
 */
async function submitTicket(key) {
	if (_saving) return;
	const config = findConfig(key);
	if (!config) {
		toast('配置项不存在', 'error');
		return;
	}

	// 获取当前控件值，敏感项未启用编辑时跳过（值为 *** 不允许提交）
	const currentValue = getControlValue(key);
	if (config.is_secret && currentValue === '***') {
		toast('敏感项请先点击"修改"输入新值', 'error');
		return;
	}

	// 弹出确认框填写变更原因（必填），二次确认避免误提交
	showConfirmDialog({
		title: '提交配置变更工单',
		bannerType: 'info',
		bannerIcon: '📝',
		bannerText: `配置项：${config.label || key}（${key}）${config.risk_level === 'high' ? '，⚠️ 高风险项需复核' : ''}`,
		bodyHtml: `<div class="form-item" style="margin-top:12px">
			<label class="form-label">变更原因 <span class="required">*</span></label>
			<textarea id="ticketReasonInput" class="input" rows="3" placeholder="请说明本次配置变更的原因，便于审批人判断" style="max-width:100%"></textarea>
		</div>`,
		buttons: [
			{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
			{
				text: '提交工单',
				type: 'primary',
				onClick: async (ctx) => {
					const reason = $('#ticketReasonInput').value.trim();
					if (!reason) {
						ctx.setError('请填写变更原因');
						return;
					}
					try {
						_saving = true;
						// 提交工单：POST /api/v1/system/tickets/
						const ticket = await api.postJson('/api/v1/system/tickets/', {
							ticket_type: 'config',
							config_key: key,
							new_value: currentValue,
							reason: reason,
						});
						ctx.close();
						// 提交成功后恢复控件为原值（工单未通过前配置未变）
						resetConfig(key);
						toast(`工单已提交（#${ticket.id}），等待审批`, 'success');
					} catch (e) {
						ctx.setError(`提交失败：${e.message}`);
					} finally {
						_saving = false;
					}
				}
			}
		],
		onShow: (ctx) => {
			// 自动聚焦原因输入框
			const input = ctx.el.querySelector('#ticketReasonInput');
			if (input) input.focus();
		}
	});
}

/* ============ 重置配置（恢复当前值）============ */
function resetConfig(key) {
	const config = findConfig(key);
	if (!config) return;
	const controlEl = $(`#cfg-control-${key}`);
	if (!controlEl) return;
	// 重新渲染控件（恢复原始值）
	const meta = CONFIG_METADATA[key] || {};
	controlEl.innerHTML = renderControl(config, meta.unit || config.unit);
	// 禁用保存按钮并隐藏重置按钮
	const saveBtn = document.querySelector(`#cfg-row-${key} .btn-save`);
	const resetBtn = document.querySelector(`#cfg-row-${key} .btn-reset`);
	if (saveBtn) saveBtn.disabled = true;
	if (resetBtn) resetBtn.classList.add('hidden');
}

/* ============ 查找配置对象 ============ */
function findConfig(key) {
	for (const cat of Object.keys(_allConfigs)) {
		const found = _allConfigs[cat].find(c => c.key === key);
		if (found) return found;
	}
	return null;
}

/* ==========================================================
   模型管理弹窗
   - openModelModal()    打开弹窗并加载模型列表
   - loadModels()        拉取后端按 model_type 分组的模型数据
   - switchModelTab(t)   切换 LLM / Embedding / Rerank tab
   - showModelForm(m)    显示新增/编辑表单（m=null 为新增）
   - saveModel()         提交新增/编辑
   - deleteModel(id)     删除指定模型
   - closeModelModal()   关闭模型管理弹窗
   ========================================================== */

// 模型类型中文名映射，用于 tab 切换与空态提示
const MODEL_TYPE_LABELS = {
	llm: 'LLM 对话模型',
	embedding: 'Embedding 向量模型',
	rerank: 'Rerank 重排序模型',
};

// 模型管理弹窗状态
let _modelGroups = {};          // 后端返回的按 model_type 分组的模型列表
let _currentModelType = 'llm';  // 当前选中的 tab 类型
let _modelSaving = false;       // 防止重复提交

/* ============ 打开模型管理弹窗 ============ */
async function openModelModal() {
	showModal('modelManageModal');
	// 默认选中 LLM tab
	switchModelTab('llm');
	await loadModels();
}

/* ============ 关闭模型管理弹窗 ============ */
function closeModelModal() {
	closeModal('modelManageModal');
	// 同时关闭可能残留的二级表单弹窗（showConfirmDialog），避免下次打开时表单还在
	const overlay = document.getElementById('confirmOverlay');
	if (overlay) {
		overlay.classList.remove('show');
		// 恢复最上层弹窗交互(showConfirmDialog 激活期间被禁用,此处直接操作 overlay 绕过 ctx.close,
		// 需手动恢复;closeModal 已同步弹窗栈,这里只需恢复栈顶,不能恢复全部,否则下层弹窗又会可穿透)
		_restoreTopModalInteraction();
	}
}

/* ============ 加载模型列表 ============ */
async function loadModels() {
	const tbody = $('#modelTableBody');
	if (!tbody) return;
	tbody.innerHTML = '<tr><td colspan="7" class="model-empty">加载中...</td></tr>';
	try {
		const data = await api.getJson('/api/v1/system/llm-models/');
		_modelGroups = data.groups || {};
		renderModelTable();
	} catch (e) {
		tbody.innerHTML = `<tr><td colspan="7" class="model-empty">加载失败：${escapeHtml(e.message)}</td></tr>`;
	}
}

/* ============ 切换类型 tab ============ */
function switchModelTab(type) {
	_currentModelType = type;
	// 更新 tab 激活态
	$$('.model-tab').forEach(btn => {
		btn.classList.toggle('active', btn.dataset.type === type);
	});
	// 仅在已加载数据后渲染，未加载时 loadModels 会触发渲染
	if (Object.keys(_modelGroups).length > 0) {
		renderModelTable();
	}
}

/* ============ 渲染模型表格 ============ */
function renderModelTable() {
	const tbody = $('#modelTableBody');
	if (!tbody) return;
	const list = _modelGroups[_currentModelType] || [];
	if (list.length === 0) {
		const label = MODEL_TYPE_LABELS[_currentModelType] || '';
		tbody.innerHTML = `<tr><td colspan="7" class="model-empty">暂无${escapeHtml(label)}，点击左下角"新增模型"添加</td></tr>`;
		return;
	}
	tbody.innerHTML = list.map(m => {
		// 状态徽标：启用绿色 / 停用红色
		const status = m.is_active
			? '<span class="model-status on">● 启用</span>'
			: '<span class="model-status off">○ 停用</span>';
		// 依赖标记：被引用时显示橙色标记
		const depBadge = m.dependency_count > 0
			? `<span class="model-dependency-badge" title="被 ${m.dependency_count} 个配置项引用">🔗 依赖</span>`
			: '';
		// 待审批工单标记：有待审批工单时显示
		const ticketBadge = m.pending_ticket_count > 0
			? `<span class="model-ticket-badge" title="有 ${m.pending_ticket_count} 个待审批工单">⏳ 审批中</span>`
			: '';
		// name/provider/model_name/base_url 可能含特殊字符，统一转义防 XSS
		return `<tr>
			<td title="${escapeHtml(m.name)}">${escapeHtml(m.name)} ${depBadge}${ticketBadge}</td>
			<td title="${escapeHtml(m.provider)}">${escapeHtml(m.provider)}</td>
			<td title="${escapeHtml(m.base_url)}">${escapeHtml(m.base_url || '-')}</td>
			<td title="${escapeHtml(m.model_name)}">${escapeHtml(m.model_name)}</td>
		<td>${m.timeout != null ? m.timeout + '秒' : '-'}</td>
		<td>${status}</td>
			<td>
				<div class="model-row-actions">
					<button class="btn btn-sm btn-outline" onclick="showModelForm(${m.id})">编辑</button>
					<button class="btn btn-sm btn-danger" onclick="deleteModel(${m.id})">删除</button>
				</div>
			</td>
		</tr>`;
	}).join('');
}

/* ============ 根据 id 查找模型对象 ============ */
function findModel(id) {
	for (const type of Object.keys(_modelGroups)) {
		const found = _modelGroups[type].find(m => m.id === id);
		if (found) return found;
	}
	return null;
}

/* ============ 显示新增/编辑表单（使用 common.js showConfirmDialog 二级弹窗）============ */
function showModelForm(id) {
	const isEdit = id != null && id !== '';
	const m = isEdit ? findModel(id) : null;
	// 编辑时若找不到对象（已被删除等），直接忽略
	if (isEdit && !m) {
		toast('模型不存在，可能已被删除', 'error');
		return;
	}

	// 表单字段 HTML（ID 保持不变，saveModel 通过 document.getElementById 读取）
	// 编辑时区分：修改显示名直接生效，修改其他字段需走审批
	const isNameOnly = isEdit && !m.provider && !m.base_url && !m.model_name && !m.timeout && m.is_active;
	const formHtml = `
		<input type="hidden" id="modelFormFieldId" value="${isEdit ? m.id : ''}">
		<div class="form-item">
			<label class="form-label">显示名称 <span class="required">*</span></label>
			<input id="modelFormFieldName" class="input" placeholder="如：DeepSeek 对话" value="${isEdit ? escapeHtml(m.name) : ''}">
			<div class="form-hint">修改显示名无需审批，立即生效</div>
		</div>
		<div class="form-item">
			<label class="form-label">提供商 <span class="required">*</span></label>
			<input id="modelFormFieldProvider" class="input" placeholder="如：deepseek、openai" value="${isEdit ? escapeHtml(m.provider) : ''}">
		</div>
		<div class="form-item">
			<label class="form-label">模型类型 <span class="required">*</span></label>
			<select id="modelFormFieldType" class="input">
				<option value="llm" ${isEdit && m.model_type === 'llm' ? 'selected' : ''}>LLM 对话模型</option>
				<option value="embedding" ${isEdit && m.model_type === 'embedding' ? 'selected' : ''}>Embedding 向量模型</option>
				<option value="rerank" ${isEdit && m.model_type === 'rerank' ? 'selected' : ''}>Rerank 重排序模型</option>
			</select>
		</div>
		<div class="form-item">
			<label class="form-label">API 地址</label>
			<input id="modelFormFieldBaseUrl" class="input" placeholder="https://api.deepseek.com" value="${isEdit ? escapeHtml(m.base_url || '') : ''}">
		</div>
		<div class="form-item">
			<label class="form-label">模型标识 <span class="required">*</span></label>
			<input id="modelFormFieldModelName" class="input" placeholder="如：deepseek-chat" value="${isEdit ? escapeHtml(m.model_name) : ''}">
		</div>
		<div class="form-item">
			<label class="form-label">超时秒数</label>
			<input id="modelFormFieldTimeout" type="number" min="1" step="1" class="input" placeholder="为空时使用全局 LLM_TIMEOUT" value="${isEdit && m.timeout != null ? m.timeout : ''}">
		</div>
		<div class="form-item">
			<label class="form-label">启用</label>
			<label class="switch">
				<input type="checkbox" id="modelFormFieldActive" ${(!isEdit || m.is_active) ? 'checked' : ''}>
				<span class="slider"></span>
			</label>
		</div>
		${isEdit ? `
		<div class="form-item">
			<label class="form-label">变更原因</label>
			<textarea id="modelFormFieldReason" class="textarea" placeholder="修改其他字段需提交审批，请说明变更原因"></textarea>
			<div class="form-hint">修改显示名外的字段需走审批流程</div>
		</div>` : ''}`;

	showConfirmDialog({
		title: isEdit ? '编辑模型' : '新增模型',
		bodyHtml: formHtml,
		buttons: [
			{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
			{ text: '保存', type: 'primary', onClick: (ctx) => saveModel(ctx) },
		],
		onShow: (ctx) => {
			// 聚焦第一个输入框
			const nameInput = ctx.el.querySelector('#modelFormFieldName');
			if (nameInput) nameInput.focus();
		},
	});
}

/* ============ 保存模型（新增/编辑）============ */
async function saveModel(dialogCtx) {
	if (_modelSaving) return;
	// 从 confirmDialog 内读取表单字段（document.getElementById 可穿透 overlay）
	const f = {
		name:     $('#modelFormFieldName'),
		provider: $('#modelFormFieldProvider'),
		type:     $('#modelFormFieldType'),
		base_url: $('#modelFormFieldBaseUrl'),
		model:    $('#modelFormFieldModelName'),
		timeout:  $('#modelFormFieldTimeout'),
		active:   $('#modelFormFieldActive'),
		id:       $('#modelFormFieldId'),
		reason:   $('#modelFormFieldReason'),
	};
	const missing = Object.entries(f).find(([, el]) => !el);
	if (missing) {
		toast(`表单元素缺失: ${missing[0]}，请刷新页面重试`, 'error');
		return;
	}
	const name = f.name.value.trim();
	const provider = f.provider.value.trim();
	const model_type = f.type.value;
	const model_name = f.model.value.trim();
	if (!name) { toast('请填写显示名称', 'error'); return; }
	if (!provider) { toast('请填写提供商', 'error'); return; }
	if (!model_name) { toast('请填写模型标识', 'error'); return; }

	const payload = {
		name,
		provider,
		model_type,
		base_url: f.base_url.value.trim(),
		model_name,
		// 超时为空时传 null，后端存 None，业务读取时回退到全局 LLM_TIMEOUT
		timeout: f.timeout.value.trim() || null,
		is_active: f.active.checked,
		reason: f.reason ? f.reason.value.trim() : '',
	};
	const id = f.id.value;
	_modelSaving = true;
	try {
		if (id) {
			const resp = await api.patchJson(`/api/v1/system/llm-models/${id}/`, payload);
			// 后端返回 202 表示已提交审批，200 表示直接生效
			if (resp && resp.ticket_id) {
				toast(`已提交审批，工单 ID: ${resp.ticket_id}`, 'warning');
			} else {
				toast('模型已更新', 'success');
			}
		} else {
			await api.postJson('/api/v1/system/llm-models/', payload);
			toast('模型已新增', 'success');
		}
		// 关闭二级弹窗
		if (dialogCtx) dialogCtx.close();
		// 刷新列表
		await loadModels();
		switchModelTab(payload.model_type);
	} catch (e) {
		toast(`保存失败：${e.message}`, 'error');
	} finally {
		_modelSaving = false;
	}
}

/* ============ 删除模型（超管复核 + 检查依赖）============ */
function deleteModel(id) {
	const m = findModel(id);
	if (!m) {
		toast('模型不存在，可能已被删除', 'error');
		return;
	}
	// 删除需填写变更原因，并提交审批（超管复核）
	const formHtml = `
		<div style="margin-bottom:8px;color:var(--text-sub);font-size:13px;">
			此操作需复核，审批通过后才能删除。
		</div>
		<div class="form-item">
			<label class="form-label">删除原因 <span class="required">*</span></label>
			<textarea id="deleteModelReason" class="textarea" placeholder="请说明删除原因，便于审批" required></textarea>
		</div>`;

	showConfirmDialog({
		title: '删除模型',
		bannerText: `确认删除模型「${m.name}」吗？此操作需复核。`,
		bannerType: 'danger',
		bodyHtml: formHtml,
		buttons: [
			{ text: '取消', type: 'cancel', onClick: (ctx) => ctx.close() },
			{
				text: '提交审批',
				type: 'danger',
				onClick: async (ctx) => {
					const reason = ctx.el.querySelector('#deleteModelReason').value.trim();
					if (!reason) {
						ctx.setError('请填写删除原因');
						return;
					}
					try {
						// DELETE 带 body：api.fetchWithAuth 带自动 token
						const res = await api.fetchWithAuth('DELETE', `/api/v1/system/llm-models/${id}/`, {
							body: JSON.stringify({ reason }),
						});
						if (!res.ok) {
							const data = await res.json().catch(() => ({}));
							throw new Error(data.detail || '提交失败');
						}
						ctx.close();
						toast('删除申请已提交，等待复核', 'warning');
					} catch (e) {
						ctx.setError(`提交失败：${e.message}`);
					}
				}
			}
		]
	});
}

/* ==========================================================
   配置变更记录（已生效变更历史）
   - openHistoryModal()   打开变更记录弹窗并加载
   - closeHistoryModal() 关闭弹窗
   - loadHistoryRecords() 拉取已通过工单（status=approved）作为变更历史
   - renderHistoryList() 渲染变更记录列表
   ========================================================== */

// 变更记录弹窗状态
let _historyRecords = [];  // 全部变更记录
let _historyPage = 1;      // 当前页码

/* ============ 打开变更记录弹窗 ============ */
async function openHistoryModal() {
	showModal('historyModal');
	// 清空搜索框，避免上次筛选残留
	const searchInput = $('#historySearchInput');
	if (searchInput) searchInput.value = '';
	_historyPage = 1; // 重置页码
	Pagination.destroy(); // 销毁旧分页实例，避免残留状态
	await loadHistoryRecords();
}

/* ============ 关闭变更记录弹窗 ============ */
function closeHistoryModal() {
	closeModal('historyModal');
}

/* ============ 加载变更记录（已通过工单）============ */
async function loadHistoryRecords() {
	const body = $('#historyListBody');
	if (!body) return;
	body.innerHTML = '<div class="ticket-empty">加载中...</div>';
	try {
		// 只加载已通过的工单，展示实际生效的变更
		const data = await api.getJson('/api/v1/system/tickets/?status=approved&ticket_type=config');
		_historyRecords = data.tickets || [];
		renderHistoryList();
	} catch (e) {
		body.innerHTML = `<div class="ticket-empty">加载失败：${escapeHtml(e.message)}</div>`;
	}
}

/* ============ 渲染变更记录列表 ============ */
const _HISTORY_PAGE_SIZE = 10; // 每页展示 10 条记录

function renderHistoryList() {
	const body = $('#historyListBody');
	if (!body) return;
	// 读取搜索关键词，同时匹配申请人、中文名、字段名
	const keyword = ($('#historySearchInput') || {}).value?.trim().toLowerCase() || '';
	// 按生效时间倒序排列，最近的变更在最前
	let records = [..._historyRecords].sort((a, b) => {
		const ta = a.applied_at ? new Date(a.applied_at).getTime() : 0;
		const tb = b.applied_at ? new Date(b.applied_at).getTime() : 0;
		return tb - ta;
	});
	// 关键词过滤：匹配 申请人(creator) / 中文名(config_label) / 字段名(config_key)
	if (keyword) {
		records = records.filter(r => {
			const creator = (r.creator || '').toLowerCase();
			const label = (r.config_label || '').toLowerCase();
			const key = (r.config_key || '').toLowerCase();
			return creator.includes(keyword) || label.includes(keyword) || key.includes(keyword);
		});
	}
	if (records.length === 0) {
		body.innerHTML = keyword
			? `<div class="ticket-empty">未找到匹配"${escapeHtml(keyword)}"的记录</div>`
			: '<div class="ticket-empty">暂无已生效的配置变更</div>';
		Pagination.destroy();
		return;
	}

	// 前端分页：全量数据按 _HISTORY_PAGE_SIZE 切片展示当前页
	const totalPages = Math.ceil(records.length / _HISTORY_PAGE_SIZE);
	if (_historyPage > totalPages) _historyPage = 1;
	const start = (_historyPage - 1) * _HISTORY_PAGE_SIZE;
	const pageItems = records.slice(start, start + _HISTORY_PAGE_SIZE);

	body.innerHTML = pageItems.map(renderHistoryCard).join('');

	// 使用公共 Pagination 组件渲染分页控件；首次 render 建 DOM，后续 update 仅刷新状态
	const pgnState = { page: _historyPage, totalPages, total: records.length, pageSize: _HISTORY_PAGE_SIZE };
	if (_historyPage > 1) {
		Pagination.update(pgnState);
	} else {
		Pagination.render({
			container: '#historyPagination',
			...pgnState,
			align: 'center',
			onPageChange: (p) => {
				_historyPage = p;
				renderHistoryList();
				const scrollBody = $('#historyListBody');
				if (scrollBody) scrollBody.scrollTop = 0;
			}
		});
	}
}

/* ============ 搜索过滤变更记录（oninput 触发）============ */
function filterHistoryRecords() {
	_historyPage = 1; // 搜索时重置页码
	renderHistoryList();
}

/* ============ 渲染单条变更记录卡片 ============ */
function renderHistoryCard(r) {
	const riskBadge = r.risk_level === 'high'
		? '<span class="ticket-badge ticket-badge-risk">⚠️ 高风险</span>'
		: '';
	const appliedTime = r.applied_at ? formatDate(r.applied_at) : '-';
	// 审批人信息：审核人 + 复核人（如果有）
	const approverInfo = [
		r.auditor ? `审核：${escapeHtml(r.auditor)}` : '',
		r.reviewer ? `复核：${escapeHtml(r.reviewer)}` : '',
	].filter(Boolean).join(' / ') || '-';

	return `<div class="history-card">
		<div class="history-card-header">
			<div class="history-card-title">
				<span class="ticket-config-label">${escapeHtml(r.config_label || r.config_key)}</span>
				<span class="ticket-config-key">${escapeHtml(r.config_key)}</span>
				${riskBadge}
			</div>
			<div class="history-card-time">生效时间：${appliedTime}</div>
		</div>
		<div class="history-card-body">
			<div class="history-card-diff">
				<span class="history-old">${escapeHtml(r.old_value || '空')}</span>
				<span class="history-arrow">→</span>
				<span class="history-new">${escapeHtml(r.new_value)}</span>
			</div>
			<div class="history-card-meta">
				<span>提交人：${escapeHtml(r.creator || '-')}</span>
				<span>${approverInfo}</span>
				<span>工单 #${r.id}</span>
			</div>
			${r.reason ? `<div class="history-card-reason">变更原因：${formatMultiline(r.reason)}</div>` : ''}
		</div>
	</div>`;
}
