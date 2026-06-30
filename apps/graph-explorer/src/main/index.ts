import { app, BrowserWindow, ipcMain, dialog } from 'electron'
import { spawn, type ChildProcess } from 'node:child_process'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { existsSync } from 'node:fs'
import { LensStore } from './lenses.js'
import type { LensInput } from '../shared/lenses.js'

const __dirname = dirname(fileURLToPath(import.meta.url))
// out/main/index.js -> app root is two up
const APP_ROOT = resolve(__dirname, '..', '..')
const REPO_ROOT = resolve(APP_ROOT, '..', '..')

// Pick up a locally-provided key (gitignored .env.local) so `hasApiKey` is
// accurate and the spawned sidecar inherits it via env.
const ENV_FILE = join(APP_ROOT, '.env.local')
if (existsSync(ENV_FILE)) {
  try {
    process.loadEnvFile(ENV_FILE)
  } catch {
    /* ignore */
  }
}
// In dev, runs auto-discover under the repo's runs/. In a packaged app there is
// no repo, so default to the user's home — they pick a run dir via the dialog.
const DEFAULT_RUNS_ROOT =
  process.env.GRAPH_EXPLORER_RUNS_ROOT ?? (app.isPackaged ? app.getPath('home') : join(REPO_ROOT, 'runs'))

/**
 * Resolve the command + args used to launch the sidecar.
 *
 * Dev: run the TS entry directly via `tsx` under the system node.
 * Packaged: run the esbuild-bundled `index.mjs` shipped in resources/sidecar,
 *   using a bundled node binary if present, else a located system node. Electron's
 *   own node has an incompatible native-addon ABI, so we never run it as the host.
 */
function resolveSidecar(): { command: string; args: string[]; cwd: string } | { error: string } {
  if (app.isPackaged) {
    const base = join(process.resourcesPath, 'sidecar')
    const entry = join(base, 'index.mjs')
    if (!existsSync(entry)) return { error: `packaged sidecar not found at ${entry}` }
    const bundledNode = join(base, process.platform === 'win32' ? 'node.exe' : 'node')
    const command =
      process.env.GRAPH_EXPLORER_NODE ?? (existsSync(bundledNode) ? bundledNode : process.platform === 'win32' ? 'node.exe' : 'node')
    return { command, args: [entry], cwd: base }
  }
  const tsxBin = join(APP_ROOT, 'node_modules', '.bin', 'tsx')
  const entry = join(APP_ROOT, 'src', 'sidecar', 'index.ts')
  if (!existsSync(tsxBin) || !existsSync(entry)) {
    return { error: `sidecar not found (tsx=${existsSync(tsxBin)} entry=${existsSync(entry)})` }
  }
  return { command: tsxBin, args: [entry], cwd: APP_ROOT }
}

let sidecar: ChildProcess | null = null
let sidecarPort = 0
let sidecarStartError: string | null = null

function startSidecar(): Promise<number> {
  return new Promise((resolvePort, reject) => {
    const resolved = resolveSidecar()
    if ('error' in resolved) return reject(new Error(resolved.error))
    sidecar = spawn(resolved.command, resolved.args, {
      cwd: resolved.cwd,
      env: {
        ...process.env,
        GRAPH_EXPLORER_RUNS_ROOT: DEFAULT_RUNS_ROOT,
        GRAPH_EXPLORER_PORT: '0'
      },
      stdio: ['ignore', 'pipe', 'pipe']
    })
    let settled = false
    sidecar.stdout?.on('data', (buf: Buffer) => {
      const text = buf.toString()
      const m = text.match(/SIDECAR_PORT=(\d+)/)
      if (m && !settled) {
        settled = true
        sidecarPort = Number(m[1])
        resolvePort(sidecarPort)
      }
      process.stdout.write(`[sidecar] ${text}`)
    })
    sidecar.stderr?.on('data', (buf: Buffer) => process.stderr.write(`[sidecar:err] ${buf}`))
    sidecar.on('exit', (code) => {
      if (!settled) {
        settled = true
        sidecarStartError = `sidecar exited early (code ${code})`
        reject(new Error(sidecarStartError))
      }
    })
    setTimeout(() => {
      if (!settled) {
        settled = true
        reject(new Error('sidecar did not report a port within 15s'))
      }
    }, 15000)
  })
}

function createWindow(): void {
  const win = new BrowserWindow({
    width: 1500,
    height: 950,
    title: 'Enterprise-Sim Graph Explorer',
    backgroundColor: '#0f1419',
    webPreferences: {
      preload: join(__dirname, '..', 'preload', 'index.mjs'),
      contextIsolation: true,
      sandbox: false,
      nodeIntegration: false
    }
  })

  const devUrl = process.env['ELECTRON_RENDERER_URL']
  if (devUrl) {
    void win.loadURL(devUrl)
  } else {
    void win.loadFile(join(__dirname, '..', 'renderer', 'index.html'))
  }
}

ipcMain.handle('sidecar-info', () => ({
  port: sidecarPort,
  runsRoot: DEFAULT_RUNS_ROOT,
  error: sidecarStartError,
  hasApiKey: Boolean(process.env.ANTHROPIC_API_KEY)
}))

ipcMain.handle('pick-run-dir', async () => {
  const res = await dialog.showOpenDialog({
    title: 'Select a run directory (the folder containing kg/)',
    defaultPath: DEFAULT_RUNS_ROOT,
    properties: ['openDirectory']
  })
  return res.canceled ? null : res.filePaths[0]
})

ipcMain.handle('set-api-key', (_e, key: string) => {
  process.env.ANTHROPIC_API_KEY = key
  // Restart sidecar so the SDK picks up the key.
  if (sidecar) sidecar.kill('SIGTERM')
  return startSidecar()
    .then((port) => ({ ok: true, port }))
    .catch((e: Error) => ({ ok: false, error: e.message }))
})

// Saved query lenses persist as JSON under the app's userData dir. Lazily
// constructed so `app.getPath` is only called once the app is ready.
let lensStore: LensStore | null = null
function getLensStore(): LensStore {
  if (!lensStore) lensStore = new LensStore(join(app.getPath('userData'), 'lenses.json'))
  return lensStore
}

ipcMain.handle('lenses-list', () => getLensStore().list())
ipcMain.handle('lenses-save', (_e, input: LensInput) => getLensStore().save(input))
ipcMain.handle('lenses-delete', (_e, id: string) => getLensStore().delete(id))

app.whenReady().then(async () => {
  try {
    await startSidecar()
  } catch (e) {
    sidecarStartError = (e as Error).message
    process.stderr.write(`failed to start sidecar: ${sidecarStartError}\n`)
  }
  createWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  if (sidecar) sidecar.kill('SIGTERM')
})
