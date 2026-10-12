import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  return {
    plugins: [react(), tailwindcss()],
    build: { chunkSizeWarningLimit: 1600 }, // MapLibre GL pèse ~800 kB
    worker: { format: 'es' }, // worker MapLibre chargé comme module
    resolve: { alias: { '@': path.resolve(__dirname, './src') } },
    server: {
      // L'API TMS est appelée via le proxy : pas de CORS en développement.
      proxy: { '/v1': env.TMS_API_URL ?? 'http://127.0.0.1:8000' },
    },
  }
})
