import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET
          || (process.env.VITE_API_BASE_URL
            ? process.env.VITE_API_BASE_URL.replace('/api/v1', '')
            : 'http://localhost:8090'),
        changeOrigin: true,
        ws: true, // proxy /api/v1/events/ws (Live Events)
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
})
