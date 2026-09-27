// The renderer's RPC client survives a sidecar restart: in-flight requests are
// rejected with a clear error (never left hanging — that left the Worlds tab's
// Extend pane blank), the status goes `reconnecting`, and once the sidecar is
// back the client reconnects on its own and requests work again.
import { WebSocketServer } from 'ws'
import { afterEach, describe, expect, it } from 'vitest'
import { Rpc, type ConnectionStatus } from '../src/renderer/rpc.js'

let server: WebSocketServer | null = null

function serve(port: number, opts: { silent?: boolean } = {}): Promise<WebSocketServer> {
  return new Promise((resolve) => {
    const wss = new WebSocketServer({ host: '127.0.0.1', port }, () => resolve(wss))
    wss.on('connection', (ws) => {
      ws.on('message', (data) => {
        if (opts.silent) return // never reply: simulates a request in flight when the sidecar dies
        const req = JSON.parse(data.toString()) as { id: string; op: string }
        ws.send(JSON.stringify({ type: 'rpc_result', id: req.id, ok: true, result: `pong:${req.op}` }))
      })
    })
  })
}

function close(wss: WebSocketServer): Promise<void> {
  for (const c of wss.clients) c.terminate()
  return new Promise((r) => wss.close(() => r()))
}

const until = async (cond: () => boolean, ms = 5000): Promise<void> => {
  const start = Date.now()
  while (!cond()) {
    if (Date.now() - start > ms) throw new Error('timed out')
    await new Promise((r) => setTimeout(r, 20))
  }
}

afterEach(async () => {
  if (server) await close(server)
  server = null
})

describe('Rpc reconnection', () => {
  it('rejects in-flight calls on a drop, reports reconnecting, then works again', async () => {
    server = await serve(0, { silent: true })
    const port = (server.address() as { port: number }).port
    const rpc = new Rpc(port)
    const statuses: ConnectionStatus[] = []
    rpc.onStatus((s) => statuses.push(s))
    await until(() => statuses.includes('open'))

    const inFlight = rpc.call('readWorldConfig')
    await new Promise((r) => setTimeout(r, 50))
    await close(server) // the sidecar restarts under the open page
    server = null
    await expect(inFlight).rejects.toThrow(/lost the connection/)
    await until(() => statuses.at(-1) === 'reconnecting')

    server = await serve(port) // back on the same port
    await until(() => statuses.at(-1) === 'open')
    await expect(rpc.call('listWorlds')).resolves.toBe('pong:listWorlds')
  })

  it('a call made while disconnected is sent once the connection is back', async () => {
    server = await serve(0)
    const port = (server.address() as { port: number }).port
    const rpc = new Rpc(port)
    await expect(rpc.call('a')).resolves.toBe('pong:a')
    await close(server)
    server = null
    let status: ConnectionStatus = 'open'
    rpc.onStatus((s) => (status = s))
    await until(() => status === 'reconnecting')
    server = await serve(port)
    // Issued during the retry backoff: queued, then flushed on reopen.
    await expect(rpc.call('b')).resolves.toBe('pong:b')
  })
})
