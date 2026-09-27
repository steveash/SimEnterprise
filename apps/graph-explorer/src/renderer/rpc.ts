import type { ServerMessage } from '../shared/protocol.js'
import type { AgentEvent } from '../shared/agent-events.js'

let counter = 0
const nextId = () => `r${++counter}`

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void }

export type ConnectionStatus = 'connecting' | 'open' | 'reconnecting'

/**
 * Thin WebSocket RPC client to the sidecar, with automatic reconnection.
 *
 * If the socket drops (the sidecar restarted, the laptop slept, a port-forward
 * blipped), every request still waiting on a reply is rejected with a clear
 * error — never left hanging — and the client reconnects with backoff. Requests
 * made while disconnected are queued and sent as soon as the socket reopens.
 * `onStatus` lets the UI show the connection state.
 */
export class Rpc {
  private ws: WebSocket | null = null
  private pending = new Map<string, Pending>()
  private streams = new Map<string, (e: unknown) => void>()
  private ready: Promise<void>
  private queue: string[] = []
  private status: ConnectionStatus = 'connecting'
  private statusListeners = new Set<(s: ConnectionStatus) => void>()
  private everOpened = false
  private retryMs = 500

  constructor(private readonly port: number) {
    this.ready = new Promise((res, rej) => this.connect(res, rej))
  }

  /** Subscribe to connection-state changes; returns an unsubscribe function. */
  onStatus(cb: (s: ConnectionStatus) => void): () => void {
    this.statusListeners.add(cb)
    cb(this.status)
    return () => this.statusListeners.delete(cb)
  }

  private setStatus(s: ConnectionStatus): void {
    this.status = s
    for (const cb of this.statusListeners) cb(s)
  }

  private connect(onFirstOpen?: () => void, onFirstFail?: (e: Error) => void): void {
    const ws = new WebSocket(`ws://127.0.0.1:${this.port}`)
    this.ws = ws
    ws.onopen = () => {
      this.everOpened = true
      this.retryMs = 500
      this.setStatus('open')
      for (const m of this.queue) ws.send(m)
      this.queue = []
      onFirstOpen?.()
    }
    ws.onerror = () => {
      // The very first connection failing is a startup error the app reports;
      // after that, errors are followed by `onclose`, which reconnects.
      if (!this.everOpened) onFirstFail?.(new Error('failed to connect to sidecar'))
    }
    ws.onclose = () => {
      if (this.ws !== ws) return
      if (!this.everOpened) return // startup failure: surfaced via onFirstFail
      const lost = new Error('lost the connection to the graph engine (sidecar); reconnecting — try again')
      for (const p of this.pending.values()) p.reject(lost)
      this.pending.clear()
      this.streams.clear()
      // Their promises were just rejected: never send these later (e.g. start a build twice).
      this.queue = []
      this.setStatus('reconnecting')
      const wait = this.retryMs
      this.retryMs = Math.min(this.retryMs * 2, 5000)
      setTimeout(() => this.connect(), wait)
    }
    ws.onmessage = (ev) => this.onMessage(JSON.parse(ev.data as string) as ServerMessage)
  }

  private onMessage(msg: ServerMessage): void {
    if (msg.type === 'rpc_result') {
      const p = this.pending.get(msg.id)
      if (!p) return
      this.pending.delete(msg.id)
      if (msg.ok) p.resolve(msg.result)
      else p.reject(new Error(msg.error ?? 'rpc error'))
    } else if (msg.type === 'stream') {
      const cb = this.streams.get(msg.id)
      if (cb) cb(msg.event)
    }
  }

  private raw(payload: object): void {
    const s = JSON.stringify(payload)
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(s)
    else this.queue.push(s)
  }

  async call<T = unknown>(op: string, params?: unknown): Promise<T> {
    await this.ready
    const id = nextId()
    return new Promise<T>((resolve, reject) => {
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject })
      this.raw({ type: 'rpc', id, op, params })
    })
  }

  /**
   * Start a streaming op: every `{type:'stream', id, event}` the sidecar pushes
   * for this request reaches `onEvent`; `done` settles with the final reply (or
   * rejects). `cancelOp` (if given) is called with `{ id }` on `cancel()`.
   */
  stream<E = unknown, T = unknown>(
    op: string,
    params: unknown,
    onEvent: (e: E) => void,
    cancelOp?: string
  ): { id: string; cancel: () => void; done: Promise<T> } {
    const id = nextId()
    this.streams.set(id, onEvent as (e: unknown) => void)
    const done = new Promise<T>((resolve, reject) => {
      this.pending.set(id, {
        resolve: (v) => {
          this.streams.delete(id)
          resolve(v as T)
        },
        reject: (e) => {
          this.streams.delete(id)
          reject(e)
        }
      })
    })
    done.catch(() => {}) // callers may ignore `done`; never surface as unhandled
    void this.ready.then(() => this.raw({ type: 'rpc', id, op, params }))
    return {
      id,
      cancel: () => {
        if (cancelOp) void this.call(cancelOp, { id })
      },
      done
    }
  }

  /** Start a streaming chat turn; returns the request id and a cancel fn. */
  chat(params: unknown, onEvent: (e: AgentEvent) => void): { id: string; cancel: () => void } {
    const { id, cancel } = this.stream<AgentEvent>('chat', params, onEvent, 'cancelChat')
    return { id, cancel }
  }
}
