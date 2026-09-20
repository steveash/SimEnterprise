// Renders a `templates validate` report step-by-step (docs/EXPLORER_TEMPLATES.md
// §4): the six steps (import, lint, conformance, archetype_sanity, tests,
// dry_run), each with its ok/fail status and its `detail` rendered in the shape
// that step actually produces — a diagnostics table for `lint`, a violation list
// for `conformance`/`archetype_sanity`, a small key/value table for
// `tests`/`dry_run`, or plain text for an `import` failure.
import type { LintDiagnostic, ValidationReport, ValidationStep } from '../../../shared/templates-protocol.js'

const STEP_LABEL: Record<ValidationStep['step'], string> = {
  import: 'Import',
  lint: 'Lint',
  conformance: 'Conformance',
  archetype_sanity: 'Archetype sanity',
  tests: 'Tests',
  dry_run: 'Dry run'
}

function isLintDiagnostics(v: unknown): v is LintDiagnostic[] {
  return Array.isArray(v) && v.every((d) => d && typeof d === 'object' && 'code' in d && 'message' in d)
}

function DetailView({ step, detail }: { step: ValidationStep['step']; detail: unknown }): JSX.Element | null {
  if (step === 'lint' && isLintDiagnostics(detail)) {
    if (detail.length === 0) return <div className="muted small">no diagnostics</div>
    return (
      <table className="prop-table tpl-lint-table">
        <thead>
          <tr>
            <th>severity</th>
            <th>code</th>
            <th>message</th>
            <th>location</th>
          </tr>
        </thead>
        <tbody>
          {detail.map((d, i) => (
            <tr key={i} className={d.severity === 'error' ? 'err' : ''}>
              <td>{d.severity}</td>
              <td className="mono">{d.code}</td>
              <td>{d.message}</td>
              <td className="mono small">{d.location}</td>
            </tr>
          ))}
        </tbody>
      </table>
    )
  }
  if (Array.isArray(detail)) {
    if (detail.length === 0) return <div className="muted small">none</div>
    return (
      <ul className="tpl-detail-list">
        {detail.map((line, i) => (
          <li key={i}>{String(line)}</li>
        ))}
      </ul>
    )
  }
  if (detail && typeof detail === 'object') {
    const entries = Object.entries(detail as Record<string, unknown>).filter(([k]) => k !== 'output')
    return (
      <table className="prop-table">
        <tbody>
          {entries.map(([k, v]) => (
            <tr key={k}>
              <td className="pk">{k}</td>
              <td className="pv mono small">{typeof v === 'string' ? v : JSON.stringify(v)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    )
  }
  if (detail === undefined || detail === null || detail === '') return null
  return <div className="mono small">{String(detail)}</div>
}

export function ValidationCard({ report }: { report: ValidationReport | null }): JSX.Element {
  if (!report) {
    return (
      <div className="tpl-validation empty">
        <div className="muted small">Not validated yet in this session — click Validate, or let the agent run it.</div>
      </div>
    )
  }
  return (
    <div className="tpl-validation">
      <div className="tpl-validation-head">
        <span className={`eval-badge ${report.ok ? 'ok' : 'bad'}`}>{report.ok ? 'valid' : 'invalid'}</span>
        <span className="muted small">{report.slug}</span>
      </div>
      {report.steps.length === 0 && <div className="muted small">no step detail</div>}
      {report.steps.map((step) => (
        <details className="block tpl-step" key={step.step} open={!step.ok}>
          <summary>
            <span className={step.ok ? 'ok-dot' : 'bad-dot'} /> {STEP_LABEL[step.step] ?? step.step} — {step.ok ? 'ok' : 'FAILED'}
          </summary>
          <DetailView step={step.step} detail={step.detail} />
        </details>
      ))}
      {report.ok && (report.provides.archetypes.length + report.provides.playbooks.length + report.provides.processes.length > 0) && (
        <div className="tpl-provides-summary muted small">
          provides: {[...report.provides.archetypes, ...report.provides.playbooks, ...report.provides.processes].join(', ')}
        </div>
      )}
    </div>
  )
}
