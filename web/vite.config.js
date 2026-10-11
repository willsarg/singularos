import { defineConfig } from 'vite';

// pnpm dev proxies /api to the Worker started by pnpm run dev:worker.
export default defineConfig({
  server: { proxy: { '/api': 'http://127.0.0.1:8787' } },
});
