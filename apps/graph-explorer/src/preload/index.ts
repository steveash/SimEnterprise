import { contextBridge, ipcRenderer } from 'electron'
import type { Lens, LensInput } from '../shared/lenses.js'

export interface SidecarInfo {
  port: number
  runsRoot: string
  error: string | null
}

const api = {
  sidecarInfo: (): Promise<SidecarInfo> => ipcRenderer.invoke('sidecar-info'),
  pickRunDir: (): Promise<string | null> => ipcRenderer.invoke('pick-run-dir'),
  lenses: {
    list: (): Promise<Lens[]> => ipcRenderer.invoke('lenses-list'),
    save: (input: LensInput): Promise<Lens[]> => ipcRenderer.invoke('lenses-save', input),
    delete: (id: string): Promise<Lens[]> => ipcRenderer.invoke('lenses-delete', id)
  }
}

contextBridge.exposeInMainWorld('explorer', api)

export type ExplorerApi = typeof api
