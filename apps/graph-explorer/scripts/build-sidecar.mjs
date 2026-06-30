// Build the production sidecar bundle.
//
// The sidecar hosts the native query engines (Kùzu Cypher addon, Oxigraph WASM)
// and the Claude Agent SDK. In dev it runs straight from TS via `tsx` under the
// *system* node so the prebuilt kuzu `.node` matches the system ABI. Electron's
// own node has a different ABI (MODULE_VERSION) and cannot load that addon, so a
// packaged app still has to run the sidecar under a real node binary — not the
// Electron runtime.
//
// This script produces a self-contained `out/sidecar/` directory:
//   index.mjs                bundled sidecar (esbuild; ws + agent SDK inlined)
//   node_modules/kuzu        native Cypher addon (kuzujs.node, trimmed)
//   node_modules/oxigraph    WASM SPARQL engine (ABI-independent)
//   node[.exe]               (optional) bundled node binary for a hermetic app
//
// electron-builder ships this whole directory via `extraResources`, and the main
// process spawns `node index.mjs` from it (see src/main/index.ts).

import { build } from 'esbuild'
import {
  rmSync,
  mkdirSync,
  cpSync,
  copyFileSync,
  existsSync,
  chmodSync,
  readdirSync
} from 'node:fs'
import { join, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const APP_ROOT = resolve(__dirname, '..')
const OUT = join(APP_ROOT, 'out', 'sidecar')
const NODE_MODULES = join(APP_ROOT, 'node_modules')
const OUT_NM = join(OUT, 'node_modules')

// kuzu and oxigraph cannot be inlined: kuzu is a native `.node` addon and
// oxigraph loads its `.wasm` relative to its own file. They are shipped as real
// packages in out/sidecar/node_modules and resolved at runtime via createRequire.
const EXTERNAL = ['kuzu', 'oxigraph']

function log(msg) {
  process.stdout.write(`[build-sidecar] ${msg}\n`)
}

// 1. Clean + bundle the sidecar entry to a single ESM file.
rmSync(OUT, { recursive: true, force: true })
mkdirSync(OUT, { recursive: true })

log('bundling src/sidecar/index.ts -> out/sidecar/index.mjs')
await build({
  entryPoints: [join(APP_ROOT, 'src', 'sidecar', 'index.ts')],
  bundle: true,
  platform: 'node',
  format: 'esm',
  target: 'node22',
  outfile: join(OUT, 'index.mjs'),
  external: EXTERNAL,
  // Bundled CJS deps (ws, agent SDK) call require() for node built-ins; provide a
  // real require for esbuild's ESM interop shim and for the createRequire() calls
  // that load the external native engines.
  banner: {
    js: 'import { createRequire as __crq } from "node:module";\nconst require = __crq(import.meta.url);'
  },
  logLevel: 'warning'
})

// 2. Stage the external native engines into out/sidecar/node_modules.
mkdirSync(OUT_NM, { recursive: true })
for (const pkg of EXTERNAL) {
  const src = join(NODE_MODULES, pkg)
  if (!existsSync(src)) throw new Error(`missing dependency: node_modules/${pkg} (run npm install)`)
  log(`staging node_modules/${pkg}`)
  cpSync(src, join(OUT_NM, pkg), { recursive: true, dereference: true })
}

// 3. Trim kuzu: drop the 400MB+ C++ source tree and the build-only install.js,
//    and keep only the prebuilt addon for the platform(s) we ship. The active
//    `kuzujs.node` (already the install platform's binary) is what gets loaded.
const kuzuOut = join(OUT_NM, 'kuzu')
rmSync(join(kuzuOut, 'kuzu-source'), { recursive: true, force: true })
rmSync(join(kuzuOut, 'install.js'), { force: true })
const prebuilt = join(kuzuOut, 'prebuilt')
if (existsSync(prebuilt) && existsSync(join(kuzuOut, 'kuzujs.node'))) {
  // kuzujs.node is the resolved binary; the per-platform prebuilt copies are dead weight.
  rmSync(prebuilt, { recursive: true, force: true })
}

// 4. Optionally bundle the running node binary so the app is hermetic (no system
//    node required). Skip with BUNDLE_NODE=0 to fall back to a located node.
if (process.env.BUNDLE_NODE !== '0') {
  const nodeBin = process.execPath
  const destName = process.platform === 'win32' ? 'node.exe' : 'node'
  const dest = join(OUT, destName)
  log(`bundling node runtime: ${nodeBin} -> out/sidecar/${destName}`)
  copyFileSync(nodeBin, dest)
  chmodSync(dest, 0o755)
} else {
  log('BUNDLE_NODE=0 — skipping node runtime (app will locate a system node at runtime)')
}

const staged = readdirSync(OUT)
log(`done. out/sidecar contains: ${staged.join(', ')}`)
