// A "lens" is a named, reusable query the user has promoted from the query
// console. Lenses persist across app restarts (stored as JSON in the app's
// userData dir by the main process) and are re-runnable with one click.

export type LensEngine = 'cypher' | 'sparql'

export interface Lens {
  id: string
  name: string
  engine: LensEngine
  query: string
  /** ms epoch the lens was created (used for stable ordering). */
  createdAt: number
}

/** Shape accepted by `saveLens` — the persistence layer assigns id/createdAt. */
export interface LensInput {
  /** Present when overwriting an existing lens; absent creates a new one. */
  id?: string
  name: string
  engine: LensEngine
  query: string
}

/** Narrow an untrusted value (e.g. parsed JSON) to a well-formed Lens. */
export function isLens(v: unknown): v is Lens {
  if (typeof v !== 'object' || v === null) return false
  const o = v as Record<string, unknown>
  return (
    typeof o.id === 'string' &&
    typeof o.name === 'string' &&
    (o.engine === 'cypher' || o.engine === 'sparql') &&
    typeof o.query === 'string' &&
    typeof o.createdAt === 'number'
  )
}
