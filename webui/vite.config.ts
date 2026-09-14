import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// AI 3D Forge —— 开发期前端。生产构建产物输出到 webui/dist，由后端静态托管，
// 因此 base 必须是相对路径 './'，保证 assets 用相对地址解析。
//
// 代理目标可用 AIFORGE_DEV_API 覆盖：体检脚本要同时跑「假后端」和「真后端」
// 之外的东西时，端口冲突只能靠换端口解决（默认仍是本机后端 8760）。
const apiTarget = process.env.AIFORGE_DEV_API || 'http://127.0.0.1:8760';

export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 5273,
    strictPort: true,
    watch: {
      // .mjs 文件被「写临时文件再改名」的方式保存时，chokidar 会去 watch 那个
      // 转瞬即逝的临时目录并抛 EBUSY，直接把 dev server 打死。忽略掉最省事。
      ignored: ['**/.*.tmpdir/**', '**/*.tmp'],
    },
    proxy: {
      '/api': {
        target: apiTarget,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
  },
});
