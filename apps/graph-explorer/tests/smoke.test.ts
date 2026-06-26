import { describe, it, expect } from 'vitest'
import { goldenRunExists, loadGolden } from './helpers.js'

// Proves the harness runs: vitest picks up TS, the sidecar loader imports
// cleanly under node, and the golden fixture loads into a GraphModel.
// Skips cleanly (exit 0) on a checkout without `runs/`.
describe.skipIf(!goldenRunExists())('harness smoke', () => {
  it('loadGolden() returns a non-empty graph', () => {
    const model = loadGolden()
    expect(model.nodes.length).toBeGreaterThan(0)
  })
})
