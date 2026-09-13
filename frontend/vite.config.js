import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址默认 8430；跑测试时如果那个端口被装好的正式版占着（用户正开着软件），
// 用 EGGPAPER_API 指到 fixture 后端去，不必把用户的窗口关掉。
const API = process.env.EGGPAPER_API || 'http://127.0.0.1:8430'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: { '/api': API, '/guide': API },
  },
})
