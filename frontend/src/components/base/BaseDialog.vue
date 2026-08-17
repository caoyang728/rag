<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    :width="width"
    :top="top"
    align-center
    :destroy-on-close="destroyOnClose"
    :close-on-click-modal="closeOnClickModal"
    :modal-class="maskClass"
    :z-index="zIndex"
    class="base-dialog"
    :style="dialogStyle"
    @update:model-value="emit('update:modelValue', $event)"
    @open="emit('open')"
    @closed="emit('closed')"
  >
    <template v-if="$slots.header" #header>
      <slot name="header" />
    </template>
    <div class="base-dialog-body">
      <slot />
    </div>
    <template v-if="$slots.footer" #footer>
      <slot name="footer" />
    </template>
  </el-dialog>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },               // v-model 控制弹窗显隐
  title: { type: String, default: '' },                         // 弹窗标题（提供 #header slot 时优先使用 slot）
  width: { type: [String, Number], default: '80%' },            // 弹窗宽度（相对全屏 overlay-dialog，80%≈80vw）
  height: { type: [String, Number], default: '80vh' },          // 弹窗高度（body 区域内部滚动，页面按需传入）
  minWidth: { type: [String, Number], default: '960px' },       // 小屏兜底最小宽
  minHeight: { type: [String, Number], default: '560px' },      // 小屏兜底最小高
  top: { type: String, default: '0' },                          // 顶部偏移（align-center 已居中，保持 0 避免偏移）
  destroyOnClose: { type: Boolean, default: true },
  // 点击遮罩是否关闭：默认 false，防止误点遮罩丢失表单内容；需要时页面可显式传 true
  closeOnClickModal: { type: Boolean, default: false },
  // 遮罩自定义类名：用于轻微模糊背景等遮罩样式（默认 base-dialog-mask）
  maskClass: { type: String, default: 'base-dialog-mask' },
  // 弹窗层级：默认不传（跟随 Element Plus 自动递增）；需要盖在其他弹窗之上时显式传入
  zIndex: { type: Number, default: null },
})
const emit = defineEmits(['update:modelValue', 'open', 'closed'])

// 高度/最小尺寸通过 CSS 变量下发到 .base-dialog（width 走 el-dialog 的 width prop 内联生效）
const toCss = v => (typeof v === 'number' ? v + 'px' : v)
const dialogStyle = computed(() => ({
  '--bd-height': toCss(props.height),
  '--bd-min-width': toCss(props.minWidth),
  '--bd-min-height': toCss(props.minHeight),
}))
</script>

<style>
/* ============ BaseDialog 弹窗骨架 ============
   统一"屏幕内尺寸约束 + header/footer 固定"布局：
   - 默认宽高各取视口 80%（含最小尺寸兜底），align-center + top=0 屏幕居中；
   - body 只提供占位容器（.base-dialog-body 撑满剩余高度），
     内部是什么格式（固定高度块 / 内部滚动区等）由页面自行控制；
   - modal 默认开启且不传 modal-penetrable → 遮罩不穿透，弹窗外的页面不可交互；
   - close-on-click-modal 默认 false → 点击遮罩不会关闭弹窗（需走弹窗内按钮/关闭图标）；
   - 遮罩带轻微模糊背景（backdrop-filter，仅模糊弹窗背后的页面内容，不影响弹窗自身）；
   - 非 scoped：el-dialog teleport 到 body，样式必须全局生效（类名前缀 base-dialog 避免污染其他弹窗） */
.base-dialog {
  height: var(--bd-height, 80vh);
  min-width: var(--bd-min-width, 960px);
  min-height: var(--bd-min-height, 560px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

/* 遮罩轻微模糊：modal-class 作用在 .el-overlay 上，backdrop-filter 只模糊其背后的页面内容；
   -webkit- 前缀兼容旧版 Safari/Chrome */
.base-dialog-mask {
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
}

.base-dialog .el-dialog__header,
.base-dialog .el-dialog__footer {
  flex-shrink: 0;
}

.base-dialog .el-dialog__body {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  padding: 20px;
}

/* body 占位：只负责撑满可用高度，不预设内部布局（flex/滚动由页面内容自行决定） */
.base-dialog-body {
  flex: 1;
  min-height: 0;
}
</style>
