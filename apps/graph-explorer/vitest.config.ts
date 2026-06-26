import { defineConfig } from 'vitest/config'

// Sidecar tests run under plain node (NOT Electron). The kuzu + oxigraph native
// addons and the Agent SDK all load fine under system node, so a node test
// environment is all the suite needs.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['tests/**/*.test.ts']
  }
})
