#!/usr/bin/env bash
# Browser-mode dev server: the graph explorer UI without Electron.
#
# The app is an Electron desktop app, which is awkward to see from a headless or
# remote dev box. The renderer only needs the sidecar's WebSocket, so this serves
# the same UI over plain Vite and shims the four Electron calls
# (src/renderer/browser-shim.ts). Forward both ports and open the web port in any
# browser — no X server, no VNC.
#
#   npm run web
#
# Ports (override with env): GRAPH_EXPLORER_WEB_PORT=5173, GRAPH_EXPLORER_PORT=8787.
# Runs are discovered under GRAPH_EXPLORER_RUNS_ROOT (default: the repo's runs/).
set -euo pipefail
cd "$(dirname "$0")/.."

export GRAPH_EXPLORER_PORT="${GRAPH_EXPLORER_PORT:-8787}"
export GRAPH_EXPLORER_WEB_PORT="${GRAPH_EXPLORER_WEB_PORT:-5173}"
export GRAPH_EXPLORER_RUNS_ROOT="${GRAPH_EXPLORER_RUNS_ROOT:-$(cd ../../runs && pwd)}"

echo "sidecar  : 127.0.0.1:${GRAPH_EXPLORER_PORT}   (runs root: ${GRAPH_EXPLORER_RUNS_ROOT})"
echo "web UI   : http://127.0.0.1:${GRAPH_EXPLORER_WEB_PORT}"
echo "forward BOTH ports, then open the web UI in a browser."
echo

npx tsx src/sidecar/index.ts &
SIDECAR_PID=$!
# Never leave the sidecar orphaned holding its port when Vite exits or Ctrl-C.
trap 'kill "$SIDECAR_PID" 2>/dev/null || true' EXIT INT TERM

# Give the native engines (kuzu/oxigraph) a moment to load before the UI connects.
sleep 3
npx vite --config vite.web.config.ts --host 127.0.0.1
