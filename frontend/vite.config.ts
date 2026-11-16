import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: fileURLToPath(new URL('../backend/app/static', import.meta.url)),
    emptyOutDir: true,
  },
  server: { proxy: { '/api': 'http://127.0.0.1:8080' } },
  test: { environment: 'node', include: ['src/**/*.test.{ts,tsx}'] },
})
