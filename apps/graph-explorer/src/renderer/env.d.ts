/// <reference types="vite/client" />
import type { ExplorerApi } from '../preload/index.js'

declare global {
  interface Window {
    explorer: ExplorerApi
  }
}

export {}
