<template>
  <div class="msg" :class="msg.role === 'user' ? 'msg-user' : 'msg-ai'">
            <!-- 用户消息 -->
            <template v-if="msg.role === 'user'">
              <div class="msg-user-content">
                <div class="msg-user-bubble">{{ msg.content }}</div>
              </div>
              <div class="msg-user-side">
                <div class="msg-user-avatar">{{ userInitial }}</div>
                <div class="msg-time">{{ msg.time }}</div>
              </div>
            </template>
            <!-- AI 消息 -->
            <template v-else>
              <div class="msg-ai-avatar">AI</div>
              <div class="msg-ai-content">
                <!-- 思考中占位（start 事件前） -->
                <div v-if="msg.thinking" class="msg-ai-thinking text-sub">
                  <el-icon class="is-loading"><Loading /></el-icon>
                  正在检索知识库并思考中...
                </div>
                <template v-else>
                  <!-- 多 Agent 工作流轨迹区 -->
                  <div v-if="msg.workflow" class="workflow-area">
                    <div class="workflow-header">
                      <span class="workflow-icon">🔄</span>
                      <span class="workflow-title">多 Agent 工作流</span>
                      <span class="workflow-status" :class="msg.workflow.status">{{ workflowStatusText(msg.workflow.status) }}</span>
                    </div>
                    <div class="workflow-body">
                      <div
                        v-for="node in msg.workflow.nodes"
                        :key="node.id"
                        class="wf-node"
                        :class="{ done: ['succeeded', 'failed', 'rejected', 'approved', 'skipped'].includes(node.status) }"
                      >
                        <div class="wf-node-header">
                          <span class="wf-node-icon">{{ wfStepIcon(node.type) }}</span>
                          <span class="wf-node-name">{{ node.name }}</span>
                          <span class="wf-node-status" :class="node.status">{{ wfNodeStatusText(node.status) }}</span>
                          <span v-if="node.latencyMs != null" class="wf-node-latency">{{ (node.latencyMs / 1000).toFixed(2) }}s</span>
                        </div>
                      </div>
                      <!-- HITL 审批确认卡片（内嵌确认/拒绝） -->
                      <div v-if="msg.workflow.approval" class="wf-approval">
                        <div class="wf-approval-title">⚠️ 该步骤需要人工确认</div>
                        <div class="wf-approval-reason">{{ msg.workflow.approval.reason }}</div>
                        <div class="wf-approval-actions">
                          <el-button
                            size="small" type="primary"
                            :loading="msg.workflow.approval.submitting"
                            @click="submitInlineApproval(msg, true)"
                          >✓ 确认执行</el-button>
                          <el-button
                            size="small" type="danger"
                            :loading="msg.workflow.approval.submitting"
                            @click="submitInlineApproval(msg, false)"
                          >✗ 拒绝</el-button>
                        </div>
                      </div>
                      <el-button
                        v-if="msg.workflow.status === 'waiting_approval'"
                        size="small"
                        @click="refreshWorkflowResult(msg)"
                      >🔄 我已审批，刷新结果</el-button>
                    </div>
                  </div>

                  <!-- 思考过程区（Agent 工具调用链，可折叠） -->
                  <div v-if="msg.thinkingArea" class="ai-thinking-area">
                    <div class="thinking-area" :class="{ collapsed: msg.thinkingArea.collapsed }">
                      <div class="thinking-header" @click="msg.thinkingArea.collapsed = !msg.thinkingArea.collapsed">
                        <span class="thinking-icon">🤔</span>
                        <span class="thinking-title">思考过程</span>
                        <span class="thinking-summary">{{ msg.thinkingArea.summary }}</span>
                        <span class="thinking-toggle">▾</span>
                      </div>
                      <div class="thinking-body">
                        <div v-for="tc in msg.thinkingArea.toolCalls" :key="tc.callId" class="tool-call">
                          <div class="tool-call-header">
                            <span class="tool-call-icon">🔧</span>
                            <span class="tool-call-name">{{ tc.name }}</span>
                            <span class="tool-call-status" :class="tc.status">{{ tc.statusText }}</span>
                            <span v-if="tc.latencyText" class="tool-call-latency">{{ tc.latencyText }}</span>
                          </div>
                          <div v-if="tc.argsText" class="tool-call-args">{{ tc.argsText }}</div>
                          <div v-if="tc.resultHtml" class="tool-call-result" :class="tc.resultCls" v-html="tc.resultHtml"></div>
                        </div>
                      </div>
                    </div>
                  </div>

                  <!-- 回答文本（占位文本 → 打字机渲染） -->
                  <div class="ai-answer-text">
                    <div v-if="msg.placeholder" v-html="msg.placeholder"></div>
                    <div v-else v-html="msg.answerHtml"></div>
                  </div>

                  <!-- 内容审查拦截卡片 -->
                  <div v-if="msg.filtered" class="content-filtered-card">
                    <div class="filtered-icon">🚫</div>
                    <div class="filtered-body">
                      <div class="filtered-title">{{ msg.filtered.reason }}</div>
                      <div class="filtered-hint">本回答因包含违规内容被系统拦截。如果您认为这是误判，请点击反馈，管理员会人工复核。</div>
                    </div>
                    <el-button
                      size="small"
                      class="filtered-feedback-btn"
                      :disabled="!msg.filtered.qaId || msg.filtered.submitted"
                      @click="toggleFilterFalsePositiveForm(msg)"
                    >{{ msg.filtered.submitted ? '✓ 已反馈' : '💬 反馈误判' }}</el-button>
                    <div v-if="msg.filtered.formOpen" class="filtered-feedback-form">
                      <el-input
                        v-model="msg.filtered.comment"
                        type="textarea"
                        :rows="3"
                        placeholder="请简要说明为什么认为是误判（可选）..."
                      />
                      <div class="filtered-feedback-actions">
                        <el-button size="small" @click="msg.filtered.formOpen = false">取消</el-button>
                        <el-button size="small" type="primary" @click="submitFilterFalsePositive(msg)">提交反馈</el-button>
                      </div>
                    </div>
                  </div>

                  <!-- 溯源来源区（标签行 + 详细卡片） -->
                  <div v-if="msg.started && (msg.sourceTags.length || msg.sourceCards.length)" class="ai-source-area">
                    <div v-if="msg.sourceTags.length" class="answer-source-tags">
                      <span class="source-tags-title">数据来源</span>
                      <span v-for="(t, i) in msg.sourceTags" :key="i" class="source-tag" :class="t.cls">{{ t.text }}</span>
                    </div>
                    <div v-if="msg.sourceCards.length" class="source-block">
                      <div class="source-header">📎 溯源来源 · {{ msg.sourceCards.length }} 项</div>
                      <div class="source-list">
                        <div v-for="(c, i) in msg.sourceCards" :key="i" class="source-card">
                          <div class="source-card-head">
                            <span class="source-card-badge" :class="'badge-' + c.type">{{ c.badge }}</span>
                            <div
                              class="source-card-title"
                              :class="{ 'source-card-title-link': c.clickable }"
                              :title="c.clickable ? '点击预览文档' : ''"
                              @click="c.clickable && previewCitation(c.docId, c.page, msg.messageId || 0, c.chunkIds.join(','))"
                            >{{ c.title }}</div>
                          </div>
                          <div class="source-card-meta">
                            <span v-for="(m, j) in c.meta" :key="j" class="source-card-section">{{ m.text }}</span>
                            <span v-if="c.type === 'db' && c.sql" class="source-card-sql-toggle" @click="c.sqlOpen = !c.sqlOpen">{{ c.sqlOpen ? '收起 SQL ▴' : '查看 SQL ▾' }}</span>
                          </div>
                          <div v-if="c.type === 'db' && c.sql && c.sqlOpen" class="source-card-sql">{{ c.sql }}</div>
                        </div>
                      </div>
                    </div>
                  </div>

                  <!-- 错误提示（发送失败/超时，可重试） -->
                  <div v-if="msg.error" class="error-box">
                    <div class="error-text" style="color:#f56c6c">{{ msg.error.text }}</div>
                    <div v-if="msg.error.hint" class="error-hint">{{ msg.error.hint }}</div>
                    <el-button size="small" type="primary" @click="retrySendChat(msg)">🔄 重试发送</el-button>
                  </div>

                  <!-- 反馈条（start 事件后显示） -->
                  <div v-if="msg.started" class="feedback-bar">
                    <el-button
                      size="small"
                      class="feedback-btn"
                      :class="{ active: msg.feedback.rating === 1 }"
                      :disabled="msg.feedback.locked"
                      @click="submitFeedback(msg.mid, msg.messageId, 1)"
                    >👍 满意</el-button>
                    <el-button
                      size="small"
                      class="feedback-btn"
                      :class="{ 'active-neg': msg.feedback.rating === -1 }"
                      :disabled="msg.feedback.locked"
                      @click="submitFeedback(msg.mid, msg.messageId, -1)"
                    >👎 不满意</el-button>
                    <el-button
                      size="small"
                      class="feedback-btn"
                      :disabled="msg.feedback.locked"
                      @click="msg.feedback.detailOpen = !msg.feedback.detailOpen"
                    >💬 详细反馈</el-button>
                    <span style="flex:1"></span>
                    <span class="feedback-latency">{{ msg.latencyText }}</span>
                  </div>
                  <!-- 详细反馈表单 -->
                  <div v-if="msg.started && msg.feedback.detailOpen" class="feedback-detail">
                    <el-input v-model="msg.feedback.detailText" type="textarea" :rows="3" placeholder="请描述具体的问题或建议..." />
                    <div style="text-align:right;margin-top:6px">
                      <el-button size="small" @click="msg.feedback.detailOpen = false">取消</el-button>
                      <el-button size="small" type="primary" @click="submitDetailedFeedback(msg)">提交反馈</el-button>
                    </div>
                  </div>
                </template>
              </div>
            </template>
  </div>
</template>

<script setup>
// 单条消息渲染组件（从 Chat.vue 拆分）：用户消息 / AI 消息（workflow、思考过程、
// 溯源来源、反馈、错误、内容拦截等区块）
// 说明：回答文本/工具结果/语法高亮均为 v-html 注入，依赖全局样式，
//       因此本组件 <style> 不加 scoped，类名与原 Chat.vue 完全一致，无冲突。
const props = defineProps({
  msg: { type: Object, required: true },
  userInitial: { type: String, default: '' },
  handlers: { type: Object, required: true },
})

// 模板中用到的交互回调均来自父组件注入的 handlers（保持消息操作逻辑收敛在 Chat.vue）
const {
  workflowStatusText, wfStepIcon, wfNodeStatusText, submitInlineApproval, refreshWorkflowResult,
  toggleFilterFalsePositiveForm, submitFilterFalsePositive, previewCitation, retrySendChat,
  submitFeedback, submitDetailedFeedback,
} = props.handlers
</script>

<style>
/* 消息气泡 */
.msg {
  display: flex;
  gap: 12px;
}

.msg-user {
  justify-content: flex-end;
  align-items: flex-start;
}

.msg-user-content {
  max-width: 70%;
  margin-left: auto;
}

.msg-user-side {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.msg-user-avatar {
  width: 36px;
  height: 36px;
  border-radius: var(--radius);
  flex-shrink: 0;
  background: linear-gradient(135deg, #3b82f6 0%, #1d4ed8 100%);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
}

.msg-user-bubble {
  padding: 10px 16px;
  background: var(--primary);
  color: #fff;
  border-radius: var(--radius-lg) var(--radius-sm) var(--radius-lg) var(--radius-lg);
  font-size: 14px;
  line-height: 1.6;
  max-width: 100%;
  word-break: break-word;
}

.msg-time {
  font-size: 11px;
  color: var(--text-sub);
  text-align: center;
}

.msg-ai-avatar {
  width: 36px;
  height: 36px;
  border-radius: var(--radius);
  flex-shrink: 0;
  background: linear-gradient(135deg, #6366f1 0%, #8b5cf6 100%);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  font-weight: 700;
}

.msg-ai-content {
  max-width: 80%;
  background: var(--white);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px;
  font-size: 14px;
  line-height: 1.7;
  color: var(--text);
  min-width: 0;
}

.msg-ai-thinking {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.text-sub {
  color: var(--text-sub);
}

/* 回答正文 */
.ai-answer-text {
  line-height: 1.7;
  word-break: break-word;
}

.ai-answer-text p {
  margin-bottom: 8px;
}

.ai-answer-text ul,
.ai-answer-text ol {
  padding-left: 20px;
  margin-bottom: 8px;
}

.ai-answer-text li {
  margin-bottom: 4px;
}

.ai-answer-text pre {
  background: #1f2937;
  color: #e5e7eb;
  padding: 12px 16px;
  border-radius: var(--radius);
  font-family: 'Courier New', monospace;
  font-size: 13px;
  overflow-x: auto;
  margin-bottom: 8px;
}

.ai-answer-text code {
  font-family: 'Courier New', monospace;
  font-size: 13px;
  background: var(--hover);
  padding: 1px 4px;
  border-radius: 3px;
}

.ai-answer-text pre code {
  background: none;
  padding: 0;
}

.ai-answer-text h3,
.ai-answer-text h4,
.ai-answer-text h5 {
  margin: 12px 0 6px;
  font-weight: 600;
  color: var(--text);
}

/* ============ 回答正文 / 工具结果的 Markdown 美化 ============ */
.md-table-wrap {
  overflow-x: auto;
  margin: 8px 0;
  border: 1px solid var(--border);
  border-radius: 8px;
}

.md-table-wrap table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.md-table-wrap th,
.md-table-wrap td {
  padding: 6px 10px;
  border: 1px solid var(--border);
  text-align: left;
  white-space: nowrap;
}

.md-table-wrap thead th {
  background: var(--hover);
  font-weight: 600;
}

.md-table-wrap tbody tr:hover td {
  background: var(--hover);
}

.ai-answer-text blockquote,
.tool-call-result blockquote {
  margin: 8px 0;
  padding: 4px 12px;
  border-left: 3px solid var(--primary);
  background: var(--hover);
  color: var(--text-sub);
  border-radius: 4px;
}

.ai-answer-text hr,
.tool-call-result hr {
  border: none;
  border-top: 1px solid var(--border);
  margin: 12px 0;
}

.ai-answer-text strong,
.tool-call-result strong {
  font-weight: 600;
}

.ai-answer-text a,
.tool-call-result a {
  color: var(--primary);
  text-decoration: underline;
}

/* 引用序号上标 */
.cite-ref {
  font-size: .72em;
  color: var(--primary);
  font-weight: 600;
  margin: 0 1px;
}

/* ============ 多 Agent 工作流轨迹区 ============ */
.workflow-area {
  border: 1px solid var(--border);
  border-radius: var(--radius);
  margin: 4px 0 10px;
  background: var(--app-card-bg);
  overflow: hidden;
}

.workflow-area:empty {
  display: none;
}

.workflow-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  background: var(--app-menu-hover);
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}

.workflow-icon {
  font-size: 14px;
}

.workflow-title {
  font-weight: 600;
  color: var(--app-text);
}

.workflow-status {
  margin-left: auto;
  font-size: 12px;
  color: var(--text-sub);
}

.workflow-status.waiting_approval,
.workflow-status.running {
  color: #b45309;
}

.workflow-status.succeeded {
  color: #047857;
}

.workflow-status.failed {
  color: var(--danger);
}

.workflow-status.degraded {
  color: #b45309;
}

.workflow-body {
  padding: 8px 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.wf-node {
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 6px 10px;
  font-size: 12px;
  background: var(--app-card-bg);
}

.wf-node-header {
  display: flex;
  align-items: center;
  gap: 6px;
}

.wf-node-icon {
  font-size: 13px;
  flex-shrink: 0;
}

.wf-node-name {
  font-weight: 500;
  color: var(--app-text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wf-node-status {
  font-size: 11px;
  flex-shrink: 0;
}

.wf-node-status.running {
  color: #b45309;
}

.wf-node-status.running::before {
  content: '⏳ ';
}

.wf-node-status.succeeded {
  color: #047857;
}

.wf-node-status.succeeded::before {
  content: '✅ ';
}

.wf-node-status.failed {
  color: var(--danger);
}

.wf-node-status.failed::before {
  content: '❌ ';
}

.wf-node-status.blocked,
.wf-node-status.pending {
  color: #b45309;
}

.wf-node-status.blocked::before {
  content: '⏸ ';
}

.wf-node-status.approved::before {
  content: '✅ ';
  color: #047857;
}

.wf-node-status.rejected {
  color: var(--danger);
}

.wf-node-status.rejected::before {
  content: '⛔ ';
}

.wf-node-status.skipped {
  color: var(--text-sub);
}

.wf-node-status.skipped::before {
  content: '⏭ ';
}

.wf-node-latency {
  font-size: 11px;
  color: var(--text-sub);
  flex-shrink: 0;
}

/* 审批确认卡片（HITL） */
.wf-approval {
  margin-top: 6px;
  padding: 10px 12px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-left: 3px solid #f59e0b;
  border-radius: var(--radius-sm);
}

.wf-approval-title {
  font-size: 13px;
  font-weight: 600;
  color: #92400e;
}

.wf-approval-reason {
  margin-top: 4px;
  font-size: 12px;
  color: #78350f;
  line-height: 1.6;
}

.wf-approval-actions {
  margin-top: 8px;
  display: flex;
  gap: 8px;
  align-items: center;
}

.wf-approval-actions .el-button + .el-button {
  margin-left: 0;
}

/* ============ Agent 思考过程区（可折叠） ============ */
.ai-thinking-area {
  margin-bottom: 10px;
}

.ai-thinking-area:empty {
  display: none;
}

.thinking-area {
  background: linear-gradient(135deg, var(--app-card-bg) 0%, var(--app-menu-hover) 100%);
  border: 1px solid #e0e7ff;
  border-radius: var(--radius);
  overflow: hidden;
}

.thinking-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 12px;
  cursor: pointer;
  user-select: none;
  font-size: 12px;
  color: var(--text-sub);
  transition: background 0.1s;
}

.thinking-header:hover {
  background: rgba(99, 102, 241, 0.05);
}

.thinking-icon {
  font-size: 13px;
}

.thinking-title {
  font-weight: 600;
  color: var(--text);
}

.thinking-summary {
  flex: 1;
  color: var(--text-sub);
  font-size: 11px;
}

.thinking-toggle {
  transition: transform 0.2s;
  font-size: 10px;
  color: var(--text-sub);
}

.thinking-area.collapsed .thinking-toggle {
  transform: rotate(-90deg);
}

.thinking-body {
  padding: 8px 12px 10px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  border-top: 1px solid #e0e7ff;
}

.thinking-area.collapsed .thinking-body {
  display: none;
}

/* 单次工具调用卡片 */
.tool-call {
  background: var(--white);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  overflow: hidden;
}

.tool-call-header {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  font-size: 12px;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
}

.tool-call-icon {
  font-size: 12px;
}

.tool-call-name {
  font-weight: 600;
  color: var(--text);
  font-family: 'Courier New', monospace;
}

.tool-call-status {
  font-size: 11px;
  color: var(--text-sub);
  margin-left: auto;
}

.tool-call-status.running {
  color: #d97706;
}

.tool-call-status.running::before {
  content: '●';
  margin-right: 3px;
  animation: chat-pulse 1.2s ease-in-out infinite;
}

.tool-call-status.ok {
  color: #059669;
}

.tool-call-status.fail {
  color: var(--danger);
}

@keyframes chat-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.4; }
}

.tool-call-latency {
  font-size: 10px;
  color: var(--text-sub);
}

.tool-call-args,
.tool-call-result {
  padding: 6px 10px;
  font-size: 11px;
  font-family: 'Courier New', monospace;
  color: var(--text-sub);
  white-space: pre-wrap;
  word-break: break-all;
  line-height: 1.5;
}

.tool-call-args {
  border-bottom: 1px dashed var(--border);
}

.tool-call-result:empty {
  display: none;
}

.tool-call-result.ok {
  color: #047857;
}

.tool-call-result.fail {
  color: var(--danger);
  background: #fef2f2;
}

/* ============ 溯源来源区 ============ */
.ai-source-area {
  margin-top: 10px;
}

.source-block {
  margin: 12px 0 8px;
}

.source-header {
  font-size: 12px;
  color: var(--text-sub);
  margin-bottom: 6px;
  font-weight: 500;
}

.source-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 6px;
}

.source-card {
  padding: 8px 10px;
  background: var(--bg);
  border-radius: var(--radius-sm);
  font-size: 12px;
  border: 1px solid var(--border);
  min-width: 0;
}

.source-card-head {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.source-card-badge {
  flex-shrink: 0;
  font-size: 10px;
  line-height: 1;
  padding: 3px 6px;
  border-radius: 999px;
  font-weight: 600;
  color: #fff;
  white-space: nowrap;
}

.badge-doc { background: var(--primary); }
.badge-db { background: #7c5cfc; }
.badge-web { background: var(--info); }

.source-card-title {
  flex: 1;
  min-width: 0;
  color: var(--text);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.source-card-title-link {
  cursor: pointer;
  color: var(--primary);
}

.source-card-title-link:hover {
  text-decoration: underline;
}

.source-card-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px 8px;
  margin-top: 4px;
}

.source-card-section {
  font-size: 11px;
  color: var(--text-sub);
  font-weight: 400;
}

.source-card-sql-toggle {
  cursor: pointer;
  color: var(--primary);
  font-size: 11px;
  font-weight: 400;
  user-select: none;
}

.source-card-sql {
  margin-top: 6px;
  padding: 6px 8px;
  background: rgba(0, 0, 0, 0.04);
  border-radius: var(--radius-sm);
  font-family: monospace;
  font-size: 11px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-sub);
  max-height: 160px;
  overflow-y: auto;
}

/* 来源标签行 */
.answer-source-tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 8px 0 10px;
  border-bottom: 1px dashed var(--border);
  margin-bottom: 8px;
}

.source-tags-title {
  font-size: 11px;
  color: var(--text-sub);
  margin-right: 2px;
  flex-shrink: 0;
}

.source-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.5;
  border: 1px solid transparent;
}

.tag-doc {
  color: #0b57d0;
  background: #e8f0fe;
  border-color: #c6dafc;
}

.tag-db {
  color: #188038;
  background: #e6f4ea;
  border-color: #b7e1cd;
}

.tag-web {
  color: #a50e0e;
  background: #fce8e6;
  border-color: #f5c6c2;
}

.tag-llm {
  color: #7b1fa2;
  background: #f3e8fd;
  border-color: #e1c7f2;
}

/* ============ 内容审查拦截卡片 ============ */
.content-filtered-card {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 14px 16px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-left: 3px solid var(--danger);
  border-radius: var(--radius);
  margin: 4px 0 8px;
  flex-wrap: wrap;
}

.content-filtered-card .filtered-icon {
  font-size: 20px;
  line-height: 1.4;
  flex-shrink: 0;
}

.content-filtered-card .filtered-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.content-filtered-card .filtered-title {
  font-size: 14px;
  font-weight: 600;
  color: #b91c1c;
}

.content-filtered-card .filtered-hint {
  font-size: 12px;
  color: #991b1b;
  line-height: 1.6;
  opacity: 0.85;
}

.content-filtered-card .filtered-feedback-btn {
  flex-shrink: 0;
  align-self: center;
}

.content-filtered-card .filtered-feedback-form {
  flex-basis: 100%;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #fecaca;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.content-filtered-card .filtered-feedback-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

/* ============ 反馈 ============ */
.feedback-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid var(--border);
}

.feedback-bar .el-button + .el-button {
  margin-left: 0;
}

.feedback-btn {
  border-radius: var(--radius-sm);
  font-size: 12px;
}

.feedback-btn.active {
  background: var(--primary-light);
  color: var(--primary);
  border-color: var(--primary);
}

.feedback-btn.active-neg {
  background: #fef2f2;
  color: var(--danger);
  border-color: var(--danger);
}

.feedback-latency {
  font-size: 11px;
  color: var(--text-sub);
  white-space: nowrap;
}

.feedback-detail {
  margin-top: 8px;
  background: var(--bg);
  border-radius: var(--radius);
  padding: 10px;
}

.feedback-detail .el-button + .el-button {
  margin-left: 8px;
}

/* ============ 错误提示 ============ */
.error-box {
  padding: 6px 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
}

.error-text {
  font-size: 14px;
}

.error-hint {
  font-size: 12px;
  color: var(--text-sub);
}
</style>
