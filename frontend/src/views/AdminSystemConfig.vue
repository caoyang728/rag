<template>
  <div class="page-container admin-system-config-page">
    <!-- 无权限：仅超级管理员 / 维护管理员可访问（与旧 tmpl-no-permission 对应） -->
    <PageGuard :allowed="userStore.isSystemMaintainer" message="仅超级管理员或维护管理员可访问此页面">
      <!-- ===== 页头 ===== -->
      <div class="page-header">
        <div>
          <div class="page-title">系统配置</div>
          <div class="page-desc">运行期参数管理（修改需提交工单，审批通过后生效）</div>
        </div>
        <div class="page-header-actions">
          <el-button size="small" @click="router.push('/ticket')">📋 工单列表</el-button>
          <el-button size="small" type="primary" @click="openModelModal">🧠 模型管理</el-button>
        </div>
      </div>

      <!-- ===== 内容区：左分类导航 + 右配置列表（撑满剩余高度） ===== -->
      <div class="page-body">
      <div class="config-main">
        <!-- ===== 分类导航 ===== -->
        <div class="app-card config-nav-card">
          <div class="card-title">配置分类</div>
          <div class="config-nav">
            <div
              v-for="cat in categoryList"
              :key="cat.key"
              class="config-nav-item"
              :class="{ active: cat.key === currentCategory }"
              @click="selectCategory(cat.key)"
            >
              <span class="config-nav-icon">{{ cat.icon }}</span>
              <span class="config-nav-label">{{ cat.label }}</span>
              <span class="config-nav-count">{{ cat.count }}</span>
            </div>
            <el-empty v-if="!categoryList.length" description="暂无分类" :image-size="50" />
          </div>
        </div>

        <!-- ===== 配置项列表 ===== -->
        <div class="app-card config-list-card">
          <div class="config-list-head">
            <span class="card-title">
              {{ currentCategoryLabel }}
              <span class="text-sub text-sm">（{{ currentConfigs.length }} 项）</span>
            </span>
            <el-button size="small" @click="openHistoryModal">📜 变更记录</el-button>
          </div>
          <div v-loading="configLoading" class="config-list">
            <el-empty v-if="!configLoading && !currentConfigs.length" description="该分类下暂无配置项" :image-size="60" />
            <!-- 每个配置项：两行结构，左侧 220px（中文名/字段名）+ 右侧（控件/解释）对齐 -->
            <div v-for="c in currentConfigs" :key="c.key" class="config-item" :class="{ readonly: c.is_readonly }">
              <div class="config-item-row">
                <div class="config-item-label">
                  {{ c.label }}
                  <el-tag v-if="c.is_readonly" size="small" type="warning" effect="plain" title="修改需重建索引或影响路由，仅限 .env 修改">🔒 只读</el-tag>
                  <el-tag v-if="c.is_secret" size="small" type="danger" effect="plain" title="敏感项，值已掩码">🔐 敏感</el-tag>
                  <el-tag v-if="c.risk_level === 'high'" size="small" type="warning" effect="plain" title="高风险项，工单需复核">⚠️ 高风险</el-tag>
                </div>
                <div class="config-item-control">
                  <!-- bool：开关 -->
                  <el-switch v-if="c.value_type === 'bool'" v-model="c.draft" :disabled="c.is_readonly" @change="onConfigChange(c)" />
                  <!-- int：非负整数，限制键入小数或负数 -->
                  <el-input-number v-else-if="c.value_type === 'int'" v-model="c.draft" :min="0" :step="1" :disabled="c.is_readonly" @change="onConfigChange(c)" class="num-input" />
                  <!-- float -->
                  <el-input-number v-else-if="c.value_type === 'float'" v-model="c.draft" :step="0.01" :disabled="c.is_readonly" @change="onConfigChange(c)" class="num-input" />
                  <!-- json：多行文本 -->
                  <el-input v-else-if="c.value_type === 'json'" v-model="c.draft" type="textarea" :rows="3" :disabled="c.is_readonly" @input="onConfigChange(c)" class="json-input" />
                  <!-- 多选：description 含"多选"时渲染自定义多选组件（el-select multiple + 全选/清空） -->
                  <el-select
                    v-else-if="c.isMulti"
                    v-model="c.draft"
                    multiple
                    :filterable="multiShowSearch(c)"
                    collapse-tags
                    collapse-tags-tooltip
                    :placeholder="multiEmptyText(c)"
                    :disabled="c.is_readonly"
                    class="multi-select"
                    @change="onConfigChange(c)"
                  >
                    <template #header>
                      <div class="ms-actions">
                        <el-button link type="primary" size="small" @click="selectAllMulti(c, true)">全选</el-button>
                        <el-button link size="small" @click="selectAllMulti(c, false)">清空</el-button>
                      </div>
                    </template>
                    <el-option v-for="o in c.options" :key="o.value" :label="o.label" :value="o.value" />
                  </el-select>
                  <!-- 单选：有可选值列表 -->
                  <el-select v-else-if="c.options && c.options.length" v-model="c.draft" :disabled="c.is_readonly" @change="onConfigChange(c)" class="single-select">
                    <el-option v-for="o in c.options" :key="o.value" :label="o.label" :value="o.value" />
                  </el-select>
                  <!-- 普通 string -->
                  <el-input v-else-if="!c.is_secret" v-model="c.draft" :disabled="c.is_readonly" @input="onConfigChange(c)" class="text-input" />
                  <!-- 敏感项：显示掩码，需点击"修改"才能输入 -->
                  <div v-else class="secret-wrap">
                    <el-button v-if="!c.secretEditing" link type="primary" @click="enableSecretEdit(c)">✏️ 点击修改</el-button>
                    <el-input v-else v-model="c.draft" placeholder="输入新值" @input="onConfigChange(c)" class="text-input" />
                  </div>
                  <span v-if="c.unit" class="config-unit">{{ c.unit }}</span>
                </div>
                <div class="config-item-actions" v-if="!c.is_readonly">
                  <!-- 重置按钮：仅在修改值后显示 -->
                  <el-button v-if="c.changed" size="small" circle title="恢复当前值" @click="resetConfig(c)">↺</el-button>
                  <el-button type="primary" size="small" :disabled="!c.changed" @click="submitTicket(c)">提交工单</el-button>
                </div>
              </div>
              <div class="config-item-row config-item-meta">
                <div class="config-item-meta-left"><span class="config-item-key-text">{{ c.key }}</span></div>
                <span class="config-item-hint">{{ c.description }}</span>
                <span v-if="c.updated_at" class="config-item-updated">最近更新：{{ formatDate(c.updated_at) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>
      </div>
    </PageGuard>

    <!-- ===== 模型管理弹窗（复用公共 BaseDialog：固定高度 + 表格区域内部滚动） ===== -->
    <BaseDialog v-model="modelDialogVisible" title="模型管理" width="1000px" min-width="1000px" height="70vh" :close-on-click-modal="false">
      <div class="model-dialog-body">
        <!-- 类型切换 tab：LLM / Embedding / Rerank -->
        <el-tabs v-model="currentModelType" class="model-tabs">
          <el-tab-pane label="🤖 LLM 对话" name="llm" />
          <el-tab-pane label="🧮 Embedding" name="embedding" />
          <el-tab-pane label="🎯 Rerank" name="rerank" />
        </el-tabs>
        <div class="model-table-wrap">
          <el-table :data="modelList" v-loading="modelLoading" class="model-table">
            <el-table-column label="名称" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">
                {{ row.name }}
                <el-tag v-if="row.dependency_count > 0" type="warning" size="small" effect="plain" :title="`被 ${row.dependency_count} 个配置项引用`">🔗 依赖</el-tag>
                <el-tag v-if="row.pending_ticket_count > 0" type="primary" size="small" effect="plain" :title="`有 ${row.pending_ticket_count} 个待审批工单`">⏳ 审批中</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="提供商" min-width="100" show-overflow-tooltip>
              <template #default="{ row }">{{ row.provider }}</template>
            </el-table-column>
            <el-table-column label="API 地址" min-width="180" show-overflow-tooltip>
              <template #default="{ row }">{{ row.base_url || '-' }}</template>
            </el-table-column>
            <el-table-column label="模型标识" min-width="130" show-overflow-tooltip>
              <template #default="{ row }">{{ row.model_name }}</template>
            </el-table-column>
            <el-table-column label="超时" width="80">
              <template #default="{ row }">{{ row.timeout != null ? row.timeout + '秒' : '-' }}</template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.is_active" type="success" size="small" effect="plain">● 启用</el-tag>
                <el-tag v-else type="danger" size="small" effect="plain">○ 停用</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="160">
              <template #default="{ row }">
                <el-button size="small" @click="showModelForm(row.id)">编辑</el-button>
                <el-button size="small" type="danger" @click="deleteModel(row.id)">删除</el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty :description='`暂无${MODEL_TYPE_LABELS[currentModelType] || ""}，点击左下角"新增模型"添加`' :image-size="60" />
            </template>
          </el-table>
        </div>
        <!-- 提示：API Key 不在此管理 -->
        <div class="model-tip">提示：API Key 仍保留在 .env 中，此处仅管理模型调用所需的地址与标识。</div>
      </div>
      <template #footer>
        <div class="model-footer">
          <el-button type="primary" size="small" @click="showModelForm(null)">＋ 新增模型</el-button>
          <el-button size="small" @click="modelDialogVisible = false">关闭</el-button>
        </div>
      </template>
    </BaseDialog>

    <!-- ===== 模型新增/编辑弹窗（复用公共 BaseDialog：高度随内容自适应） ===== -->
    <BaseDialog v-model="modelFormVisible" :title="modelFormEditId ? '编辑模型' : '新增模型'" width="560px" min-width="560px" height="auto" min-height="0" :close-on-click-modal="false">
      <div class="form-item">
        <label class="form-label">显示名称 <span class="required">*</span></label>
        <el-input v-model="modelForm.name" placeholder="如：DeepSeek 对话" />
        <div class="form-hint">修改显示名无需审批，立即生效</div>
      </div>
      <div class="form-item">
        <label class="form-label">提供商 <span class="required">*</span></label>
        <el-input v-model="modelForm.provider" placeholder="如：deepseek、openai" />
      </div>
      <div class="form-item">
        <label class="form-label">模型类型 <span class="required">*</span></label>
        <el-select v-model="modelForm.model_type" style="width: 100%">
          <el-option label="LLM 对话模型" value="llm" />
          <el-option label="Embedding 向量模型" value="embedding" />
          <el-option label="Rerank 重排序模型" value="rerank" />
        </el-select>
      </div>
      <div class="form-item">
        <label class="form-label">API 地址</label>
        <el-input v-model="modelForm.base_url" placeholder="https://api.deepseek.com" />
      </div>
      <div class="form-item">
        <label class="form-label">模型标识 <span class="required">*</span></label>
        <el-input v-model="modelForm.model_name" placeholder="如：deepseek-chat" />
      </div>
      <div class="form-item">
        <label class="form-label">超时秒数</label>
        <el-input v-model="modelForm.timeout" type="number" min="1" step="1" placeholder="为空时使用全局 LLM_TIMEOUT" />
      </div>
      <div class="form-item">
        <label class="form-label">启用</label>
        <el-switch v-model="modelForm.is_active" />
      </div>
      <div v-if="modelFormEditId" class="form-item">
        <label class="form-label">变更原因</label>
        <el-input v-model="modelForm.reason" type="textarea" :rows="2" placeholder="修改其他字段需提交审批，请说明变更原因" />
        <div class="form-hint">修改显示名外的字段需走审批流程</div>
      </div>
      <template #footer>
        <el-button @click="modelFormVisible = false">取消</el-button>
        <el-button type="primary" :loading="modelSaving" @click="saveModel">保存</el-button>
      </template>
    </BaseDialog>

    <!-- ===== 配置变更记录弹窗 =====
         展示所有已生效的配置变更历史（从已通过工单汇总），
         包含配置项、变更前后的值、操作人、生效时间。 -->
    <BaseDialog v-model="historyVisible" title="配置变更记录" width="900px" min-width="900px" height="auto" min-height="0" :close-on-click-modal="false">
      <div class="history-search-bar">
        <el-input v-model="historyKeyword" placeholder="搜索申请人或配置项（中文名/字段名）" clearable />
      </div>
      <div class="history-list-body">
        <el-empty v-if="!historyFiltered.length" :description='historyKeyword.trim() ? `未找到匹配"${historyKeyword.trim()}"的记录` : "暂无已生效的配置变更"' :image-size="60" />
        <div v-for="r in historyPageItems" :key="r.id" class="history-card">
          <div class="history-card-header">
            <div class="history-card-title">
              <span class="ticket-config-label">{{ r.config_label || r.config_key }}</span>
              <span class="ticket-config-key">{{ r.config_key }}</span>
              <el-tag v-if="r.risk_level === 'high'" type="danger" size="small" effect="plain">⚠️ 高风险</el-tag>
            </div>
            <div class="history-card-time">生效时间：{{ r.applied_at ? formatDate(r.applied_at) : '-' }}</div>
          </div>
          <div class="history-card-body">
            <div class="history-card-diff">
              <span class="history-old">{{ r.old_value || '空' }}</span>
              <span class="history-arrow">→</span>
              <span class="history-new">{{ r.new_value }}</span>
            </div>
            <div class="history-card-meta">
              <span>提交人：{{ r.creator || '-' }}</span>
              <span>{{ approverInfo(r) }}</span>
              <span>工单 #{{ r.id }}</span>
            </div>
            <div v-if="r.reason" class="history-card-reason">变更原因：{{ formatMultiline(r.reason) }}</div>
          </div>
        </div>
      </div>
      <template #footer>
        <AppPagination
          class="history-pagination"
          :total="historyFiltered.length"
          :page-size="HISTORY_PAGE_SIZE"
          :page="historyPage"
          @page-change="onHistoryPageChange"
        />
      </template>
    </BaseDialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { formatDate, errMsg } from '../utils/format'
import BaseDialog from '../components/base/BaseDialog.vue'
import PageGuard from '../components/base/PageGuard.vue'
import AppPagination from '../components/base/AppPagination.vue'

const userStore = useUserStore()
const router = useRouter()

/* ============ 配置分类中文名 + 图标映射（与后端 SystemConfig.CATEGORY_CHOICES 对应） ============ */
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
}

// 分类显示顺序（非字母序，按业务逻辑排序）
const CATEGORY_ORDER = ['llm', 'embedding', 'retrieval', 'storage', 'email', 'agent', 'security', 'memory', 'analytics', 'eval', 'knowledge']

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
  FAST_MODE_STRATEGY: { label: '快速问答检索策略', description: '控制快速问答模式的检索策略：parallel 三路并行（Wiki+GraphRAG+RAG，延迟 4-6s）；rag_only 仅 RAG（延迟 3-5s，最省资源）；sequential 串行降级（命中即停，延迟波动大）', sortOrder: 10 },
  PERSONALIZED_RETRIEVAL_ENABLED: { label: '个性化检索开关', description: '开启后，基于用户历史问答/画像对检索结果做轻量加权排序（默认影响≤10%）；关闭时行为与现状完全一致，冷启动用户无副作用', sortOrder: 11 },
  PERSONALIZED_WEIGHT: { label: '个性化加权系数', description: '个性化排序加权系数（0-0.2，默认 0.1 即影响不超过 10%）。数值越大个性化对排序影响越明显，过高会导致画像污染全局检索结果', sortOrder: 12 },

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
  AGENT_DEFAULT_MODE: { label: '默认问答模式', description: '用户未指定模式时的默认问答模式：rag 快速问答（仅 RAG 检索，首字 3-5s）；agent 智能问答（ReAct 循环，默认）；plan 深度分析（Plan-and-Execute 三阶段编排）', sortOrder: 1 },
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
}

// 模型类型中文名映射，用于 tab 切换与空态提示
const MODEL_TYPE_LABELS = {
  llm: 'LLM 对话模型',
  embedding: 'Embedding 向量模型',
  rerank: 'Rerank 重排序模型',
}

/* ==========================================================
   配置列表状态
   ========================================================== */
const allConfigs = ref({})       // 全部配置，按 category 分组：{ llm: [...], embedding: [...] }
const currentCategory = ref('')  // 当前选中的 category
const configLoading = ref(false)
let originalValues = {}          // 各配置项的原始值（用于对比变化和重置）：{ KEY: 'value' }
let saving = false               // 防止重复提交

const categoryList = computed(() => {
  // 按 CATEGORY_ORDER 预定义顺序排序，而非字母序；未在其中的分类排到最后按字母序
  return Object.keys(allConfigs.value)
    .sort((a, b) => {
      const ia = CATEGORY_ORDER.indexOf(a)
      const ib = CATEGORY_ORDER.indexOf(b)
      if (ia === -1 && ib === -1) return a.localeCompare(b)
      if (ia === -1) return 1
      if (ib === -1) return -1
      return ia - ib
    })
    .map(cat => {
      const info = CATEGORY_MAP[cat] || { label: cat, icon: '📁' }
      return { key: cat, label: info.label, icon: info.icon, count: allConfigs.value[cat].length }
    })
})

const currentCategoryLabel = computed(() => (CATEGORY_MAP[currentCategory.value] || { label: currentCategory.value }).label)

// 当前分类配置项：按 CONFIG_METADATA 中的 sortOrder 排序，未定义的排到最后
const currentConfigs = computed(() => {
  const list = (allConfigs.value[currentCategory.value] || []).slice()
  list.sort((a, b) => {
    const sa = (CONFIG_METADATA[a.key] || {}).sortOrder || 999
    const sb = (CONFIG_METADATA[b.key] || {}).sortOrder || 999
    return sa - sb
  })
  return list
})

function selectCategory(cat) {
  currentCategory.value = cat
}

/* ============ 加载配置列表 ============ */
async function loadConfigs() {
  configLoading.value = true
  try {
    const data = await api.getJson('/api/v1/system/configs/')
    const groups = data.groups || {}
    originalValues = {}
    // 为每个配置项补充前端展示/编辑字段：label/description/unit 来自元数据；
    // draft 为当前编辑值（bool→布尔、数字→number、多选→数组），changed 标记是否修改
    for (const cat of Object.keys(groups)) {
      groups[cat] = groups[cat].map(c => {
        const meta = CONFIG_METADATA[c.key] || {}
        // 多选判定依据原始 description 是否含"多选"（覆盖 meta.description 之前取值）
        const isMulti = !!c.description && c.description.includes('多选')
        c.label = meta.label || c.label || c.key
        c.description = meta.description || c.description || ''
        c.unit = meta.unit || c.unit || ''
        c.isMulti = isMulti
        originalValues[c.key] = c.value
        c.draft = initDraft(c)
        c.secretEditing = false
        c.changed = false
        return c
      })
    }
    allConfigs.value = groups
    // 默认选中第一个有数据的分类
    const firstCat = Object.keys(allConfigs.value).find(k => allConfigs.value[k].length > 0)
    if (firstCat) selectCategory(firstCat)
  } catch (e) {
    ElMessage.error('加载配置失败: ' + errMsg(e, '未知错误'))
  } finally {
    configLoading.value = false
  }
}

/* ============ 控件初始值（按 value_type 归一化） ============ */
function initDraft(c) {
  const val = c.value == null ? '' : c.value
  if (c.value_type === 'bool') return val === 'true'
  if (c.isMulti) return val ? val.split(',').map(v => v.trim()).filter(Boolean) : []
  if (c.value_type === 'int' || c.value_type === 'float') {
    if (val === '') return undefined
    const n = Number(val)
    return isNaN(n) ? undefined : n
  }
  return val
}

/* ============ 获取控件当前值（与旧 getControlValue 对应，提交时使用） ============ */
function controlValue(c) {
  if (c.value_type === 'bool') return c.draft ? 'true' : 'false'
  if (c.isMulti) return Array.isArray(c.draft) ? c.draft.join(',') : ''
  if (c.value_type === 'int' || c.value_type === 'float') {
    return c.draft == null || c.draft === '' ? '' : String(c.draft)
  }
  return String(c.draft ?? '')
}

function isChanged(c) {
  return controlValue(c) !== String(originalValues[c.key] ?? '')
}

/* ============ 监听配置项变化：对比当前值与原始值，决定保存/重置按钮状态 ============ */
function onConfigChange(c) {
  c.changed = isChanged(c)
}

/* ============ 重置配置（恢复原始值） ============ */
function resetConfig(c) {
  c.draft = initDraft(c)
  c.secretEditing = false
  c.changed = false
}

/* ============ 敏感项启用编辑 ============ */
function enableSecretEdit(c) {
  c.secretEditing = true
  c.draft = originalValues[c.key] ?? ''
  onConfigChange(c)
}

/* ============ 多选组件：全选/清空（el-select 的 #header 插槽） ============ */
function selectAllMulti(c, selectAll) {
  c.draft = selectAll ? c.options.map(o => o.value) : []
  onConfigChange(c)
}

// 从 CONFIG_METADATA 读取多选定制文案，未配置时回退到通用文案
function multiSelectMeta(c) {
  return (CONFIG_METADATA[c.key] || {}).multiSelect || {}
}
// showSearch: false 时隐藏搜索框（如选项数固定且较少的多选场景）
function multiShowSearch(c) {
  return multiSelectMeta(c).showSearch !== false
}
function multiEmptyText(c) {
  return multiSelectMeta(c).emptyText || '未选择任何项'
}

/* ============ 提交变更工单（替代原 saveConfig 直改） ============
 * 配置修改不再直接落库，而是创建一份 ConfigChangeTicket 等待审批：
 * - 普通项：审核通过后生效
 * - 高风险项：审核 + 超管复核通过后生效
 * 提交时需填写变更原因，便于审批人判断是否通过。
 */
async function submitTicket(c) {
  if (saving) return
  // 获取当前控件值，敏感项未启用编辑时跳过（值为 *** 不允许提交）
  const currentValue = controlValue(c)
  if (c.is_secret && currentValue === '***') {
    ElMessage.error('敏感项请先点击"修改"输入新值')
    return
  }
  const riskText = c.risk_level === 'high' ? '，⚠️ 高风险项需复核' : ''
  try {
    // 弹出确认框填写变更原因（必填），二次确认避免误提交
    const { value: reason } = await ElMessageBox.prompt(
      `配置项：${c.label}（${c.key}）${riskText}`,
      '提交配置变更工单',
      {
        confirmButtonText: '提交工单',
        cancelButtonText: '取消',
        inputType: 'textarea',
        inputPlaceholder: '请说明本次配置变更的原因，便于审批人判断',
        inputValidator: v => (v && v.trim() ? true : '请填写变更原因'),
        inputErrorMessage: '请填写变更原因',
        type: 'info',
      }
    )
    saving = true
    const ticket = await api.postJson('/api/v1/system/tickets/', {
      ticket_type: 'config',
      config_key: c.key,
      new_value: currentValue,
      reason: reason.trim(),
    })
    // 提交成功后恢复控件为原值（工单未通过前配置未变）
    resetConfig(c)
    ElMessage.success(`工单已提交（#${ticket.id}），等待审批`)
  } catch (e) {
    // 用户取消输入框时不提示错误
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(`提交失败：${e.message}`)
  } finally {
    saving = false
  }
}

/* ==========================================================
   模型管理
   - openModelModal()    打开弹窗并加载模型列表
   - loadModels()        拉取后端按 model_type 分组的模型数据
   - showModelForm(m)    显示新增/编辑表单（m=null 为新增）
   - saveModel()         提交新增/编辑
   - deleteModel(id)     删除指定模型（需复核 + 检查依赖）
   ========================================================== */
const modelDialogVisible = ref(false)
const modelGroups = ref({})
const currentModelType = ref('llm')
const modelLoading = ref(false)
const modelSaving = ref(false)

async function openModelModal() {
  modelDialogVisible.value = true
  currentModelType.value = 'llm' // 默认选中 LLM tab
  await loadModels()
}

async function loadModels() {
  modelLoading.value = true
  try {
    const data = await api.getJson('/api/v1/system/llm-models/')
    modelGroups.value = data.groups || {}
  } catch (e) {
    ElMessage.error('加载模型列表失败: ' + errMsg(e, '未知错误'))
  } finally {
    modelLoading.value = false
  }
}

// 当前 tab 类型对应的模型列表
const modelList = computed(() => modelGroups.value[currentModelType.value] || [])

// 根据 id 查找模型对象（遍历所有类型分组）
function findModel(id) {
  for (const type of Object.keys(modelGroups.value)) {
    const found = modelGroups.value[type].find(m => m.id === id)
    if (found) return found
  }
  return null
}

/* ============ 模型新增/编辑表单 ============ */
const modelFormVisible = ref(false)
const modelFormEditId = ref(null)
const modelForm = reactive({ name: '', provider: '', model_type: 'llm', base_url: '', model_name: '', timeout: '', is_active: true, reason: '' })

function showModelForm(id) {
  const isEdit = id != null && id !== ''
  const m = isEdit ? findModel(id) : null
  // 编辑时若找不到对象（已被删除等），直接忽略
  if (isEdit && !m) {
    ElMessage.error('模型不存在，可能已被删除')
    return
  }
  modelFormEditId.value = isEdit ? m.id : null
  modelForm.name = isEdit ? m.name : ''
  modelForm.provider = isEdit ? m.provider : ''
  modelForm.model_type = isEdit ? (m.model_type || 'llm') : 'llm'
  modelForm.base_url = isEdit ? (m.base_url || '') : ''
  modelForm.model_name = isEdit ? m.model_name : ''
  modelForm.timeout = isEdit && m.timeout != null ? String(m.timeout) : ''
  modelForm.is_active = !isEdit || m.is_active
  modelForm.reason = ''
  modelFormVisible.value = true
}

async function saveModel() {
  if (modelSaving.value) return
  const name = modelForm.name.trim()
  const provider = modelForm.provider.trim()
  const model_name = modelForm.model_name.trim()
  if (!name) { ElMessage.warning('请填写显示名称'); return }
  if (!provider) { ElMessage.warning('请填写提供商'); return }
  if (!model_name) { ElMessage.warning('请填写模型标识'); return }

  const payload = {
    name,
    provider,
    model_type: modelForm.model_type,
    base_url: modelForm.base_url.trim(),
    model_name,
    // 超时为空时传 null，后端存 None，业务读取时回退到全局 LLM_TIMEOUT
    timeout: modelForm.timeout === '' || modelForm.timeout == null ? null : Number(modelForm.timeout),
    is_active: modelForm.is_active,
    reason: modelForm.reason ? modelForm.reason.trim() : '',
  }
  modelSaving.value = true
  try {
    if (modelFormEditId.value) {
      const resp = await api.patchJson(`/api/v1/system/llm-models/${modelFormEditId.value}/`, payload)
      // 后端返回 202 表示已提交审批，200 表示直接生效
      if (resp && resp.ticket_id) {
        ElMessage.warning(`已提交审批，工单 ID: ${resp.ticket_id}`)
      } else {
        ElMessage.success('模型已更新')
      }
    } else {
      await api.postJson('/api/v1/system/llm-models/', payload)
      ElMessage.success('模型已新增')
    }
    modelFormVisible.value = false
    await loadModels()
    currentModelType.value = payload.model_type
  } catch (e) {
    ElMessage.error(`保存失败：${e.message}`)
  } finally {
    modelSaving.value = false
  }
}

/* ============ 删除模型（超管复核 + 检查依赖） ============ */
function deleteModel(id) {
  const m = findModel(id)
  if (!m) {
    ElMessage.error('模型不存在，可能已被删除')
    return
  }
  // 删除需填写变更原因，并提交审批（超管复核）
  ElMessageBox.prompt(`确认删除模型「${m.name}」吗？此操作需复核，审批通过后才能删除。`, '删除模型', {
    confirmButtonText: '提交审批',
    cancelButtonText: '取消',
    inputType: 'textarea',
    inputPlaceholder: '请说明删除原因，便于审批',
    inputValidator: v => (v && v.trim() ? true : '请填写删除原因'),
    inputErrorMessage: '请填写删除原因',
    type: 'warning',
  }).then(async ({ value: reason }) => {
    try {
      // DELETE 带 body：api.delete 的 options.body 透传 JSON
      await api.deleteJson(`/api/v1/system/llm-models/${id}/`, { body: JSON.stringify({ reason: reason.trim() }) })
      ElMessage.warning('删除申请已提交，等待复核')
      await loadModels()
    } catch (e) {
      ElMessage.error(`提交失败：${e.message}`)
    }
  }).catch(() => {})
}

/* ==========================================================
   配置变更记录（已生效变更历史）
   - 拉取已通过工单（status=approved）作为变更历史，前端搜索 + 分页
   ========================================================== */
const historyVisible = ref(false)
const historyRecords = ref([])
const historyKeyword = ref('')
const historyPage = ref(1)
const HISTORY_PAGE_SIZE = 10 // 每页展示 10 条记录

async function openHistoryModal() {
  // 清空搜索框并重置页码，避免上次筛选残留
  historyKeyword.value = ''
  historyPage.value = 1
  historyVisible.value = true
  await loadHistoryRecords()
}

async function loadHistoryRecords() {
  try {
    // 只加载已通过的工单，展示实际生效的变更
    const data = await api.getJson('/api/v1/system/tickets/?status=approved&ticket_type=config')
    historyRecords.value = data.tickets || []
  } catch (e) {
    ElMessage.error('加载变更记录失败: ' + errMsg(e, '未知错误'))
  }
}

// 搜索 + 按生效时间倒序（最近的变更在最前）；关键词同时匹配 申请人 / 中文名 / 字段名
const historyFiltered = computed(() => {
  const keyword = historyKeyword.value.trim().toLowerCase()
  let records = [...historyRecords.value]
  records.sort((a, b) => {
    const ta = a.applied_at ? new Date(a.applied_at).getTime() : 0
    const tb = b.applied_at ? new Date(b.applied_at).getTime() : 0
    return tb - ta
  })
  if (keyword) {
    records = records.filter(r => {
      const creator = (r.creator || '').toLowerCase()
      const label = (r.config_label || '').toLowerCase()
      const key = (r.config_key || '').toLowerCase()
      return creator.includes(keyword) || label.includes(keyword) || key.includes(keyword)
    })
  }
  return records
})

// 数据量减少（搜索/过滤）导致当前页越界时回到第 1 页
watch(historyFiltered, list => {
  const totalPages = Math.max(1, Math.ceil(list.length / HISTORY_PAGE_SIZE))
  if (historyPage.value > totalPages) historyPage.value = 1
})

// 前端分页：全量数据按 HISTORY_PAGE_SIZE 切片展示当前页
const historyPageItems = computed(() => {
  const start = (historyPage.value - 1) * HISTORY_PAGE_SIZE
  return historyFiltered.value.slice(start, start + HISTORY_PAGE_SIZE)
})

function onHistoryPageChange(p) {
  historyPage.value = p
}

// 审批人信息：审核人 + 复核人（如果有）
function approverInfo(r) {
  return [
    r.auditor ? `审核：${r.auditor}` : '',
    r.reviewer ? `复核：${r.reviewer}` : '',
  ].filter(Boolean).join(' / ') || '-'
}

// 多行文本处理：合并 3 个及以上连续换行为 2 个（即多空行压缩为单空行）
function formatMultiline(text) {
  if (!text) return '-'
  return text.replace(/\n{3,}/g, '\n\n')
}

/* ============ 初始化 ============ */
onMounted(() => {
  userStore.restore()
  if (!userStore.isSystemMaintainer) return
  loadConfigs()
})
</script>

<style scoped>
/* ===== 主体两栏（page-body 内撑满，右侧列表内部滚动） ===== */
/* 覆盖全局 .text-sm：本页计数提示更紧凑，用 12px */
.text-sm {
  font-size: 12px;
}

.config-main {
  display: flex;
  gap: 16px;
  flex: 1;
  min-height: 0;
}

/* 覆盖全局 .app-card 的 margin-bottom 与 .app-card + .app-card 的 margin-top：
   .config-main 的 gap:16px 已负责卡片间距，去掉卡片自身的冗余外间距。
   选择器带 .app-card 前缀提升特异性，避免被全局规则按加载顺序压过 */
.app-card.config-nav-card {
  width: 220px;
  flex-shrink: 0;
  align-self: stretch;
  overflow-y: auto;
  padding: 16px;
  margin: 0;
}

.app-card.config-list-card {
  flex: 1;
  min-width: 0;
  overflow-y: auto;
  padding: 16px;
  margin: 0;
}

.config-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

/* ===== 分类导航 ===== */
.config-nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 12px;
}

.config-nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: all 0.2s ease;
  font-size: 13px;
  color: var(--app-text);
  position: relative;
}

.config-nav-item:hover {
  background: var(--app-bg);
}

.config-nav-item.active {
  background: #eef2ff;
  color: #4f46e5;
  font-weight: 600;
}

/* 选中态左侧指示条 */
.config-nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  transform: translateY(-50%);
  width: 3px;
  height: 60%;
  background: #4f46e5;
  border-radius: 0 3px 3px 0;
}

.config-nav-icon {
  font-size: 15px;
  flex-shrink: 0;
}

.config-nav-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.config-nav-count {
  font-size: 11px;
  color: var(--app-text-sub);
  background: var(--app-menu-hover);
  padding: 1px 7px;
  border-radius: 10px;
  flex-shrink: 0;
  font-weight: 400;
}

.config-nav-item.active .config-nav-count {
  background: rgba(79, 70, 229, 0.12);
  color: #4f46e5;
}

/* ===== 配置项行（两行结构，左右对齐） ===== */
.config-list {
  display: flex;
  flex-direction: column;
}

.config-item {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--app-border);
  transition: background 0.15s;
}

.config-item:hover {
  background: var(--app-menu-hover);
}

.config-item:last-child {
  border-bottom: none;
}

/* 每行通用布局：左侧 220px + 右侧 flex:1 + 操作区 */
.config-item-row {
  display: flex;
  align-items: center;
  gap: 16px;
}

/* 第一行：中文名 */
.config-item-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text);
  line-height: 1.4;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  flex-shrink: 0;
  width: 220px;
  min-width: 0;
}

/* 控件区 */
.config-item-control {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 6px;
}

/* 操作按钮区 */
.config-item-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
  align-items: center;
  min-width: 96px;
  justify-content: flex-end;
}

/* 第二行：字段名 + 解释 */
.config-item-meta {
  padding-top: 2px;
}

.config-item-meta-left {
  width: 220px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.config-item-key-text {
  font-size: 10px;
  color: var(--app-text-sub);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  opacity: 0.65;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.config-item-hint {
  font-size: 11px;
  color: var(--app-text-sub);
  line-height: 1.5;
  flex: 1;
  min-width: 0;
  white-space: normal;
  word-break: break-word;
}

.config-item-updated {
  font-size: 10px;
  color: var(--app-text-sub);
  margin-left: auto;
  white-space: nowrap;
}

/* 只读项：控件半透明 + 不可交互 */
.config-item.readonly .config-item-control {
  opacity: 0.5;
  pointer-events: none;
}

/* 控件宽度限制 */
.text-input {
  max-width: 480px;
}

.num-input {
  max-width: 220px;
}

.json-input {
  max-width: 480px;
}

.json-input :deep(.el-textarea__inner) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  resize: vertical;
  line-height: 1.5;
}

.single-select {
  max-width: 360px;
}

.multi-select {
  max-width: 560px;
  width: 100%;
  min-width: 320px;
}

/* 多选下拉头部：全选/清空 */
.ms-actions {
  display: flex;
  gap: 12px;
  padding: 2px 12px 6px;
  border-bottom: 1px solid var(--app-border);
  margin-bottom: 4px;
}

/* 单位标签（控件后面灰色小字） */
.config-unit {
  font-size: 12px;
  color: var(--app-text-sub);
  margin-left: 6px;
  white-space: nowrap;
  flex-shrink: 0;
}

/* 敏感项编辑 */
.secret-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ===== 模型管理弹窗 ===== */
/* body 列布局：tab 与提示固定，表格区域撑满剩余高度并内部滚动 */
.model-dialog-body {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.model-table-wrap {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.model-tabs {
  margin-bottom: 12px;
  flex-shrink: 0;
}

.model-table {
  width: 100%;
}

.model-tip {
  margin-top: 12px;
  font-size: 12px;
  color: var(--app-text-sub);
  line-height: 1.5;
  flex-shrink: 0;
}

.model-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
}

/* ===== 配置变更记录弹窗 ===== */
.history-search-bar {
  margin-bottom: 12px;
}

.history-list-body {
  max-height: 55vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.history-card {
  border: 1px solid var(--app-border);
  border-radius: 8px;
  overflow: hidden;
  transition: box-shadow 0.15s;
}

.history-card:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.history-card-header {
  padding: 10px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: var(--app-menu-hover);
}

.history-card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.ticket-config-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--app-text);
}

.ticket-config-key {
  font-size: 10px;
  color: var(--app-text-sub);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  background: var(--app-menu-hover);
  padding: 1px 6px;
  border-radius: 4px;
}

.history-card-time {
  font-size: 12px;
  color: var(--app-text-sub);
  white-space: nowrap;
}

.history-card-body {
  padding: 12px 14px;
  background: var(--app-card-bg);
}

.history-card-diff {
  display: flex;
  align-items: center;
  gap: 10px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.history-old {
  background: #fef2f2;
  color: #991b1b;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid #fecaca;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-arrow {
  color: var(--app-text-sub);
  font-weight: bold;
}

.history-new {
  background: #f0fdf4;
  color: #166534;
  padding: 2px 8px;
  border-radius: 4px;
  border: 1px solid #bbf7d0;
  max-width: 240px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.history-card-meta {
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 12px;
  color: var(--app-text-sub);
  flex-wrap: wrap;
  margin-bottom: 6px;
}

.history-card-reason {
  font-size: 13px;
  color: var(--el-text-color-regular);
  line-height: 1.5;
  padding-top: 6px;
  border-top: 1px dashed var(--app-border);
  white-space: pre-wrap;
  word-break: break-all;
}

.history-pagination {
  justify-content: flex-end;
}

/* ===== 响应式 ===== */
@media (max-width: 1100px) {
  .config-main {
    flex-direction: column;
  }
  .app-card.config-nav-card {
    width: 100%;
  }
  .config-nav {
    flex-direction: row;
    flex-wrap: wrap;
  }
}
</style>
