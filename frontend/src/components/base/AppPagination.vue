<template>
  <!-- 分页条统一组件：收敛各列表页重复的
       v-if="total>0" + background + layout + total/page-size/current-page + current-change 六件套。
       样式通过 class 透传落在 el-pagination 根节点（父组件 scoped 样式仍可命中根节点），
       需要"每页条数切换"时传 layout 含 sizes 并配合 pageSizes / size-change -->
  <el-pagination
    v-if="total > 0"
    background
    :layout="layout"
    :total="total"
    :page-size="pageSize"
    :current-page="page"
    :page-sizes="pageSizes"
    @current-change="emit('page-change', $event)"
    @size-change="emit('size-change', $event)"
  />
</template>

<script setup>
defineProps({
  total: { type: Number, default: 0 },       // 总条数（<=0 时不渲染分页条）
  page: { type: Number, default: 1 },        // 当前页
  pageSize: { type: Number, default: 20 },   // 每页条数
  layout: { type: String, default: 'total, prev, pager, next' },
  pageSizes: { type: Array, default: () => [20, 50, 100] }, // 每页条数候选（layout 含 sizes 时生效）
})
const emit = defineEmits(['page-change', 'size-change'])
</script>
