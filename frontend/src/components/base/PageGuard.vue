<template>
  <div v-if="!allowed" class="page-guard">
    <el-empty :description="message" :image-size="80" />
  </div>
  <slot v-else />
</template>

<script setup>
// 页面级权限守卫：无权限时展示统一空态（浅色卡片 + 居中），有权限时渲染默认插槽内容
// 业务背景：多个管理页顶部重复 "无权限 el-empty + v-else 内容" 结构且形态不一致
// （部分页面裸 el-empty、部分套 .denied-card 卡片），统一收敛为卡片居中形态
defineProps({
  allowed: { type: Boolean, default: false },
  message: { type: String, default: '无权限访问此页面' },
})
</script>

<style>
/* 无权限占位：浅色卡片 + 内容居中（由原各页 .denied-card 样式收敛而来） */
.page-guard {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 320px;
}
</style>
