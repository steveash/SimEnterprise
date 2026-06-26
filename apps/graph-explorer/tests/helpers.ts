// Shared test fixtures for the graph-explorer sidecar suite.
//
// Every test in this directory loads the same deterministic gold KG run — the
// "golden slice" produced by `enterprise-sim run examples/golden.toml`. It lands
// under `runs/golden/` at the repo root (two levels up from this app), is
// gitignored, and reproduces byte-for-byte from its seed.
//
// `runs/` is NOT checked in, so a fresh checkout won't have the golden run until
// it is generated. Tests must skip-guard on `goldenRunExists()` so the suite
// degrades gracefully (exit 0) instead of failing on such a checkout.

import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadRun } from '../src/sidecar/graph/loader.js'
import type { GraphModel } from '../src/shared/model.js'

/** Absolute path to this app's root (apps/graph-explorer). */
const APP_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

/** Absolute path to the golden run directory (repo-root `runs/golden/...`). */
export const GOLDEN_RUN = join(
  APP_ROOT,
  '..',
  '..',
  'runs',
  'golden',
  'golden-slice-co-40644d551158'
)

/**
 * True when the golden run is present on disk. Use as a skip-guard, e.g.
 * `describe.skipIf(!goldenRunExists())(...)`, so the suite passes on a checkout
 * without `runs/`. Probes `kg/nodes.jsonl` — the file every loader test relies on.
 */
export function goldenRunExists(): boolean {
  return existsSync(join(GOLDEN_RUN, 'kg', 'nodes.jsonl'))
}

/** Load the golden run into the canonical GraphModel. */
export function loadGolden(): GraphModel {
  return loadRun(GOLDEN_RUN)
}
