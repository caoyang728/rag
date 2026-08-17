import { computed, ref } from 'vue'
import { useTheme } from './useTheme'
import { formatDate } from '../utils/format'
import { getUser } from '../utils/authStorage'

/**
 * 文档预览水印（防截图泄密，供 Upload/AdminDocs/AdminNodes/Chat 预览弹窗复用）
 * - previewWatermark: 水印文案（账号 + 打开时间）
 * - watermarkFont: el-watermark 字体配置，颜色随暗色/浅色主题切换
 * - refreshWatermark: 打开预览时调用，刷新为当前时间
 */
export function useDocWatermark() {
  const { isDark } = useTheme()
  const previewWatermark = ref('')

  // 水印颜色随主题切换：浅色深灰半透明，暗色浅灰半透明。
  // （普通可见水印，非盲水印；透明度贴近背景、不干扰阅读，
  //  页图模式的纸张底由使用方按主题切换水印颜色，见 Chat 页 watermarkBg）
  const watermarkFont = computed(() => ({
    fontSize: 14,
    rotate: -20,
    gap: [80, 48],
    color: isDark.value ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.05)',
  }))

  // 刷新水印文案：当前登录账号 + 打开预览的时间（每次打开都更新，防截图泄密）
  function refreshWatermark() {
    const u = getUser() || {}
    const uid = u.username || u.id || '?'
    previewWatermark.value = uid + ' · ' + formatDate(new Date())
  }

  return { previewWatermark, watermarkFont, refreshWatermark }
}
