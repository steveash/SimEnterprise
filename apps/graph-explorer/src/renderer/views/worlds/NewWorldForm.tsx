import { useState } from 'react'
import { useWorldsStore } from '../../store-worlds.js'

interface Catalog {
  archetypes?: { name: string; charter?: string; playbooks?: string[]; source?: string }[]
  playbooks?: { name: string; vertical?: string; source?: string }[]
  data_scenarios?: { name: string; title: string; brief?: string; source?: string }[]
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

export function NewWorldForm({ onDone }: Props): JSX.Element {
  const form = useWorldsStore((s) => s.form)
  const extend = useWorldsStore((s) => s.extend)
  const updateForm = useWorldsStore((s) => s.updateForm)
  const toggleDepartment = useWorldsStore((s) => s.toggleDepartment)
  const toggleDataScenario = useWorldsStore((s) => s.toggleDataScenario)
  const addProjectRow = useWorldsStore((s) => s.addProjectRow)
  const removeProjectRow = useWorldsStore((s) => s.removeProjectRow)
  const updateProjectRow = useWorldsStore((s) => s.updateProjectRow)
  const loadFromJson = useWorldsStore((s) => s.loadFromJson)
  const cancelExtend = useWorldsStore((s) => s.cancelExtend)
  const estimate = useWorldsStore((s) => s.estimate)
  const estimating = useWorldsStore((s) => s.estimating)
  const estimateResult = useWorldsStore((s) => s.estimateResult) as EstimateResult | null
  const estimateError = useWorldsStore((s) => s.estimateError)
  const createAndStart = useWorldsStore((s) => s.createAndStart)
  const creating = useWorldsStore((s) => s.creating)
  const createError = useWorldsStore((s) => s.createError)
  const catalog = useWorldsStore((s) => s.catalog) as Catalog | null

  const [tomlText, setTomlText] = useState('')
  const [loadError, setLoadError] = useState<string | null>(null)

  const parentProjectCount = extend?.parentConfig ? ((extend.parentConfig.projects as unknown[]) ?? []).length : 0
  const locked = Boolean(extend) // company/seed/worlds_root/departments are inherited on an extend
  const parentData = new Set(
    ((extend?.parentConfig?.data as { scenarios?: string[] } | undefined)?.scenarios ?? []) as string[]
  )

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
        <div className="section-title">{extend ? 'Extend world' : 'New world'}</div>
        <button className="btn ghost" onClick={onDone}>
          Close
        </button>
      </div>

      {extend && (
        <div className="block runs-extend-banner">
          Extending <span className="mono">{extend.parentWorldDir}</span> <em>in place</em> — the same world grows:
          only a later window end, new scenario instances, more structured-data scenarios, and model/scale are
          editable. Everything already rendered
          is reused from the world's cache, so you only pay for what is new.
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
            placeholder="paste a WorldConfig as JSON (TOML: convert first — see note)"
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
        <div className="form-hint muted small">The one company this world simulates. Its name becomes the world's id.</div>
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
        <div className="form-hint muted small">
          The span of simulated time. Extending a world later only ever moves the end date later; the seed makes the
          organization and schedule reproducible.
        </div>
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
        <div className="form-hint muted small">
          A <strong>department</strong> is an org-unit archetype — its charter, the team shapes and roles it staffs,
          and the playbooks it runs. Leave all unchecked to let the company's vertical and size pick. Departments
          authored in the Templates tab appear here once they validate.
        </div>
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
        <div className="form-hint muted small">
          Each row is a project the company runs during the window. A <strong>playbook</strong> is the script it
          follows — the ordered events and deliverables (specs, reviews, tickets, decks) that become the documents
          in the corpus. Leave the playbook blank to use the department's default; playbooks from the Templates tab
          are listed once they validate. With no rows, the world invents its own projects.
        </div>
        <div className="runs-project-row runs-project-head muted small">
          <span>Project name</span>
          <span>What the project is about</span>
          <span>Playbook</span>
          <span>Department</span>
          <span />
        </div>
        {form.projects.map((p, i) => {
          const isParent = extend && i < parentProjectCount
          return (
            <div key={p.key} className="runs-project-row">
              <input
                className="search-input small"
                placeholder="e.g. Billing migration"
                disabled={Boolean(isParent)}
                value={p.name}
                onChange={(e) => updateProjectRow(p.key, { name: e.target.value })}
              />
              <input
                className="search-input small"
                placeholder="e.g. Move billing to the new metering service (optional — guides the documents)"
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
              {!isParent ? (
                <button className="mini-btn" title="remove this project" onClick={() => removeProjectRow(p.key)}>
                  ✕
                </button>
              ) : (
                <span />
              )}
            </div>
          )
        })}
        <button className="btn ghost small" onClick={addProjectRow}>
          + add scenario instance
        </button>
      </section>

      <section className="block">
        <div className="section-title">Structured data</div>
        <div className="form-hint muted small">
          A world has two halves: the <strong>documents + knowledge graph</strong> above, and optional{' '}
          <strong>structured data</strong> — each <em>data scenario</em> is a causal model of one part of the business
          (sales pipeline, support queue, subscriptions…) sampled into parquet tables and SQL views over the same
          window, with its entities linked to the same people and projects in the graph, plus business questions the
          data can answer. Pick any to build them with the world (they land in <span className="mono">data/</span>).
          Data scenarios authored in the Templates tab appear here once they validate.
        </div>
        <div className="runs-dept-picker data-scenario-picker">
          {(catalog?.data_scenarios ?? []).map((d) => {
            const on = form.data.scenarios.includes(d.name)
            const inherited = parentData.has(d.name)
            return (
              <label key={d.name} className={`legend-row ${on ? '' : 'off'}`} title={d.brief}>
                <input type="checkbox" disabled={inherited} checked={on} onChange={() => toggleDataScenario(d.name)} />
                <span className="legend-label">
                  {d.title} <span className="muted small mono">{d.name}</span>
                  {inherited && <span className="muted small"> (already in this world)</span>}
                </span>
              </label>
            )
          })}
          {catalog && (catalog.data_scenarios ?? []).length === 0 && (
            <div className="muted small">no data scenarios available (is the data extra installed?)</div>
          )}
          {!catalog && <div className="muted small">loading catalog…</div>}
        </div>
        {form.data.scenarios.length > 0 && (
          <div className="runs-grid">
            <label>
              Authoring
              <select
                className="model-select"
                value={form.data.authoring}
                onChange={(e) =>
                  updateForm({ data: { ...form.data, authoring: e.target.value as 'template' | 'llm' } })
                }
              >
                <option value="template">template (deterministic, free)</option>
                <option value="llm">llm (the model elaborates each spec; needs Live)</option>
              </select>
            </label>
            <label>
              Scale ({form.data.scale_factor}× rows)
              <input
                className="search-input"
                type="number"
                min={0.01}
                step={0.1}
                value={form.data.scale_factor}
                onChange={(e) => updateForm({ data: { ...form.data, scale_factor: Number(e.target.value) || 1 } })}
              />
            </label>
            <label>
              Question-loop passes
              <input
                className="search-input"
                type="number"
                min={1}
                max={5}
                value={form.data.iterations}
                onChange={(e) =>
                  updateForm({ data: { ...form.data, iterations: Math.min(5, Math.max(1, Number(e.target.value) || 1)) } })
                }
              />
            </label>
          </div>
        )}
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
            Cache dir (optional; default: inside the world)
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

      {!extend && (
        <section className="block">
          <div className="section-title">Worlds root</div>
          <div className="form-hint muted small">
            The world lands in <span className="mono">{form.worlds_root || 'worlds'}/&lt;world-id&gt;/</span>, one folder
            holding everything.
          </div>
          <input
            className="search-input"
            disabled={locked}
            value={form.worlds_root}
            onChange={(e) => updateForm({ worlds_root: e.target.value })}
          />
        </section>
      )}

      <div className="btn-row">
        <button className="btn ghost" disabled={estimating} onClick={() => void estimate()}>
          {estimating ? 'estimating…' : 'Estimate'}
        </button>
        <button className="btn primary" disabled={creating} onClick={() => void start()}>
          {creating ? 'starting…' : extend ? 'Extend & build' : 'Create & build'}
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
