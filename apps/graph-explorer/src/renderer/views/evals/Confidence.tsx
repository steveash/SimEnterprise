// How far a question's expected answer can be trusted (enterprise_sim.evals.critic):
// a small meter + label, and a distinct "verified" state once a human checked it.
import type { QAPair } from '../../../shared/evals-protocol.js'

export type ConfidenceBand = 'verified' | 'high' | 'medium' | 'low' | 'unreviewed'

export function confidenceBand(q: QAPair): ConfidenceBand {
  if (q.verified) return 'verified'
  if (typeof q.confidence !== 'number') return 'unreviewed'
  if (q.confidence >= 0.75) return 'high'
  if (q.confidence >= 0.4) return 'medium'
  return 'low'
}

export const BAND_LABEL: Record<ConfidenceBand, string> = {
  verified: 'human verified',
  high: 'high confidence',
  medium: 'medium confidence',
  low: 'low confidence',
  unreviewed: 'not reviewed'
}

/** Ordering for the "group by confidence" view: the ones needing a human first. */
export const BAND_ORDER: ConfidenceBand[] = ['low', 'medium', 'unreviewed', 'high', 'verified']

export function ConfidenceMeter({ q, showLabel = false }: { q: QAPair; showLabel?: boolean }): JSX.Element {
  const band = confidenceBand(q)
  const value = band === 'verified' ? 1 : (q.confidence ?? 0)
  const title =
    band === 'verified'
      ? `human verified${q.critique ? ` — ${q.critique}` : ''}`
      : band === 'unreviewed'
        ? 'the expected answer has not been reviewed yet'
        : `${Math.round(value * 100)}% confidence (${q.critic ?? 'critic'}): ${q.critique ?? ''}`
  return (
    <span className={`conf conf-${band}`} title={title}>
      {band === 'verified' ? (
        <span className="conf-check" aria-hidden="true">✓</span>
      ) : (
        <span className="conf-meter" aria-hidden="true">
          <span className="conf-fill" style={{ width: `${Math.round(value * 100)}%` }} />
        </span>
      )}
      {showLabel ? (
        <span className="conf-label">
          {BAND_LABEL[band]}
          {band !== 'verified' && band !== 'unreviewed' ? ` · ${Math.round(value * 100)}%` : ''}
        </span>
      ) : (
        <span className="conf-label sr-only">{BAND_LABEL[band]}</span>
      )}
    </span>
  )
}
