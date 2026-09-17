import { fileURLToPath, URL } from 'node:url'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

/**
 * 开发态把 /api、/health、/ready 代理到后端（FRONTEND-SPEC §1：独立工程，与后端解耦）。
 * 后端地址由 .env.development 的 VITE_API_TARGET 决定，不硬编码在源码里。
 *
 * `test` 段与构建共用同一份 `resolve.alias`：**别名为单一定义**，
 * 否则测试能解析 `@/...` 而构建不能（或反之）时会难查。
 */
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_API_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: Number(env.VITE_PORT || 5173),
      proxy: {
        '/api': { target, changeOrigin: true },
        '/health': { target, changeOrigin: true },
        '/ready': { target, changeOrigin: true },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      chunkSizeWarningLimit: 1600,
    },
    test: {
      // DOMPurify 需要真实 DOM（F-10.02 的安全断言必须跑在 DOM 环境下才成立）
      environment: 'jsdom',
      include: ['tests/**/*.spec.ts'],
      globals: false,
      restoreMocks: true,
      unstubGlobals: true,
    },
  }
})
