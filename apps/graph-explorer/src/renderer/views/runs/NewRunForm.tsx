import { useState } from 'react'
import { useRunsStore } from '../../store-runs.js'

interface Catalog {
  archetypes?: { name: string; charter?: string; playbooks?: string[] }[]
  playbooks?: { name: string; vertical?: string }[]
  backends?: string[]
  models?: Record<string, unknown>
  company_sizes?: string[]
}

interface EstimateResult {
  num_artifacts?: number
  estimated_cost_usd?: number
  events?: number
  world?: { nodes: number; edges: number; departments: number; scenarios: number }
  error?: string
}

interface Props {
  onDone: () => void
}

export function NewRunForm({ onDone }: Props): JSX.Element {
  const form = useRunsStore((s) => s.form)
  const extend = useRunsStore((s) => s.extend)
  const updateForm = useRunsStore((s) => s.updateForm)
  const toggleDepartment = useRunsStore((s) => s.toggleDepartment)
  const addProjectRow = useRunsStore((s) => s.addProjectRow)
  const removeProjectRow = useRunsStore((s) => s.removeProjectRow)
  const updateProjectRow = useRunsStore((s) => s.updateProjectRow)
  const loadFromJson = useRunsStore((s) => s.loadFromJson)
  const cancelExtend = useRunsStore((s) => s.cancelExtend)
  const estimate = useRunsStore((s) => s.estimate)
  const estimating = useRunsStore((s) => s.estimating)
  const estimateResult = useRunsStore((s) => s.estimateResult) as EstimateResult | null
  const estimateError = useRunsStore((s) => s.estimateError)
  const createAndStart = useRunsStore((s) => s.createAndStart)
  const creating = useRunsStore((s) => s.creating)
  const createError = useRunsStore((s) => s.createError)
  const catalog = useRunsStore((s) => s.catalog) as Catalog | null

  const [tomlText, setTomlText] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)

  const parentProjectCount = extend?.parentConfig ? ((extend.parentConfig.projects as unknown[]) ?? []).length : 0
  const locked = Boolean(extend) // company/seed/output_dir/departments are inherited on an extend

  const doLoad = (): void => {
    const res = loadFromJson(tomlText)
    setLoadError(res.ok ? null : (res.error ?? 'invalid JSON'))
  }

  const start = async (): Promise<void> => {
    await createAndStart()
    onDone()
  }

  return (
    <div className="runs-form">
      <div className="runs-form-head">
        <div className="section-title">{extend ? 'Extend run' : 'New run'}</div>
        <button className="btn ghost" onClick={onDone}>
          Close
        </button>
      </div>

      {extend && (
        <div className="block runs-extend-banner">
          Extending <span className="mono">{extend.parentRunPath}</span> — only the window, new scenario
          instances, and model/scale are editable.
          <button className="mini-btn" onClick={cancelExtend}>
            cancel extend
          </button>
        </div>
      )}

      {!extend && (
        <details className="block">
          <summary>Load from TOML/JSON…</summary>
          <textarea
            className="query-text mono small"
            placeholder="paste a RunConfig as JSON (TOML: convert first — see note)"
            value={tomlText}
            onChange={(e) => setTomlText(e.target.value)}
          />
          <div className="btn-row">
            <button className="btn ghost" onClick={doLoad}>
              Load
            </button>
            <span className="muted small">Accepts JSON. A TOML config can be pasted as its JSON equivalent.</span>
          </div>
          {loadError && <div className="msg-error">⚠ {loadError}</div>}
        </details>
      )}

      <section className="block">
        <div className="section-title">Company</div>
        <div className="runs-grid">
          <label>
            Name
            <input
              className="search-input"
              disabled={locked}
              value={form.company.name}
              onChange={(e) => updateForm({ company: { ...form.company, name: e.target.value } })}
            />
          </label>
          <label>
            Vertical
            <input
              className="search-input"
              disabled={locked}
              value={form.company.vertical}
              onChange={(e) => updateForm({ company: { ...form.company, vertical: e.target.value } })}
            />
          </label>
          <label>
            Size
            <select
              className="model-select"
              disabled={locked}
              value={form.company.size}
              onChange={(e) => updateForm({ company: { ...form.company, size: e.target.value } })}
            >
              {(catalog?.company_sizes ?? ['startup', 'small', 'medium', 'large', 'enterprise']).map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
        </div>
      </section>

      <section className="block">
        <div className="section-title">Window &amp; seed</div>
        <div className="runs-grid">
          <label>
            Period start
            <input
              className="search-input"
              type="date"
              disabled={locked}
              value={form.simulation.period_start}
              onChange={(e) => updateForm({ simulation: { ...form.simulation, period_start: e.target.value } })}
            />
          </label>
          <label>
            Period end
            <input
              className="search-input"
              type="date"
              value={form.simulation.period_end}
              onChange={(e) => updateForm({ simulation: { ...form.simulation, period_end: e.target.value } })}
            />
          </label>
          <label>
            Seed
            <input
              className="search-input"
              type="number"
              disabled={locked}
              value={form.seed}
              onChange={(e) => updateForm({ seed: Number(e.target.value) })}
            />
          </label>
        </div>
      </section>

      <section className="block">
        <div className="section-title">Departments (ordered; first = primary)</div>
        <div className="runs-dept-picker">
          {(catalog?.archetypes ?? []).map((a) => {
            const idx = form.departments.indexOf(a.name)
            return (
              <label key={a.name} className={`legend-row ${idx >= 0 ? '' : 'off'}`} title={a.charter}>
                <input type="checkbox" disabled={locked} checked={idx >= 0} onChange={() => toggleDepartment(a.name)} />
                <span className="legend-label">
                  {a.name} {idx >= 0 && <span className="muted small">#{idx + 1}</span>}
                </span>
              </label>
            )
          })}
          {!catalog && <div className="muted small">loading catalog…</div>}
        </div>
      </section>

      <section className="block">
        <div className="section-title">Scenario instances</div>
        {form.projects.map((p, i) => {
          const isParent = extend && i < parentProjectCount
          return (
            <div key={p.key} className="runs-project-row">
              <input
                className="search-input small"
                placeholder="name"
                disabled={Boolean(isParent)}
                value={p.name}
                onChange={(e) => updateProjectRow(p.key, { name: e.target.value })}
              />
              <input
                className="search-input small grow"
                placeholder="description"
                disabled={Boolean(isParent)}
                value={p.description}
                onChange={(e) => updateProjectRow(p.key, { description: e.target.value })}
              />
              <select
                className="model-select"
                disabled={Boolean(isParent)}
                value={p.playbook}
                onChange={(e) => updateProjectRow(p.key, { playbook: e.target.value })}
              >
                <option value="">(default playbook)</option>
                {(catalog?.playbooks ?? []).map((pb) => (
                  <option key={pb.name} value={pb.name}>
                    {pb.name}
                  </option>
                ))}
              </select>
              <select
                className="model-select"
                disabled={Boolean(isParent)}
                value={p.department}
                onChange={(e) => updateProjectRow(p.key, { department: e.target.value })}
              >
                <option value="">(primary department)</option>
                {form.departments.map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
              {!isParent && (
                <button className="mini-btn" onClick={() => removeProjectRow(p.key)}>
                  ✕
                </button>
              )}
            </div>
          )
        })}
        <button className="btn ghost small" onClick={addProjectRow}>
          + add scenario instance
        </button>
      </section>

      <section className="block">
        <div className="section-title">Model</div>
        <div className="runs-grid">
          <label>
            Backend
            <select
              className="model-select"
              value={form.model.backend}
              onChange={(e) => updateForm({ model: { ...form.model, backend: e.target.value } })}
            >
              {(catalog?.backends ?? ['anthropic_api', 'bedrock', 'claude_cli', 'fake']).map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </label>
          <label>
            Model
            <select
              className="model-select"
              value={form.model.name}
              onChange={(e) => updateForm({ model: { ...form.model, name: e.target.value } })}
            >
              {Object.keys(catalog?.models ?? { 'claude-opus-4-8': null }).map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
          <label>
            Realism ({form.model.realism.toFixed(2)})
            <input
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={form.model.realism}
              onChange={(e) => updateForm({ model: { ...form.model, realism: Number(e.target.value) } })}
            />
          </label>
        </div>
        <label className="runs-live-toggle">
          <input type="checkbox" checked={form.live} onChange={(e) => updateForm({ live: e.target.checked })} />
          Live (calls the real provider — <strong>this costs money</strong>)
        </label>
        {form.live && <div className="msg-error small">⚠ Live mode bills the configured backend for every artifact.</div>}
      </section>

      <section className="block">
        <div className="section-title">Scale</div>
        <div className="runs-grid">
          <label>
            Max concurrency
            <input
              className="search-input"
              type="number"
              value={form.scale.max_concurrency}
              onChange={(e) => updateForm({ scale: { ...form.scale, max_concurrency: Number(e.target.value) } })}
            />
          </label>
          <label>
            Cost ceiling (USD, optional)
            <input
              className="search-input"
              type="number"
              value={form.scale.cost_ceiling_usd ?? ''}
              onChange={(e) =>
                updateForm({ scale: { ...form.scale, cost_ceiling_usd: e.target.value ? Number(e.target.value) : null } })
              }
            />
          </label>
          <label>
            Cache dir (optional)
            <input
              className="search-input"
              value={form.scale.cache_dir}
              onChange={(e) => updateForm({ scale: { ...form.scale, cache_dir: e.target.value } })}
            />
          </label>
          <label>
            Est. input tokens / artifact
            <input
              className="search-input"
              type="number"
              value={form.scale.est_input_tokens_per_artifact}
              onChange={(e) =>
                updateForm({ scale: { ...form.scale, est_input_tokens_per_artifact: Number(e.target.value) } })
              }
            />
          </label>
          <label>
            Est. cached input tokens / artifact
            <input
              className="search-input"
              type="number"
              value={form.scale.est_cached_input_tokens_per_artifact}
              onChange={(e) =>
                updateForm({
                  scale: { ...form.scale, est_cached_input_tokens_per_artifact: Number(e.target.value) }
                })
              }
            />
          </label>
          <label>
            Est. output tokens / artifact
            <input
              className="search-input"
              type="number"
              value={form.scale.est_output_tokens_per_artifact}
              onChange={(e) =>
                updateForm({ scale: { ...form.scale, est_output_tokens_per_artifact: Number(e.target.value) } })
              }
            />
          </label>
        </div>
      </section>

      <section className="block">
        <div className="section-title">Output dir</div>
        <input
          className="search-input"
          disabled={locked}
          value={form.output_dir}
          onChange={(e) => updateForm({ output_dir: e.target.value })}
        />
      </section>

      <div className="btn-row">
        <button className="btn ghost" disabled={estimating} onClick={() => void estimate()}>
          {estimating ? 'estimating…' : 'Estimate'}
        </button>
        <button className="btn primary" disabled={creating} onClick={() => void start()}>
          {creating ? 'starting…' : extend ? 'Start extend' : 'Start'}
        </button>
      </div>

      {estimateError && <div className="msg-error">⚠ {estimateError}</div>}
      {createError && <div className="msg-error">⚠ {createError}</div>}
      {estimateResult && !estimateResult.error && (
        <div className="block runs-estimate">
          <div>
            <strong>{estimateResult.num_artifacts}</strong> artifacts · <strong>{estimateResult.events}</strong>{' '}
            events · est. <strong>${(estimateResult.estimated_cost_usd ?? 0).toFixed(2)}</strong>
          </div>
          {estimateResult.world && (
            <div className="muted small">
              world: {estimateResult.world.nodes}n/{estimateResult.world.edges}e ·{' '}
              {estimateResult.world.departments} departments · {estimateResult.world.scenarios} scenarios
            </div>
          )}
        </div>
      )}
      {estimateResult?.error && <div className="msg-error">⚠ {estimateResult.error}</div>}
    </div>
  )
}
