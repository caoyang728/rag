<template>
  <div class="panel-header" :class="{ 'panel-header-wrap': wrap, 'panel-header-plain': plain }">
    <span class="panel-header-title" :class="titleClass"><slot /></span>
    <div v-if="$slots.actions" class="panel-header-actions">
      <slot name="actions" />
    </div>
  </div>
</template>

<script setup>
/**
 * 面板头部统一组件：收敛各管理页反复书写的
 * `<div class="panel-head"><span class="card-title">标题</span> <el-button>操作</el-button></div>`
 * 同构结构（标题在左、操作按钮/筛选器在右）。
 * 默认形态为"带底边框的卡片头部"（padding 14px 16px + border-bottom）；
 * 需要换行（筛选器较多）时传 wrap；不需要边框与内边距（仅底部留白）时传 plain。
 */
defineProps({
  /** 标题额外 class：默认 card-title（15px 全局样式）；节点树等小字号场景传 panel-title（14px） */
  titleClass: { type: String, default: 'card-title' },
  /** 空间不足时换行（右侧含多个筛选器/按钮时使用） */
  wrap: { type: Boolean, default: false },
  /** 无边框无内边距，仅底部留白 12px（如上传面板头部） */
  plain: { type: Boolean, default: false },
})
</script>

<style scoped>
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 16px;
  border-bottom: 1px solid var(--app-border);
  flex-shrink: 0;
}

.panel-header-wrap {
  flex-wrap: wrap;
}

.panel-header-plain {
  padding: 0;
  border-bottom: none;
  margin-bottom: 12px;
}

.panel-header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

/* 节点树面板等使用的 14px 小字号标题变体 */
.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text);
}
</style>
