import { contextBridge, ipcRenderer } from 'electron'

export interface SidecarInfo {
  port: number
  runsRoot: string
  error: string | null
  hasApiKey: boolean
}

const api = {
  sidecarInfo: (): Promise<SidecarInfo> => ipcRenderer.invoke('sidecar-info'),
  pickRunDir: (): Promise<string | null> => ipcRenderer.invoke('pick-run-dir'),
  setApiKey: (key: string): Promise<{ ok: boolean; port?: number; error?: string }> =>
    ipcRenderer.invoke('set-api-key', key)
}

contextBridge.exposeInMainWorld('explorer', api)

export type ExplorerApi = typeof api
