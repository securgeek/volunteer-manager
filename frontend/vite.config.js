import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import vueDevTools from 'vite-plugin-vue-devtools'

export default defineConfig({
  plugins: [vue(), vueDevTools()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: {
    proxy: {
      // Listed first: Vite matches proxy keys in insertion order, and '/api' would
      // otherwise swallow the portal's routes.
      '/api/portal': { target: 'http://portal:5001/' },
      '/api': { target: 'http://backend:5000/' },
    },
  },
  base: '/',
})
