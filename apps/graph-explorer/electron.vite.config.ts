import { resolve } from 'node:path'
import { defineConfig } from 'electron-vite'
import react from '@vitejs/plugin-react'

// The sidecar (native-addon host) is NOT built here — it is run with the system
// `node` via `tsx` so the prebuilt kuzu/oxigraph binaries match the system ABI
// rather than Electron's. electron-vite only owns main / preload / renderer.
export default defineConfig({
  main: {
    build: {
      rollupOptions: {
        input: { index: resolve('src/main/index.ts') }
      }
    }
  },
  preload: {
    build: {
      rollupOptions: {
        input: { index: resolve('src/preload/index.ts') }
      }
    }
  },
  renderer: {
    root: resolve('src/renderer'),
    build: {
      rollupOptions: {
        input: { index: resolve('src/renderer/index.html') }
      }
    },
    resolve: {
      alias: { '@renderer': resolve('src/renderer') }
    },
    plugins: [react()]
  }
})
