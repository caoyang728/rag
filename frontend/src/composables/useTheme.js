import { ref } from 'vue'

/**
 * 暗色/浅色主题切换（独立于 Pinia，布局与其他组件可直接使用）
 * - 通过 html.dark 类触发 Element Plus dark css-vars 与全局自定义变量
 * - 选择持久化到 localStorage（rag_theme），刷新后保持
 */

const STORAGE_KEY = 'rag_theme'

const isDark = ref(localStorage.getItem(STORAGE_KEY) === 'dark')

// 应用主题到 <html>：light 移除 dark 类，dark 添加；并持久化用户选择
function applyTheme(dark) {
  isDark.value = dark
  document.documentElement.classList.toggle('dark', dark)
  localStorage.setItem(STORAGE_KEY, dark ? 'dark' : 'light')
}

// 初始化主题：必须在 App mount 前调用，避免首帧闪白
export function initTheme() {
  applyTheme(isDark.value)
}

export function useTheme() {
  function toggleTheme() {
    applyTheme(!isDark.value)
  }
  return { isDark, toggleTheme }
}
