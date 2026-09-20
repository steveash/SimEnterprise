/**
 * Browser-mode dev server for the renderer (dev only; the Electron app uses
 * electron.vite.config.ts and is unaffected).
 *
 * Serves the same React/Cytoscape UI over plain Vite so it can be viewed through
 * an SSH port-forward from a headless dev box. Two adjustments to the page:
 *
 *  - inject `src/renderer/browser-shim.ts` (and the sidecar port) BEFORE main.tsx,
 *    so `window.explorer` exists by the time the store's init() runs;
 *  - drop the Electron CSP meta tag, which forbids Vite's HMR websocket.
 */
import { resolve } from 'node:path'
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

const SIDECAR_PORT = Number(process.env.GRAPH_EXPLORER_PORT ?? 8787)
const WEB_PORT = Number(process.env.GRAPH_EXPLORER_WEB_PORT ?? 5173)

export default defineConfig({
  root: resolve('src/renderer'),
  resolve: { alias: { '@renderer': resolve('src/renderer') } },
  server: { host: '127.0.0.1', port: WEB_PORT, strictPort: true },
  plugins: [
    react(),
    {
      name: 'graph-explorer-browser-shim',
      transformIndexHtml(html: string) {
        return html
          .replace(/<meta\s+http-equiv="Content-Security-Policy"[\s\S]*?\/>\s*/, '')
          .replace(
            '<script type="module" src="./main.tsx"></script>',
            `<script>window.__SIDECAR_PORT__=${SIDECAR_PORT};</script>\n` +
              '    <script type="module" src="./browser-shim.ts"></script>\n' +
              '    <script type="module" src="./main.tsx"></script>'
          )
      }
    }
  ]
})
