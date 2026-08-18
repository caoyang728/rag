import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 构建产物输出到 Django 的 static/vue/ 目录，由 Django 以 /static/vue/ 路径服务
export default defineConfig({
  plugins: [vue()],
  base: '/static/vue/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  build: {
    outDir: '../static/vue',
    emptyOutDir: true,
    // 将体积较大的第三方库单独拆包，避免全部打进主 chunk，利于缓存与首屏
    rollupOptions: {
      output: {
        manualChunks: {
          'element-plus': ['element-plus'],
          'vendor': ['vue', 'vue-router', 'pinia']
        }
      }
    }
  },
  server: {
    port: 5173,
    proxy: {
      // 本地开发时代理 API 与静态资源到 Django（8000）
      '/api': 'http://localhost:8000',
      // '/static': 'http://localhost:8000',
      // /static/vue/ 由 Vite dev server 直接服务（HMR），其余 /static/ 转发 Django
      '/static': {
        target: 'http://localhost:8000',
        bypass(req) {
          if (req.url?.startsWith('/static/vue/')) return req.url
        },
      },
      '/media': 'http://localhost:8000'
    }
  }
})
