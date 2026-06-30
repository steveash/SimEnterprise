// File-backed persistence for saved query lenses. Lives in the main process so
// the renderer never touches the filesystem (access is mediated by IPC). The
// store is a plain JSON array on disk; reads tolerate a missing or corrupt file
// by degrading to an empty list rather than throwing.

import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { randomUUID } from 'node:crypto'
import { isLens, type Lens, type LensInput } from '../shared/lenses.js'

export class LensStore {
  constructor(private readonly file: string) {}

  /** Current lenses, oldest first. Returns [] if the file is absent or invalid. */
  list(): Lens[] {
    if (!existsSync(this.file)) return []
    try {
      const parsed: unknown = JSON.parse(readFileSync(this.file, 'utf8'))
      if (!Array.isArray(parsed)) return []
      return parsed.filter(isLens).sort((a, b) => a.createdAt - b.createdAt)
    } catch {
      return []
    }
  }

  /**
   * Create a new lens, or overwrite an existing one when `input.id` matches.
   * Returns the full, updated list. Throws on an empty name/query.
   */
  save(input: LensInput): Lens[] {
    const name = input.name.trim()
    const query = input.query.trim()
    if (!name) throw new Error('lens name is required')
    if (!query) throw new Error('lens query is required')

    const lenses = this.list()
    const existing = input.id ? lenses.find((l) => l.id === input.id) : undefined
    if (existing) {
      existing.name = name
      existing.engine = input.engine
      existing.query = query
    } else {
      lenses.push({ id: randomUUID(), name, engine: input.engine, query, createdAt: Date.now() })
    }
    this.write(lenses)
    return lenses.sort((a, b) => a.createdAt - b.createdAt)
  }

  /** Remove the lens with `id` (no-op if absent). Returns the updated list. */
  delete(id: string): Lens[] {
    const lenses = this.list().filter((l) => l.id !== id)
    this.write(lenses)
    return lenses
  }

  private write(lenses: Lens[]): void {
    mkdirSync(dirname(this.file), { recursive: true })
    writeFileSync(this.file, JSON.stringify(lenses, null, 2), 'utf8')
  }
}
