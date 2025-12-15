import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      '/api': {
        // Use 'api' hostname in Docker, 'localhost' for local development
        target: process.env.VITE_API_PROXY_TARGET || 'http://api:8000',
        changeOrigin: true,
      },
    },
  },
})
