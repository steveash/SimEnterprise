/// <reference types="vite/client" />
import type { ExplorerApi } from '../preload/index.js'

declare global {
  interface Window {
    explorer: ExplorerApi
    /** Browser-mode only (see browser-shim.ts): sidecar port stamped by vite.web.config.ts. */
    __SIDECAR_PORT__: number
  }
}

export {}
