// Shared test fixtures for the graph-explorer sidecar suite.
//
// Every test in this directory loads the same deterministic gold KG world — the
// "golden slice" produced by `enterprise-sim world create examples/golden.toml`.
// It lands under `worlds/` at the repo root (two levels up from this app), is
// gitignored, and reproduces byte-for-byte from its seed.
//
// `worlds/` is NOT checked in, so a fresh checkout won't have the golden world
// until it is generated. Tests must skip-guard on `goldenRunExists()` so the suite
// degrades gracefully (exit 0) instead of failing on such a checkout.

import { existsSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { loadWorld } from '../src/sidecar/graph/loader.js'
import type { GraphModel } from '../src/shared/model.js'

/** Absolute path to this app's root (apps/graph-explorer). */
const APP_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..')

/** Absolute path to the golden world directory (repo-root `worlds/...`). */
export const GOLDEN_RUN = join(
  APP_ROOT,
  '..',
  '..',
  'worlds',
  'golden-slice-co-5b1e84522235'
)

/**
 * True when the golden run is present on disk. Use as a skip-guard, e.g.
 * `describe.skipIf(!goldenRunExists())(...)`, so the suite passes on a checkout
 * without `worlds/`. Probes the `.world` and `kg/nodes.jsonl` every loader test relies on.
 */
export function goldenRunExists(): boolean {
  return existsSync(join(GOLDEN_RUN, '.world')) && existsSync(join(GOLDEN_RUN, 'kg', 'nodes.jsonl'))
}

/** Load the golden run into the canonical GraphModel. */
export function loadGolden(): GraphModel {
  return loadWorld(GOLDEN_RUN)
}
