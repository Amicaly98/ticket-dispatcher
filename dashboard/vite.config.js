import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    host: '127.0.0.1',   // 强制 IPv4，避免 Windows 上 IPv4/IPv6 不匹配导致 proxy 502
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      '/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
  // SPA 模式：所有路由都返回 index.html（支持 vue-router history 模式）
  appType: 'spa',
})
