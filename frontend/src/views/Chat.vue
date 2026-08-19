<template>
  <div class="chat-page">
    <!-- ===== 主内容列：顶部工具栏 + 消息区 + 输入区 ===== -->
    <div class="chat-main">
      <!-- ===== 顶部工具栏：来源选择 + 问答模式 + 新建会话 ===== -->
      <div class="chat-header">
        <div class="chat-title-input" v-if="currentSessionTitle">{{ currentSessionTitle }}</div>
        <div class="header-right">
          <!-- 知识来源选择器（来源开关 + 知识范围节点树，自 Chat.vue 抽出） -->
          <SourceScopePicker
            v-model:sources="currentSources"
            v-model:enabled="enabledSources"
            v-model:scopes="selectedScopeIds"
            :disabled="isRagMode"
          />
          <!-- 快速问答模式提示 -->
          <span v-if="isRagMode" class="tag tag-info" style="color: var(--el-color-warning)">
            ⚡ 快速问答仅基于内部文档回答
          </span>
          <el-radio-group v-model="currentMode" class="mode-switcher" @change="setChatMode">
            <el-radio-button value="rag" title="快速问答：单次检索 + LLM 生成，延迟最低">⚡ 快速问答</el-radio-button>
            <el-radio-button value="agent" title="智能问答：Agent 决策 + 工具调用（推荐）">🧠 智能问答</el-radio-button>
            <el-radio-button value="plan" title="深度分析：规划→并行执行→综合生成，适合复杂问题">🔬 深度分析</el-radio-button>
          </el-radio-group>
          <span class="tag tag-info">💡 4 层记忆</span>
          <el-button size="small" @click="newSession">+ 新建会话</el-button>
        </div>
      </div>

      <!-- ===== 消息区 ===== -->
      <div class="chat-messages" ref="msgWrapEl">
        <div v-if="loadingRecords" class="empty-state">
          <el-empty description="加载会话记录中...">
            <el-icon class="is-loading" :size="22"><Loading /></el-icon>
          </el-empty>
        </div>
        <template v-else-if="messages.length === 0">
          <div class="empty-state">
            <div class="empty-inner">
              <div class="empty-icon">💬</div>
              <div class="empty-title">欢迎使用智能聊天</div>
              <div class="empty-desc">选择知识来源，开始提问吧</div>
            </div>
          </div>
        </template>
        <template v-else>
            <ChatMessageItem
              v-for="msg in messages"
              :key="msg.mid"
              :msg="msg"
              :user-initial="userInitial"
              :handlers="chatMsgHandlers"
            />
        </template>
      </div>

      <!-- ===== 输入区 ===== -->
      <div class="chat-input-area">
        <div class="chat-input-wrap">
          <el-input
            v-model="draft"
            type="textarea"
            class="chat-input"
            :autosize="{ minRows: 2, maxRows: 6 }"
            resize="none"
            placeholder="请输入你的问题，Shift + Enter 换行，Enter 发送…"
            @input="onDraftInput"
            @keydown.enter.exact.prevent="sendChat"
          />
          <div class="chat-input-actions">
            <el-button
              v-if="!isSending"
              type="primary"
              class="chat-send-btn"
              @click="sendChat"
            >发送 ↵</el-button>
            <el-button
              v-else
              type="danger"
              class="chat-send-btn stopping"
              @click="stopChat"
            >⏹ 终止</el-button>
          </div>
        </div>
      </div>
    </div>

    <!-- ===== 历史会话右栏 ===== -->
    <!-- 历史会话侧边栏（自 Chat.vue 抽出，搜索/编辑/删除通过事件上抛） -->
    <SessionSidebar
      :sessions="sessionCache"
      :current-id="currentSessionId"
      @switch="switchSession"
      @delete="delSession"
      @save-title="saveSessionTitle"
      @search="onSessionSearch"
    />

    <!-- 文档预览弹窗（公共组件） -->
    <DocPreviewDialog v-model="previewVisible" :doc-id="previewDocId" :initial-page="previewInitialPage" />
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { useUserStore } from '../stores/user'
import api from '../api/http'
import { errMsg, formatDuration, formatSessionTime } from '../utils/format'
import { buildSourceData, formatAnswer, formatToolArgs, formatToolResult, wfNodeStatusText, wfStepIcon, workflowStatusText } from '../utils/chatRender'
import { useConfirm } from '../composables/useConfirm'
import ChatMessageItem from '../components/chat/ChatMessageItem.vue'
import SessionSidebar from '../components/chat/SessionSidebar.vue'
import SourceScopePicker from '../components/chat/SourceScopePicker.vue'
import DocPreviewDialog from '../components/doc-preview/DocPreviewDialog.vue'

const userStore = useUserStore()
// 二次确认弹窗统一封装
const { confirm } = useConfirm()

const userInitial = computed(() => (userStore.name || '?').slice(0, 2))

/* ==========================================================
   常量与本地存储 key（与旧 chat.js 保持一致，不可变更）
   ========================================================== */
const MODE_STORAGE_KEY = 'rag_chat_mode'         // 问答模式持久化
const DRAFT_KEY = 'rag_chat_draft'               // 输入草稿（sessionStorage）
const SESSION_CACHE_PREFIX = 'rag_session_cache_' // 会话详情缓存前缀
const MAX_CACHED_SESSIONS = 20                   // 只缓存最近 20 条会话
const MAX_SESSION_CACHE_SIZE = 51200             // 单条缓存上限 50KB
const FEEDBACK_STATE_KEY = 'chat_feedback_states' // 反馈状态本地持久化
const FEEDBACK_STATE_MAX = 200
const FEEDBACK_STATE_TTL = 90 * 86400000
const TYPING_CHARS_PER_STEP = 3   // 打字机每步补字字符数
const TYPING_INTERVAL_MS = 16     // 打字机补帧间隔

/* ==========================================================
   状态定义
   ========================================================== */
// 数据来源（SourceScopePicker 通过 v-model 维护勾选状态，此处仅持有供发送消息读取）
const enabledSources = ref(['doc', 'db', 'web', 'llm'])
const currentSources = reactive({ doc: true, db: true, web: true, llm: true })
// 问答模式：rag（快速问答）/ agent（智能问答）/ plan（深度分析）
const currentMode = ref('agent')
// 快速问答模式下，数据来源强制为仅内部文档
const isRagMode = computed(() => currentMode.value === 'rag')
// 选中的知识范围节点 ID 集合（统一存字符串 ID，避免与 API 数字 ID 混淆；由 SourceScopePicker 维护）
const selectedScopeIds = ref(new Set())
// 会话
const currentSessionId = ref(null)
const sessionCache = ref([])
const searchKeyword = ref('')
const loadingRecords = ref(false)
// 消息
const messages = ref([])
const msgWrapEl = ref(null)
const isSending = ref(false)
const currentAbortController = ref(null)          // 当前流式请求的 AbortController，供 stopChat 中断
const userAborted = ref(false)                    // 标记用户主动终止（区分超时中断）
const draft = ref('')                             // 输入草稿
// 会话标题（顶部展示）
const currentSessionTitle = ref('')

// 打字机动画 timer（key=消息对象，非响应式，避免污染响应式对象）
const typingTimers = new Map()
let draftSaveTimer = null

/* ==========================================================
   问答模式切换
   ========================================================== */
// 从 localStorage 恢复上次选择的模式，默认 agent
function initModeSwitcher() {
  const saved = localStorage.getItem(MODE_STORAGE_KEY)
  if (saved && ['rag', 'agent', 'plan'].includes(saved)) {
    currentMode.value = saved
  }
}

function setChatMode(mode) {
  if (!['rag', 'agent', 'plan'].includes(mode)) return
  currentMode.value = mode
  localStorage.setItem(MODE_STORAGE_KEY, mode)
  const label = { rag: '快速问答', agent: '智能问答', plan: '深度分析' }[mode]
  ElMessage.success('已切换为 ' + label + ' 模式')

  // 快速问答模式：强制仅内部文档，禁用其他来源
  if (mode === 'rag') {
    currentSources.doc = true
    currentSources.db = false
    currentSources.web = false
    currentSources.llm = false
    enabledSources.value = ['doc']
  } else {
    // 恢复所有来源
    currentSources.doc = true
    currentSources.db = true
    currentSources.web = true
    currentSources.llm = true
    enabledSources.value = ['doc', 'db', 'web', 'llm']
  }
}

/* ==========================================================
   输入草稿（sessionStorage：标签页关闭即失效，避免跨会话污染）
   ========================================================== */
function initDraftRestore() {
  try {
    const raw = sessionStorage.getItem(DRAFT_KEY)
    if (raw) draft.value = raw
  } catch (e) { /* sessionStorage 不可用时静默降级 */ }
}

function onDraftInput() {
  if (draftSaveTimer) clearTimeout(draftSaveTimer)
  draftSaveTimer = setTimeout(() => {
    try {
      const val = draft.value.trim()
      if (val) sessionStorage.setItem(DRAFT_KEY, val)
      else sessionStorage.removeItem(DRAFT_KEY)
    } catch (e) { /* ignore */ }
  }, 300)
}

// 思考区摘要文本：工具调用次数 + 总耗时（进行中显示"执行中..."）
function updateThinkingSummary(area, toolCount, totalMs) {
  let text = toolCount + ' 次工具调用'
  if (totalMs != null) text += ' · 总计 ' + formatDuration(totalMs)
  else text += ' · 执行中...'
  area.summary = text
}

// 更新单节点卡片状态（状态文案 + 耗时）
function setWfNodeStatus(msg, nodeId, status, latencyMs) {
  const wf = msg.workflow
  if (!wf) return
  const node = wf.nodes.find(n => n.id === nodeId)
  if (!node) return
  node.status = status
  if (latencyMs != null) node.latencyMs = latencyMs
  scrollChatBottom()
}

// 审批通过/驳回后刷新工作流结果（"我已审批，刷新结果"按钮触发）
async function refreshWorkflowResult(msg) {
  const wf = msg.workflow
  if (!wf || !wf.wfId) return
  try {
    const data = await api.getJson('/api/v1/agent/workflows/' + wf.wfId + '/')
    if (!data || data.status === 'waiting_approval') {
      ElMessage.warning('该步骤仍在等待审批确认')
      return
    }
    wf.status = data.status
    // 重绘节点轨迹（含审批结果与后续新增节点）
    const existingIds = new Set(wf.nodes.map(n => n.id))
    ;(data.nodes || []).forEach(n => {
      if (!existingIds.has(n.node_id)) {
        wf.nodes.push({ id: n.node_id, type: n.step_type, name: n.node_name || n.node_id, status: 'pending', latencyMs: null })
      }
      const node = wf.nodes.find(x => x.id === n.node_id)
      if (node) {
        node.status = n.status
        if (n.latency_ms != null) node.latencyMs = n.latency_ms
      }
    })
    // 移除审批确认卡片
    wf.approval = null
    const result = data.result || {}
    // 渲染最终答案（waiting_approval 期间 answerText 为空，直接填充）
    msg.answerText = result.answer || msg.answerText
    msg.answerHtml = formatAnswer(result.answer || '', result.citations || [])
    // 回填真实 message_id 到反馈按钮
    if (result.qa_id) {
      msg.messageId = result.qa_id
      msg.feedback.rating = 0
      msg.feedback.locked = false
      msg.latencyText = '已完成'
    }
    // 渲染溯源区（citations 为空时 buildSourceData 内部兜底"大模型知识"标签行）
    const src = buildSourceData(result.citations, undefined, undefined, undefined, result.answer_type, currentSources.llm)
    msg.sourceCards = src.cards
    msg.sourceTags = src.tags
    scrollChatBottom()
  } catch (e) {
    ElMessage.error('刷新工作流结果失败：' + errMsg(e, '未知错误'))
  }
}

// 内嵌确认/拒绝：敏感工具节点的轻量级 HITL，直接调用 API 恢复工作流（不创建工单）
async function submitInlineApproval(msg, approved) {
  const wf = msg.workflow
  const appr = wf && wf.approval
  if (!wf || !appr) return
  appr.submitting = true
  try {
    await api.postJson('/api/v1/agent/workflows/' + wf.wfId + '/approve/', { node_id: appr.nodeId, approved })
    wf.approval = null
    refreshWorkflowResult(msg)
  } catch (e) {
    ElMessage.error('操作失败：' + errMsg(e, '未知错误'))
    appr.submitting = false
  }
}

/* ==========================================================
   消息滚动
   ========================================================== */
let scrollScheduled = false
function scrollChatBottom() {
  if (scrollScheduled) return
  scrollScheduled = true
  requestAnimationFrame(() => {
    const el = msgWrapEl.value
    if (el) el.scrollTop = el.scrollHeight
    scrollScheduled = false
  })
}

/* ==========================================================
   发送消息（SSE 流式）
   ========================================================== */
function stopChat() {
  if (currentAbortController.value) {
    userAborted.value = true
    currentAbortController.value.abort()
  }
}

// 打字机逐字符补帧渲染（displayText 逐步逼近 answerText，保持视觉连贯）
function startTypingAnimation(msg) {
  if (typingTimers.has(msg)) return
  const timer = setInterval(() => {
    if (msg.displayText.length >= msg.answerText.length) {
      clearInterval(timer)
      typingTimers.delete(msg)
      return
    }
    const end = Math.min(msg.answerText.length, msg.displayText.length + TYPING_CHARS_PER_STEP)
    msg.displayText = msg.answerText.slice(0, end)
    msg.answerHtml = formatAnswer(msg.displayText, msg.citations)
    scrollChatBottom()
  }, TYPING_INTERVAL_MS)
  typingTimers.set(msg, timer)
}

function stopTypingAnimation(msg) {
  const timer = typingTimers.get(msg)
  if (timer) {
    clearInterval(timer)
    typingTimers.delete(msg)
  }
}

// 强制把 displayText 对齐到 answerText（done / 用户终止等收尾场景）
function flushDisplayText(msg) {
  stopTypingAnimation(msg)
  msg.displayText = msg.answerText
  msg.answerHtml = formatAnswer(msg.displayText, msg.citations)
}

function createAiMessage(mid) {
  return reactive({
    mid, role: 'ai',
    thinking: true,          // start 事件前显示"思考中"占位
    started: false,          // start 事件后渲染回答骨架
    placeholder: '',         // start→first_token 间的占位文本（html）
    answerText: '',          // 完整 answer 文本（后端已到达的全部 delta 合并）
    displayText: '',         // 已展示到前端的文本（打字机效果用）
    answerHtml: '',          // 展示用（formatAnswer 渲染结果）
    citations: [],
    routeSource: null,
    toolTraces: [],
    answerType: null,
    messageId: null,
    ttfbMs: 0,
    totalMs: 0,
    latencyText: '',
    thinkingArea: null,      // 思考区（Agent 工具调用链，惰性创建）
    workflow: null,          // 多 Agent 工作流轨迹区
    sourceCards: [],
    sourceTags: [],
    feedback: { rating: 0, locked: false, detailOpen: false, detailText: '' },
    filtered: null,          // 内容审查拦截卡片
    error: null,             // 发送失败/超时错误
  })
}

async function sendChat() {
  if (isSending.value) return
  const text = draft.value.trim()
  if (!text) return
  isSending.value = true

  const now = new Date()
  const time = String(now.getHours()).padStart(2, '0') + ':' + String(now.getMinutes()).padStart(2, '0')
  const uMsg = { mid: 'u' + Date.now(), role: 'user', content: text, time }
  messages.value.push(uMsg)
  draft.value = ''
  sessionStorage.removeItem(DRAFT_KEY)
  scrollChatBottom()

  const mid = 'm' + Date.now()
  const msg = createAiMessage(mid)
  messages.value.push(msg)
  scrollChatBottom()

  const body = {
    question: text,
    root_types: [],
    node_ids: [...selectedScopeIds.value].map(Number),
    sources: enabledSources.value.filter(k => currentSources[k]),
    use_cache: true,
    do_task_split: false,
    mode: currentMode.value,
    // 多 Agent 工作流开关：rag/plan 模式下不启用；
    // agent 模式由后端编排器判断——复杂问题走工作流（含 HITL 审批）
    do_workflow: currentMode.value === 'agent'
  }
  if (currentSessionId.value) {
    body.session_id = currentSessionId.value
  }

  // 流式请求可能持续较久，120s 超时自动中断
  const abortController = new AbortController()
  currentAbortController.value = abortController
  const timeoutId = setTimeout(() => abortController.abort(), 120000)

  try {
    await api.stream('/api/v1/chat/ask_stream/', body, (chunk) => {
      if (!chunk) return
      // 兼容 streamer.py 外层兜底异常（无 type，仅 error/finish 字段）
      if (!chunk.type && chunk.error) {
        msg.error = { text: '生成失败：' + chunk.error, hint: '', retryText: '' }
        msg.thinking = false
        msg.started = true
        return
      }
      if (!chunk.type) return
      switch (chunk.type) {
        case 'start': {
          // 后端已响应：切换"思考中"占位 → 回答骨架
          msg.thinking = false
          msg.started = true
          if (chunk.session_id) {
            currentSessionId.value = chunk.session_id
            const title = text.slice(0, 30) + (text.length > 30 ? '...' : '')
            if (!currentSessionTitle.value) {
              currentSessionTitle.value = title
              updateSessionTitle(chunk.session_id, title)
            }
          }
          msg.citations = chunk.citations || []
          // 答案区占位文本：避免 start→delta 间隔显示空白框
          // Agent/Plan 模式用更友好的占位，RAG 模式用"检索并生成中..."占位
          if (chunk.is_agent) {
            msg.placeholder = '<p style="color:var(--app-text-sub)">🧠 LLM 正在分析问题，必要时会调用工具... 请稍候</p>'
          } else {
            msg.placeholder = '<p style="color:var(--app-text-sub)">🔎 正在检索知识库并生成答案，请稍候...</p>'
          }
          // Agent 模式下工具尚未执行完，先不渲染"基于模型知识"占位，避免闪烁
          const src = buildSourceData(msg.citations, chunk.route_source, undefined, !!chunk.is_agent, null, currentSources.llm)
          msg.sourceCards = src.cards
          msg.sourceTags = src.tags
          msg.latencyText = '生成中...'
          scrollChatBottom()
          break
        }
        case 'tool_call': {
          // Agent 工具调用开始：在思考区追加一张工具调用卡片
          if (!msg.thinkingArea) {
            msg.thinkingArea = reactive({ collapsed: false, summary: '', toolCalls: [] })
          }
          const toolCallCount = msg.thinkingArea.toolCalls.length
          const callId = chunk.call_id || ('call_' + (toolCallCount + 1))
          msg.thinkingArea.toolCalls.push({
            callId, name: chunk.tool_name || 'unknown',
            argsText: formatToolArgs(chunk.tool_args || {}),
            status: 'running', statusText: '执行中...',
            latencyText: '', resultHtml: '', resultCls: '',
          })
          updateThinkingSummary(msg.thinkingArea, toolCallCount + 1, null)
          scrollChatBottom()
          break
        }
        case 'tool_result': {
          // 工具执行完成：回填对应卡片的 status / latency / result
          const toolCalls = msg.thinkingArea ? msg.thinkingArea.toolCalls : []
          const callId = chunk.call_id || ('call_' + toolCalls.length)
          const card = toolCalls.find(c => c.callId === callId)
          if (!card) break
          const ok = !!chunk.ok
          card.status = ok ? 'ok' : 'fail'
          card.statusText = ok ? '成功' : '失败'
          if (chunk.latency_ms != null) card.latencyText = formatDuration(chunk.latency_ms)
          // 工具结果按 Markdown 渲染（数据库表格/LLM 输出美化；截断预览按已聚合行渲染）
          card.resultHtml = formatToolResult(chunk.result_preview || '')
          card.resultCls = ok ? 'ok' : 'fail'
          if (msg.thinkingArea) updateThinkingSummary(msg.thinkingArea, msg.thinkingArea.toolCalls.length, null)
          scrollChatBottom()
          break
        }
        case 'workflow_planning': {
          // 编排器判断阶段（do_workflow 模式）：更新思考占位提示
          msg.placeholder = '<p style="color:var(--app-text-sub)">🧠 正在规划多 Agent 工作流...</p>'
          break
        }
        case 'workflow_start': {
          // 工作流已创建：渲染节点 DAG 轨迹区（插入回答骨架顶部）
          msg.workflow = reactive({
            wfId: chunk.workflow_id,
            status: 'running',
            nodes: (chunk.nodes || []).map(n => ({
              id: n.id, type: n.type, name: n.name || n.id,
              status: 'pending', latencyMs: null,
            })),
            approval: null,
          })
          scrollChatBottom()
          break
        }
        case 'workflow_node_start': {
          setWfNodeStatus(msg, chunk.node_id, 'running')
          break
        }
        case 'workflow_node_done': {
          setWfNodeStatus(msg, chunk.node_id, chunk.status, chunk.latency_ms)
          break
        }
        case 'workflow_approval_required': {
          // HITL：统一在对话框内嵌确认
          setWfNodeStatus(msg, chunk.node_id, 'blocked')
          if (msg.workflow) {
            msg.workflow.status = 'waiting_approval'
            msg.workflow.approval = reactive({ nodeId: chunk.node_id, reason: chunk.reason || '', submitting: false })
          }
          scrollChatBottom()
          break
        }
        case 'first_token': {
          msg.ttfbMs = chunk.ttfb_ms || 0
          // first_token 到达时：清空占位文本，重置 answerText/displayText 为空
          // （真正的文本会由紧随其后的 delta 逐字输出）
          msg.placeholder = ''
          msg.answerText = ''
          msg.displayText = ''
          msg.answerHtml = ''
          msg.latencyText = '首字 ' + (msg.ttfbMs / 1000).toFixed(2) + 's · 生成中...'
          break
        }
        case 'delta': {
          // 合并到 answerText，打字机补帧动画自行显示（16ms 间隔逐字补）
          msg.answerText += chunk.delta || ''
          startTypingAnimation(msg)
          break
        }
        case 'done': {
          // 多 Agent 工作流分支：
          // 1) 审批阻塞（message_id 为空，工作流停留 waiting_approval）：
          //    不落历史记录，仅提示用户去工单中心确认，等审批后手动刷新结果
          // 2) 工作流完成（succeeded/degraded/failed）：更新状态后走通用收尾
          if (chunk.is_workflow && chunk.status === 'waiting_approval') {
            if (msg.workflow) msg.workflow.status = 'waiting_approval'
            msg.latencyText = '已暂停，等待人工确认'
            break
          }
          if (chunk.is_workflow && msg.workflow) {
            msg.workflow.status = chunk.status
          }
          msg.messageId = chunk.message_id
          // 同步回填 messageId 到对应的用户消息（撤回/删除操作需要通过用户消息定位 QaRecord）
          const msgIdx = messages.value.findIndex(m => m.mid === msg.mid)
          if (msgIdx > 0 && messages.value[msgIdx - 1].role === 'user') {
            messages.value[msgIdx - 1].messageId = chunk.message_id
          }
          msg.totalMs = (chunk.stats && chunk.stats.total_ms) || 0
          msg.ttfbMs = (chunk.stats && chunk.stats.ttfb_ms) || msg.ttfbMs
          msg.citations = chunk.citations || msg.citations
          msg.toolTraces = chunk.tool_traces || []
          msg.answerType = chunk.answer_type

          // 命中审查拦截时跳过 flushDisplayText：content_filtered 事件已清空
          // answerText 并渲染拦截卡片，flush 会用 formatAnswer('') 覆盖卡片为"暂无回答"
          if (!chunk.is_filtered) {
            flushDisplayText(msg)
          }

          // 刷新溯源区（answer_type 用于区分拒答——拒答时不渲染来源标识）
          const src = buildSourceData(msg.citations, chunk.route_source, msg.toolTraces, undefined, chunk.answer_type, currentSources.llm)
          msg.sourceCards = src.cards
          msg.sourceTags = src.tags

          // 展示首字 + 总计耗时
          const ttfb = (msg.ttfbMs / 1000).toFixed(2)
          const total = (msg.totalMs / 1000).toFixed(2)
          msg.latencyText = '首字 ' + ttfb + 's · 总计 ' + total + 's'

          // 同一问答再次渲染时恢复历史反馈状态（刷新/切换会话重载后）
          restoreFeedbackBar(msg)

          // 思考区摘要收尾：补全总耗时；若全流程无工具调用，则移除空思考区；
          // 有工具调用时自动折叠思考区（结束后折叠避免干扰阅读答案）
          if (msg.thinkingArea) {
            if (msg.thinkingArea.toolCalls.length === 0) {
              msg.thinkingArea = null
            } else {
              updateThinkingSummary(msg.thinkingArea, msg.thinkingArea.toolCalls.length, msg.totalMs)
              msg.thinkingArea.collapsed = true
            }
          }
          scrollChatBottom()
          // 增量更新会话列表（预览+时间+置顶），不重新请求后端
          if (currentSessionId.value) {
            updateSessionInCache(currentSessionId.value, text.slice(0, 50), text)
            // 同步更新会话详情缓存：追加本轮 QaRecord（含 answer_type 供历史来源标识）
            const newRecord = {
              id: msg.messageId,
              question: text,
              answer: msg.answerText,
              citations: msg.citations,
              latency_total_ms: msg.totalMs,
              latency_ttfb_ms: msg.ttfbMs,
              created_at: new Date().toISOString(),
              tool_traces: msg.toolTraces,
              answer_type: chunk.answer_type,
            }
            const cache = getSessionCache(currentSessionId.value)
            if (cache && cache.records) {
              cache.records.push(newRecord)
              setSessionCache(currentSessionId.value, cache.records, new Date().toISOString())
            }
          }
          break
        }
        case 'content_filtered': {
          // 命中敏感词 block：立即停止打字机，清空已展示内容，显示拦截提示卡片
          // 不暴露具体命中词（避免二次传播违规内容），仅提示"违规已拦截"
          stopTypingAnimation(msg)
          msg.answerText = ''
          msg.displayText = ''
          msg.answerHtml = ''
          msg.filtered = {
            category: chunk.category || 'other',
            reason: chunk.reason || '检测到违规内容，已拦截',
            qaId: null,       // done 事件回填后才能启用"反馈误判"
            formOpen: false, comment: '', submitted: false,
          }
          // 隐藏溯源区（拦截时无引用）
          msg.sourceCards = []
          msg.sourceTags = []
          msg.latencyText = '已拦截' + (msg.ttfbMs > 0 ? ' · 首字 ' + formatDuration(msg.ttfbMs) : '')
          scrollChatBottom()
          break
        }
        case 'error': {
          // 错误时立即停止打字机动画，避免对已脱离 DOM 的节点空转
          stopTypingAnimation(msg)
          msg.error = { text: '生成失败：' + (chunk.detail || '未知错误'), hint: '', retryText: '' }
          break
        }
      }
    }, { signal: abortController.signal })
  } catch (e) {
    // 区分：用户主动终止 vs 网络/超时异常
    if (userAborted.value) {
      // 用户主动终止：停止打字机动画，保留已生成的部分回答，标注"已终止"
      flushDisplayText(msg)
      if (msg.answerText) {
        msg.answerHtml = formatAnswer(msg.answerText, msg.citations)
      }
      msg.latencyText = '已终止' + (msg.ttfbMs > 0 ? ' · 首字 ' + formatDuration(msg.ttfbMs) : '')
      if (!msg.started) {
        // start 事件未到达就已终止：显示简短提示
        msg.thinking = false
        msg.started = true
        msg.error = { text: '已终止', hint: '', retryText: '' }
      }
      scrollChatBottom()
    } else {
      console.error('stream chat failed:', e)
      // 异常时也停止打字机动画，避免空转
      stopTypingAnimation(msg)
      const isTimeout = e.name === 'AbortError'
      msg.thinking = false
      msg.started = true
      msg.error = {
        text: isTimeout ? '请求超时，请稍后重试' : '发送失败：' + e.message,
        hint: isTimeout ? '服务器响应时间过长，请检查网络或缩短提问内容' : '请检查网络连接或重试',
        retryText: text,
      }
      scrollChatBottom()
    }
  } finally {
    // 保底重置：无论流式成功、异常还是中断，都必须释放发送锁，否则后续无法发送
    clearTimeout(timeoutId)
    currentAbortController.value = null
    userAborted.value = false
    isSending.value = false
  }
}

// 失败重试：与原 chat.js 行为一致——仅把原问题填回输入框再发送，不删除失败消息
// （失败消息保留错误提示，新发送会产生新的 AI 消息）
function retrySendChat(msg) {
  draft.value = msg.error.retryText || ''
  sendChat()
}

/* ==========================================================
   反馈
   ========================================================== */
// 反馈状态本地持久化：key=qa_id，value={rating, ts}
// 刷新页面后恢复已反馈状态并锁定按钮，防止重复反馈（覆盖原始评价）
function getFeedbackStates() {
  try {
    return JSON.parse(localStorage.getItem(FEEDBACK_STATE_KEY)) || {}
  } catch (e) {
    return {}
  }
}

function setFeedbackStates(states) {
  try {
    localStorage.setItem(FEEDBACK_STATE_KEY, JSON.stringify(states))
  } catch (e) {
    // localStorage 存满（配额超限）时静默降级：仅本次刷新不恢复状态，不影响反馈提交
  }
}

function saveFeedbackState(qaId, rating) {
  const states = getFeedbackStates()
  states[qaId] = { rating, ts: Date.now() }
  // 容量保护：仅保留最近 FEEDBACK_STATE_MAX 条，避免长期使用后本地存储膨胀
  const keys = Object.keys(states)
  if (keys.length > FEEDBACK_STATE_MAX) {
    keys.sort((a, b) => (states[b].ts || 0) - (states[a].ts || 0))
    keys.slice(FEEDBACK_STATE_MAX).forEach(k => delete states[k])
  }
  setFeedbackStates(states)
}

// 读取反馈状态；过期状态自动清理，避免长期占用存储
function getFeedbackState(qaId) {
  const states = getFeedbackStates()
  const s = states[qaId]
  if (!s) return null
  if (Date.now() - (s.ts || 0) > FEEDBACK_STATE_TTL) {
    delete states[qaId]
    setFeedbackStates(states)
    return null
  }
  return s.rating
}

// 恢复单条消息的已反馈状态（刷新/切换会话重新渲染后调用）
function restoreFeedbackBar(msg) {
  if (!msg.messageId) return
  const rating = getFeedbackState(msg.messageId)
  if (rating == null) return
  msg.feedback.rating = rating
  msg.feedback.locked = true
}

// 满意/不满意反馈提交
async function submitFeedback(mid, qaId, rating) {
  if (!qaId) {
    ElMessage.error('记录尚未就绪，请稍后重试')
    return
  }
  try {
    await api.postJson('/api/v1/chat/feedback/', {
      qa_record_id: qaId,
      rating,
      tags: rating === 1 ? ['good'] : ['bad']
    })
    ElMessage.success(rating === 1 ? '感谢反馈，已记录为满意' : '感谢反馈，将用于优化召回')
    // 持久化反馈状态并锁定按钮：刷新页面后不再允许重复反馈
    saveFeedbackState(qaId, rating)
    const msg = messages.value.find(m => m.mid === mid)
    if (msg) {
      msg.feedback.rating = rating
      msg.feedback.locked = true
    }
  } catch (e) {
    console.error('submit feedback failed:', e)
    ElMessage.error('反馈提交失败')
  }
}

// 详细反馈提交
async function submitDetailedFeedback(msg) {
  const comment = (msg.feedback.detailText || '').trim()
  if (!comment) {
    ElMessage.error('请填写反馈内容')
    return
  }
  try {
    await api.postJson('/api/v1/chat/feedback/', {
      qa_record_id: msg.messageId,
      rating: 0,
      comment,
      tags: ['detailed']
    })
    ElMessage.success('详细反馈已提交，感谢您的建议')
    msg.feedback.detailOpen = false
  } catch (e) {
    ElMessage.error('提交失败')
  }
}

/* ---- 内容审查误判反馈 ----
 * content_filtered 事件命中后，用户可点击"反馈误判"提交申诉，
 * 提交到 /api/v1/chat/feedback/（tags 标记 false_positive） */
function toggleFilterFalsePositiveForm(msg) {
  if (!msg.filtered) return
  // done 事件未到达或后端未落库时 qaId 为空，提示用户稍候
  if (!msg.filtered.qaId) {
    ElMessage.error('记录尚未就绪，请稍后重试')
    return
  }
  msg.filtered.formOpen = !msg.filtered.formOpen
}

async function submitFilterFalsePositive(msg) {
  const f = msg.filtered
  if (!f || !f.qaId) return
  try {
    await api.postJson('/api/v1/chat/feedback/', {
      qa_record_id: f.qaId,
      rating: 0,
      comment: (f.comment || '').trim() || '内容审查误判反馈',
      tags: ['false_positive', 'filter_' + f.category]
    })
    ElMessage.success('反馈已提交，管理员会人工复核')
    f.formOpen = false
    f.submitted = true
  } catch (e) {
    console.error('submit filter false positive failed:', e)
    ElMessage.error('反馈提交失败')
  }
}

/* ==========================================================
   消息撤回与删除（用户消息 + AI 回复成对操作）
   ========================================================== */
/**
 * 从消息列表中移除一对消息（用户消息 + 紧随其后的 AI 回复）
 * @param {Object} userMsg - 用户消息对象（role='user'）
 */
function removeMessagePair(userMsg) {
  const idx = messages.value.findIndex(m => m.mid === userMsg.mid)
  if (idx === -1) return
  // 移除用户消息
  messages.value.splice(idx, 1)
  // 紧随其后的 AI 消息（若存在且 role='ai'）一并移除
  if (idx < messages.value.length && messages.value[idx].role === 'ai') {
    messages.value.splice(idx, 1)
  }
  // 清理会话详情缓存，避免残留已删除消息
  if (currentSessionId.value) {
    removeSessionCache(currentSessionId.value)
  }
}

/**
 * 撤回消息：将问题文本填回输入框，删除消息对（确认弹窗后执行）
 * 适用场景：用户误发或想重新编辑后发送
 */
async function recallMessage(msg) {
  if (isSending.value) {
    ElMessage.warning('正在回答中，请稍后再试')
    return
  }
  const ok = await confirm({ message: '撤回后将删除该消息及 AI 回复，并把问题填回输入框，确定撤回吗？' })
  if (!ok) return
  // 将问题文本填回输入框
  draft.value = msg.content || ''
  // 前端移除消息对
  removeMessagePair(msg)
  // 后端软删除（尽力而为，失败不影响前端体验）
  if (msg.messageId) {
    api.deleteJson('/api/v1/chat/records/' + msg.messageId + '/').catch(() => {})
  }
  ElMessage.success('已撤回，问题已填入输入框')
}

/**
 * 删除消息：仅删除消息对，不保留问题文本（确认弹窗后执行）
 * 适用场景：用户想清理不需要的对话记录
 */
async function deleteMessage(msg) {
  if (isSending.value) {
    ElMessage.warning('正在回答中，请稍后再试')
    return
  }
  const ok = await confirm({ message: '确定删除该消息及 AI 回复吗？此操作不可撤销。', type: 'warning' })
  if (!ok) return
  removeMessagePair(msg)
  if (msg.messageId) {
    api.deleteJson('/api/v1/chat/records/' + msg.messageId + '/').catch(() => {})
  }
  ElMessage.success('已删除')
}

// 格式化耗时展示：有首字耗时则显示"首字 X · 总计 Y"，否则仅显示总计
function formatLatencyText(stats) {
  if (!stats) return ''
  const total = stats.total_ms || stats.latency_total_ms || 0
  const ttfb = stats.ttfb_ms || stats.latency_ttfb_ms || 0
  if (ttfb > 0) return '首字 ' + formatDuration(ttfb) + ' · 总计 ' + formatDuration(total)
  if (total > 0) return '总计 ' + formatDuration(total)
  return ''
}

/* ==========================================================
   会话详情 localStorage 缓存
   ========================================================== */
function getSessionCache(sessionId) {
  try {
    const raw = localStorage.getItem(SESSION_CACHE_PREFIX + sessionId)
    if (!raw) return null
    return JSON.parse(raw)
  } catch (e) { return null }
}

// 写入单个会话缓存（超 50KB 跳过）
function setSessionCache(sessionId, records, lastActiveAt) {
  try {
    const data = { records, last_active_at: lastActiveAt }
    const raw = JSON.stringify(data)
    if (raw.length > MAX_SESSION_CACHE_SIZE) return
    localStorage.setItem(SESSION_CACHE_PREFIX + sessionId, raw)
  } catch (e) { /* localStorage 满或不可用，静默降级 */ }
}

function removeSessionCache(sessionId) {
  try { localStorage.removeItem(SESSION_CACHE_PREFIX + sessionId) } catch (e) { /* ignore */ }
}

// 校验和清理会话缓存：进入页面时调用，纯本地操作不请求后端
function cleanupSessionCache(sessionList) {
  // 收集所有缓存 key
  const cachedIds = []
  for (let i = 0; i < localStorage.length; i++) {
    const key = localStorage.key(i)
    if (key && key.startsWith(SESSION_CACHE_PREFIX)) {
      cachedIds.push(key.slice(SESSION_CACHE_PREFIX.length))
    }
  }
  // 构建会话列表 lookup：id → last_active_at
  const sessionMap = {}
  for (const s of sessionList) {
    sessionMap[String(s.id)] = s.last_active_at || s.created_at
  }
  // 校验每个缓存项：会话不存在 / last_active_at 不一致 → 删除
  const validCaches = []
  for (const id of cachedIds) {
    const cache = getSessionCache(id)
    if (!cache) { removeSessionCache(id); continue }
    if (!(id in sessionMap)) { removeSessionCache(id); continue }
    if (cache.last_active_at !== sessionMap[id]) { removeSessionCache(id); continue }
    validCaches.push({ id, last_active_at: cache.last_active_at })
  }
  // 按 last_active_at 降序排序，只保留最近 MAX_CACHED_SESSIONS 条
  validCaches.sort((a, b) => new Date(b.last_active_at) - new Date(a.last_active_at))
  const keepIds = new Set(validCaches.slice(0, MAX_CACHED_SESSIONS).map(c => c.id))
  for (const c of validCaches) {
    if (!keepIds.has(c.id)) removeSessionCache(c.id)
  }
}

/* ==========================================================
   历史会话
   ========================================================== */
// 历史消息渲染：由记录数组转为消息列表（用户 + AI 配对）
function recordsToMessages(records) {
  if (!records || !records.length) return []
  const list = []
  records.forEach(r => {
    list.push({
      mid: 'u' + r.id, role: 'user',
      content: r.question || '',
      time: formatSessionTime(r.created_at),
      // messageId 用于撤回/删除操作定位后端 QaRecord，与 AI 消息共用同一 ID
      messageId: r.id,
    })
    list.push(buildHistoryAiMessage(r))
  })
  return list
}

// 构建历史 AI 消息（思考区默认折叠、恢复反馈状态、渲染来源区）
function buildHistoryAiMessage(r) {
  const msg = reactive({
    mid: 'm' + r.id, role: 'ai',
    thinking: false, started: true, placeholder: '',
    answerText: r.answer || '', displayText: r.answer || '',
    answerHtml: formatAnswer(r.answer || '', r.citations || []),
    citations: r.citations || [], routeSource: null,
    toolTraces: r.tool_traces || [], answerType: r.answer_type,
    messageId: r.id, ttfbMs: 0, totalMs: 0,
    latencyText: formatLatencyText({ latency_total_ms: r.latency_total_ms, latency_ttfb_ms: r.latency_ttfb_ms }),
    thinkingArea: buildHistoryThinking(r.tool_traces),
    workflow: null,
    sourceCards: [], sourceTags: [],
    feedback: { rating: 0, locked: false, detailOpen: false, detailText: '' },
    filtered: null, error: null,
  })
  // answer_type 用于区分拒答：拒答的历史记录同样不显示来源标识
  const src = buildSourceData(r.citations, undefined, r.tool_traces, undefined, r.answer_type, currentSources.llm)
  msg.sourceCards = src.cards
  msg.sourceTags = src.tags
  restoreFeedbackBar(msg)
  return msg
}

// 历史思考区（工具调用链，默认折叠，不打扰阅读）
function buildHistoryThinking(toolTraces) {
  if (!toolTraces || !toolTraces.length) return null
  return {
    collapsed: true,
    summary: toolTraces.length + ' 次工具调用',
    toolCalls: toolTraces.map((t, idx) => {
      const ok = t.result_ok !== false
      return {
        callId: 'hist_' + idx, name: t.tool_name || 'unknown',
        argsText: formatToolArgs(t.tool_args),
        status: ok ? 'ok' : 'fail',
        statusText: ok ? '成功' : '失败',
        latencyText: t.latency_ms != null ? formatDuration(t.latency_ms) : '',
        resultHtml: formatToolResult(t.tool_result),
        resultCls: ok ? 'ok' : 'fail',
      }
    }),
  }
}

// 统一会话消息加载：缓存命中零请求，未命中显示加载中再请求并写入缓存
async function switchToSession(id, options = {}) {
  const cache = getSessionCache(id)
  if (cache && cache.records) {
    messages.value = recordsToMessages(cache.records)
    nextTick(scrollChatBottom)
    if (!options.skipToast) ElMessage.success('已切换会话')
    return
  }
  loadingRecords.value = true
  messages.value = []
  try {
    const data = await api.getJson('/api/v1/chat/sessions/' + id + '/qa/')
    const records = Array.isArray(data) ? data : (data.records || [])
    messages.value = recordsToMessages(records)
    nextTick(scrollChatBottom)
    const session = sessionCache.value.find(s => s.id == id)
    setSessionCache(id, records, session ? (session.last_active_at || session.created_at) : null)
    if (!options.skipToast) ElMessage.success('已切换会话')
  } catch (e) {
    console.error('load records failed:', e)
    messages.value = []
    if (!options.skipToast) ElMessage.error('加载会话记录失败')
  } finally {
    loadingRecords.value = false
  }
}

// 切换会话：更新标题 + 加载消息
async function switchSession(id) {
  currentSessionId.value = id
  const s = sessionCache.value.find(x => String(x.id) === String(id))
  currentSessionTitle.value = s ? s.title : '新会话'
  await switchToSession(id)
}

async function updateSessionTitle(sessionId, title) {
  try {
    await api.patchJson('/api/v1/chat/sessions/' + sessionId + '/', { title })
  } catch (e) {
    console.error('update session title failed:', e)
  }
}

// 增量更新会话缓存：发送消息后更新预览和时间，移到列表顶部，不请求后端
function updateSessionInCache(sessionId, preview, questionText) {
  const idx = sessionCache.value.findIndex(s => s.id == sessionId)
  if (idx === -1) {
    // 新会话：构造最小 session 对象添加到列表顶部
    const now = new Date().toISOString()
    sessionCache.value.unshift({
      id: sessionId,
      title: questionText ? questionText.slice(0, 32) : '新会话',
      preview: preview || '',
      last_active_at: now,
      created_at: now,
      turn_count: 1,
      is_archived: false,
    })
    return
  }
  const s = sessionCache.value[idx]
  s.preview = preview
  s.last_active_at = new Date().toISOString()
  // 移到列表顶部（最近活跃在前）
  sessionCache.value.splice(idx, 1)
  sessionCache.value.unshift(s)
}

// 会话列表加载：带搜索关键词时附加 search 参数；以列表为准选择最近一条会话
async function initSessionList(skipLoadMessages = false) {
  try {
    let url = '/api/v1/chat/sessions/'
    if (searchKeyword.value) {
      url += '?search=' + encodeURIComponent(searchKeyword.value)
    }
    const data = await api.getJson(url)
    sessionCache.value = data.results || data

    // 以会话列表为准：有则选最近一条，无则留空（发送时后端自动创建）
    currentSessionId.value = sessionCache.value.length > 0 ? sessionCache.value[0].id : null

    // 校验和清理会话详情缓存（纯本地操作，零请求）
    cleanupSessionCache(sessionCache.value)

    if (currentSessionId.value) {
      const active = sessionCache.value.find(s => String(s.id) === String(currentSessionId.value))
      currentSessionTitle.value = active ? active.title : '新会话'
      if (!skipLoadMessages) {
        await switchToSession(currentSessionId.value, { skipToast: true })
      }
    } else {
      currentSessionTitle.value = ''
    }
  } catch (e) {
    console.error('load sessions failed:', e)
  }
}

// 搜索回调（SessionSidebar 防抖后触发）：记录关键词后重载会话列表
function onSessionSearch(keyword) {
  searchKeyword.value = keyword
  initSessionList()
}

// 保存标题（SessionSidebar 编辑失焦后触发）：更新缓存与顶部标题并持久化
async function saveSessionTitle(sessionId, newTitle) {
  try {
    await api.patchJson('/api/v1/chat/sessions/' + sessionId + '/', { title: newTitle })
    const s = sessionCache.value.find(x => String(x.id) === String(sessionId))
    if (s) {
      s.title = newTitle
      if (String(currentSessionId.value) === String(sessionId)) currentSessionTitle.value = newTitle
    }
    ElMessage.success('标题已更新')
  } catch (e) {
    console.error('save title failed:', e)
    ElMessage.error('保存标题失败')
  }
}

// 删除会话：二次确认后执行（删除当前会话时自动选中最近的下一个会话）
function delSession(id) {
  confirm({
    message: '确定删除此会话？删除后不可恢复。',
    title: '删除会话', confirmText: '删除',
  }, () => _doDelSession(id))
}

async function _doDelSession(id) {
  try {
    await api.deleteJson('/api/v1/chat/sessions/' + id + '/')
    // 同步清理内存缓存与本地详情缓存并重新渲染
    sessionCache.value = sessionCache.value.filter(s => s.id != id)
    removeSessionCache(id)
    const deletedCurrent = String(currentSessionId.value) === String(id)
    if (deletedCurrent) {
      // 先选定下一个会话再渲染，确保列表高亮正确（列表按最近活跃降序）
      currentSessionId.value = sessionCache.value.length > 0 ? sessionCache.value[0].id : null
    }
    ElMessage.success('会话已删除')
    // 删除的不是当前会话，当前视图无需变化
    if (!deletedCurrent) return
    if (currentSessionId.value) {
      const s = sessionCache.value.find(x => String(x.id) === String(currentSessionId.value))
      currentSessionTitle.value = s ? s.title : '新会话'
      await switchToSession(currentSessionId.value, { skipToast: true })
    } else {
      // 会话已全部删除，回到空状态
      messages.value = []
      currentSessionTitle.value = ''
    }
  } catch (e) {
    ElMessage.error('删除失败: ' + e.message)
  }
}

// 新建会话：后端创建后切换到空状态
async function newSession() {
  try {
    const data = await api.postJson('/api/v1/chat/sessions/', { title: '新会话' })
    currentSessionId.value = data.id
    messages.value = []
    currentSessionTitle.value = '新会话'
    initSessionList(true)
    ElMessage.success('已创建新会话')
  } catch (e) {
    ElMessage.error('创建会话失败')
  }
}


/* ==========================================================
   生命周期
   ========================================================== */
onMounted(() => {
  userStore.restore()
  initModeSwitcher()
  initDraftRestore()
  initSessionList()
})

onBeforeUnmount(() => {
  // 清理定时器与流式请求，避免页面卸载后继续更新
  if (draftSaveTimer) clearTimeout(draftSaveTimer)
  typingTimers.forEach(timer => clearInterval(timer))
  typingTimers.clear()
  if (currentAbortController.value) currentAbortController.value.abort()
})

// 文档预览弹窗（DocPreviewDialog 组件）：显隐与打开参数
const previewVisible = ref(false)
const previewDocId = ref(null)        // 当前预览文档 ID
const previewInitialPage = ref(1)     // 打开预览定位页（image 为页号）

// 从引用卡片跳转文档预览并定位页码，同时做反馈闭环点击埋点
function previewCitation(docId, page, qaRecordId, chunkIdsStr) {
  if (!docId) return
  // 点击埋点为尽力而为，失败不影响预览主流程（供每日聚合调整关键词权重）
  try {
    const chunkIds = String(chunkIdsStr || '').split(',').map(s => parseInt(s, 10)).filter(n => n > 0)
    const qaId = parseInt(qaRecordId, 10) || null
    for (const cid of chunkIds) {
      api.postJson('/api/v1/analytics/chunk-clicks/', {
        chunk_id: cid, document_id: docId, qa_record_id: qaId,
      }).catch(() => {})
    }
  } catch (e) { /* 忽略埋点异常 */ }
  previewDocId.value = docId
  previewInitialPage.value = page || 1
  previewVisible.value = true
}

// 消息渲染所需的交互回调集合：由 ChatMessageItem 通过 props.handlers 使用
const chatMsgHandlers = {
  workflowStatusText, wfStepIcon, wfNodeStatusText, submitInlineApproval, refreshWorkflowResult,
  toggleFilterFalsePositiveForm, submitFilterFalsePositive, previewCitation, retrySendChat,
  submitFeedback, submitDetailedFeedback, recallMessage, deleteMessage,
}

</script>

<style>
/* ==========================================================
   聊天页样式（由 static/css/chat.css + preview-doc.css 迁移）
   非 scoped：v-html 渲染的 Markdown 内容与 teleport 到 body 的
   el-dialog / el-popover 都需要全局样式生效
   ========================================================== */

/* 设计变量（与原 common.css 保持一致） */
:root {
  --primary: #2563eb;
  --primary-hover: #1d4ed8;
  --primary-light: #eff6ff;
  --bg: var(--app-bg);
  --white: var(--app-card-bg);
  --text: var(--app-text);
  --text-main: var(--app-text);
  --text-sub: var(--app-text-sub);
  --text-placeholder: var(--el-text-color-placeholder);
  --border: var(--app-border);
  --border-strong: var(--app-border);
  --hover: var(--app-menu-hover);
  --danger: #ef4444;
  --info: #0ea5e9;
  --radius-sm: 4px;
  --radius: 6px;
  --radius-lg: 8px;
  --modal-header-height: 48px;
  --modal-footer-height: 48px;
}

/* ============ 页面布局 ============ */
.chat-page {
  display: flex;
  height: 100%;
  min-height: 0;
}

/* 主内容列：顶部工具栏 + 消息区 + 输入区 */
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}

/* 标题栏：与全局 .page-header 统一（--app-header-height 固定高度） */
.chat-header {
  height: var(--app-header-height);
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 0 20px;
  background: var(--white);
  border-bottom: 1px solid var(--border);
}

.chat-title-input {
  flex: 1;
  font-size: 16px;
  font-weight: 600;
  color: var(--text);
  background: transparent;
  padding: 4px 0;
  cursor: default;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

/* 模式切换（el-radio-group 分段按钮微调） */
.mode-switcher .el-radio-button__inner {
  padding: 6px 12px;
  font-size: 12px;
  font-weight: 500;
}

/* 通用 tag */
.tag {
  display: inline-block;
  padding: 2px 8px;
  font-size: 12px;
  border-radius: 4px;
  line-height: 1.6;
  white-space: nowrap;
}

.tag-info {
  background: #ecf5ff;
  color: #409eff;
}

/* ============ 消息区 ============ */
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-height: 0;
}

.empty-state {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
}

.empty-inner {
  text-align: center;
  padding: 60px 20px;
}

.empty-icon {
  font-size: 48px;
  margin-bottom: 16px;
}

.empty-title {
  font-size: 18px;
  font-weight: 600;
  margin-bottom: 8px;
  color: var(--text);
}

.empty-desc {
  color: var(--text-sub);
  font-size: 14px;
}

/* ============ 输入区 ============ */
.chat-input-area {
  background: var(--white);
  border-top: 1px solid var(--border);
  padding: 12px 24px 16px;
  flex-shrink: 0;
}

.chat-input-wrap {
  display: flex;
  gap: 10px;
  /* 发送按钮相对输入框上下居中（而非贴底） */
  align-items: center;
}

.chat-input {
  flex: 1;
}

.chat-input .el-textarea__inner {
  font-size: 14px;
  line-height: 1.5;
  padding: 10px 14px;
}

.chat-input-actions {
  flex-shrink: 0;
}

.chat-send-btn {
  min-width: 72px;
}


@media (max-width: 640px) {
  .source-list {
    grid-template-columns: 1fr;
  }
  .source-card-sql {
    max-height: none;
  }
  .chat-header {
    flex-wrap: wrap;
    padding: 10px 12px;
  }
  .chat-messages {
    padding: 16px 12px;
  }
  .chat-input-area {
    padding: 10px 12px 12px;
  }
}
</style>
