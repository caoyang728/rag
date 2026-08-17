<template>
  <aside class="chat-sessions">
    <div class="panel-header">
      <div class="session-search-wrap">
        <el-input
          v-model="searchKeyword"
          size="small"
          placeholder="搜索会话"
          class="session-search-input"
          clearable
          @input="debounceSearch"
          @keydown.enter="searchSessions"
        />
      </div>
    </div>
    <el-scrollbar class="panel-body">
      <div v-if="!sessions.length" class="session-empty">暂无会话</div>
      <template v-for="group in groupedSessions" :key="group.name">
        <div class="session-group-title">{{ group.name }}</div>
        <div
          v-for="s in group.items"
          :key="s.id"
          class="session-item"
          :class="{ active: String(s.id) === String(currentId) }"
          @click="onSwitch(s.id)"
        >
          <div class="session-content">
            <template v-if="editingSessionId === s.id">
              <el-input
                v-model="editingTitle"
                size="small"
                ref="titleInputRef"
                @blur="onEditBlur(s.id)"
                @keydown.enter.prevent="$event.target.blur()"
              />
            </template>
            <template v-else>
              <div class="session-title">{{ s.title }}</div>
              <div class="session-preview">{{ s.preview || '' }}</div>
            </template>
            <div class="session-time">{{ formatSessionTime(s.last_active_at || s.created_at) }}</div>
          </div>
          <div class="session-actions">
            <el-button
              size="small" circle text
              class="session-icon-btn"
              title="编辑标题"
              @click.stop="editSessionTitle(s.id, s)"
            >
              <el-icon><Edit /></el-icon>
            </el-button>
            <el-button
              size="small" circle text
              class="session-icon-btn icon-del"
              title="删除会话"
              @click.stop="onDelete(s.id)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
      </template>
    </el-scrollbar>
  </aside>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref } from 'vue'
import { Delete, Edit } from '@element-plus/icons-vue'
import { debounce } from '../../utils/debounce'
import { formatSessionTime } from '../../utils/format'

// 会话侧边栏（自 Chat.vue 抽出）：负责历史会话列表的搜索/分组/编辑/删除交互，
// 列表数据与选中会话由父组件通过 props 传入，变更通过 emits 通知父组件处理
const props = defineProps({
  sessions: { type: Array, default: () => [] },
  currentId: { type: [String, Number], default: null },
})
const emit = defineEmits(['switch', 'delete', 'save-title', 'search'])

const searchKeyword = ref('')
const editingSessionId = ref(null)
const editingTitle = ref('')
const titleInputRef = ref(null)

// 会话列表按日期分组：今天 / 昨天 / 本周 / 更早
const groupedSessions = computed(() => {
  const grouped = {}
  const order = []
  for (const s of props.sessions) {
    const date = new Date(s.last_active_at || s.created_at)
    const now = new Date()
    const diff = now.getTime() - date.getTime()
    let group = '更早'
    if (diff < 24 * 60 * 60 * 1000) group = '今天'
    else if (diff < 48 * 60 * 60 * 1000) group = '昨天'
    else if (diff < 7 * 24 * 60 * 60 * 1000) group = '本周'
    if (!grouped[group]) { grouped[group] = []; order.push(group) }
    grouped[group].push(s)
  }
  // 保持 今天→昨天→本周→更早 的展示顺序
  return order.map(name => ({ name, items: grouped[name] }))
})

// 搜索防抖（300ms，定时器由 utils/debounce 统一管理）
const debounceSearch = debounce(searchSessions, 300)
function searchSessions() {
  emit('search', searchKeyword.value)
}

// 编辑标题：切换为行内输入框并聚焦
function editSessionTitle(id, s) {
  editingSessionId.value = id
  editingTitle.value = s.title || ''
  nextTick(() => {
    // v-for 内的 ref 会收集为数组，同一时刻只有一个编辑框，取第一个聚焦
    const el = Array.isArray(titleInputRef.value) ? titleInputRef.value[0] : titleInputRef.value
    if (el) el.focus()
  })
}

// 失焦/回车保存标题：空值回退不保存，非空交由父组件持久化
function onEditBlur(sessionId) {
  editingSessionId.value = null
  const newTitle = (editingTitle.value || '').trim()
  if (!newTitle) {
    editingTitle.value = ''
    return
  }
  emit('save-title', sessionId, newTitle)
}

function onSwitch(id) {
  emit('switch', id)
}

function onDelete(id) {
  emit('delete', id)
}

onBeforeUnmount(() => {
  debounceSearch.cancel()
})
</script>

<style scoped>
/* ============ 历史会话右栏 ============ */
.chat-sessions {
  width: 260px;
  flex-shrink: 0;
  background: var(--white);
  border-left: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.chat-sessions .panel-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 已删除「历史会话」标题，搜索框占满面板宽度 */
.chat-sessions .session-search-wrap {
  flex: 1;
  min-width: 0;
}

/* 历史会话搜索框：高度与面板标题行一致（与按钮/输入控件统一），上下居中 */
.chat-sessions .session-search-input .el-input__wrapper {
  height: 32px;
  padding: 0 10px;
  border-radius: 6px;
}

.chat-sessions .session-search-input .el-input__inner {
  font-size: 12px;
}

.chat-sessions .panel-body {
  flex: 1;
  overflow-y: auto;
  padding: 4px 0;
  min-height: 0;
}

.chat-sessions .session-group-title {
  padding: 8px 16px 4px;
  font-size: 11px;
  color: var(--text-sub);
  letter-spacing: 0.03em;
}

.chat-sessions .session-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 10px 10px 12px;
  cursor: pointer;
  border-left: 3px solid transparent;
  transition: all 0.1s;
}

.chat-sessions .session-item:hover {
  background: var(--hover);
}

.chat-sessions .session-item.active {
  background: var(--primary-light);
  border-left-color: var(--primary);
}

/* 暗色主题下选中会话：--primary-light 是浅色常量，需用半透明蓝适配暗底 */
html.dark .chat-sessions .session-item.active {
  background: rgba(37, 99, 235, 0.18);
}

html.dark .chat-sessions .session-item.active .session-title {
  color: #93c5fd;
}

.chat-sessions .session-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.chat-sessions .session-title {
  font-size: 13px;
  font-weight: 500;
  color: var(--text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-sessions .session-preview {
  font-size: 11px;
  color: var(--text-sub);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.chat-sessions .session-time {
  font-size: 10px;
  color: var(--text-placeholder);
}

.chat-sessions .session-actions {
  display: flex;
  flex-direction: row;
  gap: 4px;
  padding-top: 1px;
  opacity: 0;
  transition: opacity 0.15s;
  flex-shrink: 0;
}

.chat-sessions .session-item:hover .session-actions {
  opacity: 1;
}

.chat-sessions .session-icon-btn {
  width: 20px;
  height: 20px;
}

.chat-sessions .session-icon-btn.icon-del:hover {
  background: #fef2f2;
  color: var(--danger);
}

.session-empty {
  padding: 24px 16px;
  text-align: center;
  font-size: 12px;
  color: var(--text-sub);
}

/* 窄屏隐藏历史会话右栏（与 Chat.vue 页面级响应式保持一致） */
@media (max-width: 640px) {
  .chat-sessions {
    display: none;
  }
}
</style>
