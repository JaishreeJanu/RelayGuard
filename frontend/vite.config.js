import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,       // Critical for Docker: binds to 0.0.0.0 to allow external access
    port: 5173,
    watch: {
      usePolling: true // Guarantees file change detection across Docker volume mounts
    }
  }
})