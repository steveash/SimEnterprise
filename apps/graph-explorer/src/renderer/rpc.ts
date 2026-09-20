import type { ServerMessage } from '../shared/protocol.js'
import type { AgentEvent } from '../shared/agent-events.js'

let counter = 0
const nextId = () => `r${++counter}`

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void }

/** Thin WebSocket RPC client to the sidecar. */
export class Rpc {
  private ws: WebSocket | null = null
  private pending = new Map<string, Pending>()
  private streams = new Map<string, (e: unknown) => void>()
  private ready: Promise<void>
  private queue: string[] = []

  constructor(port: number) {
    this.ready = new Promise((res, rej) => {
      const ws = new WebSocket(`ws://127.0.0.1:${port}`)
      this.ws = ws
      ws.onopen = () => {
        for (const m of this.queue) ws.send(m)
        this.queue = []
        res()
      }
      ws.onerror = () => rej(new Error('failed to connect to sidecar'))
      ws.onmessage = (ev) => this.onMessage(JSON.parse(ev.data as string) as ServerMessage)
    })
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
