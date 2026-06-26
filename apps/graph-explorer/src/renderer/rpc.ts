import type { ServerMessage } from '../shared/protocol.js'
import type { AgentEvent } from '../shared/agent-events.js'

let counter = 0
const nextId = () => `r${++counter}`

type Pending = { resolve: (v: unknown) => void; reject: (e: Error) => void }

/** Thin WebSocket RPC client to the sidecar. */
export class Rpc {
  private ws: WebSocket | null = null
  private pending = new Map<string, Pending>()
  private streams = new Map<string, (e: AgentEvent) => void>()
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
      if (cb) cb(msg.event as AgentEvent)
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

  /** Start a streaming chat turn; returns the request id and a cancel fn. */
  chat(params: unknown, onEvent: (e: AgentEvent) => void): { id: string; cancel: () => void } {
    const id = nextId()
    this.streams.set(id, onEvent)
    this.pending.set(id, {
      resolve: () => this.streams.delete(id),
      reject: () => this.streams.delete(id)
    })
    void this.ready.then(() => this.raw({ type: 'rpc', id, op: 'chat', params }))
    return {
      id,
      cancel: () => void this.call('cancelChat', { id })
    }
  }
}
