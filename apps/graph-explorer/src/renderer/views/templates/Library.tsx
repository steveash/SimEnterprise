// Left pane of the Templates view (docs/EXPLORER_TEMPLATES.md §4): the "New
// template" form and the card library.
import { useState } from 'react'
import { useTemplatesStore, openAuthoringChat } from '../../store-templates.js'
import type { TemplateKind, TemplateListEntry } from '../../../shared/templates-protocol.js'

const KINDS: { id: TemplateKind; label: string; hint: string }[] = [
  { id: 'department', label: 'Department', hint: 'an org unit: its charter, team shapes and roles, and the playbooks it runs' },
  { id: 'playbook', label: 'Playbook', hint: 'a scenario script: the events and deliverables that become documents and graph edges' },
  { id: 'bundle', label: 'Dept + playbook', hint: 'a department together with a playbook it runs' },
  { id: 'data_scenario', label: 'Data scenario', hint: 'structured data: a causal model sampled into tables, views and business questions' }
]

function slugify(name: string): string {
  let s = name
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
  if (!s) return ''
  if (!/^[a-z]/.test(s)) s = `t_${s}`
  return s
}

function NewTemplateForm(): JSX.Element {
  const scaffold = useTemplatesStore((s) => s.scaffold)
  const scaffolding = useTemplatesStore((s) => s.scaffolding)
  const scaffoldError = useTemplatesStore((s) => s.scaffoldError)

  const [name, setName] = useState('')
  const [slug, setSlug] = useState('')
  const [slugTouched, setSlugTouched] = useState(false)
  const [kind, setKind] = useState<TemplateKind>('bundle')
  const [description, setDescription] = useState('')

  const onName = (v: string) => {
    setName(v)
    if (!slugTouched) setSlug(slugify(v))
  }

  const valid = /^[a-z][a-z0-9_]*$/.test(slug) && name.trim().length > 0 && !scaffolding

  const submit = async () => {
    if (!valid) return
    const template = await scaffold({ slug, kind, name: name.trim(), description: description.trim() })
    if (template) {
      openAuthoringChat(template.slug, template.kind, template.description)
      setName('')
      setSlug('')
      setSlugTouched(false)
      setDescription('')
    }
  }

  return (
    <div className="tpl-new">
      <div className="section-title">New template</div>
      <input
        className="search-input small"
        placeholder="Name (e.g. Clinical Trials)"
        value={name}
        onChange={(e) => onName(e.target.value)}
      />
      <input
        className="search-input small mono"
        placeholder="slug"
        value={slug}
        onChange={(e) => {
          setSlugTouched(true)
          setSlug(e.target.value)
        }}
      />
      <div className="tpl-kind-row">
        {KINDS.map((k) => (
          <button key={k.id} className={kind === k.id ? 'active' : ''} title={k.hint} onClick={() => setKind(k.id)}>
            {k.label}
          </button>
        ))}
      </div>
      <div className="tpl-hint muted">{KINDS.find((k) => k.id === kind)?.hint}</div>
      <textarea
        className="tpl-desc-input"
        placeholder="One-paragraph domain description…"
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      <button className="btn primary" disabled={!valid} onClick={() => void submit()}>
        {scaffolding ? 'Scaffolding…' : 'Scaffold & author'}
      </button>
      {slug && !/^[a-z][a-z0-9_]*$/.test(slug) && (
        <div className="tpl-hint warn">slug must start with a letter and use only lowercase letters, digits, underscores</div>
      )}
      {scaffoldError && <div className="tpl-hint err">{scaffoldError}</div>}
    </div>
  )
}

function pillClass(status: string): string {
  if (status === 'valid') return 'ok'
  if (status === 'invalid') return 'bad'
  return 'muted'
}

function TemplateCard({ entry }: { entry: TemplateListEntry }): JSX.Element {
  const selectedSlug = useTemplatesStore((s) => s.selectedSlug)
  const selectTemplate = useTemplatesStore((s) => s.selectTemplate)
  const validate = useTemplatesStore((s) => s.validate)
  const loadFiles = useTemplatesStore((s) => s.loadFiles)
  const deleteTemplate = useTemplatesStore((s) => s.deleteTemplate)
  const [validating, setValidating] = useState(false)
  const [confirming, setConfirming] = useState(false)

  if (!entry.loadable) {
    return (
      <div className="tpl-card broken">
        <div className="tpl-card-head">
          <span className="tpl-name mono">{entry.slug}</span>
          <span className="eval-badge bad">unloadable</span>
        </div>
        <div className="muted small">{entry.error}</div>
      </div>
    )
  }

  const provides = [
    ...entry.provides.archetypes,
    ...entry.provides.playbooks,
    ...entry.provides.processes,
    ...(entry.provides.data_scenarios ?? [])
  ]

  return (
    <div className={`tpl-card ${selectedSlug === entry.slug ? 'selected' : ''}`}>
      <div className="tpl-card-head" onClick={() => selectTemplate(entry.slug)}>
        <span className="tpl-name">{entry.name}</span>
        <span className={`eval-badge ${pillClass(entry.validation.status)}`}>{entry.validation.status}</span>
      </div>
      <div className="muted small">
        {entry.kind} · <span className="mono">{entry.slug}</span>
      </div>
      {provides.length > 0 && <div className="tpl-provides mono small">{provides.join(', ')}</div>}
      <div className="tpl-updated small muted">updated {new Date(entry.updated_at).toLocaleString()}</div>
      <div className="btn-row">
        <button
          className="mini-btn"
          disabled={validating}
          onClick={async () => {
            setValidating(true)
            await validate(entry.slug)
            setValidating(false)
          }}
        >
          {validating ? 'Validating…' : 'Validate'}
        </button>
        <button
          className="mini-btn"
          onClick={() => {
            selectTemplate(entry.slug)
          }}
        >
          Open chat
        </button>
        <button
          className="mini-btn"
          onClick={() => {
            selectTemplate(entry.slug)
            void loadFiles(entry.slug)
          }}
        >
          View files
        </button>
        {confirming ? (
          <>
            <button className="mini-btn err" onClick={() => void deleteTemplate(entry.slug).then(() => setConfirming(false))}>
              Confirm delete
            </button>
            <button className="mini-btn" onClick={() => setConfirming(false)}>
              Cancel
            </button>
          </>
        ) : (
          <button className="mini-btn" onClick={() => setConfirming(true)}>
            Delete
          </button>
        )}
      </div>
    </div>
  )
}

export function TemplateLibrary(): JSX.Element {
  const entries = useTemplatesStore((s) => s.entries)
  const loadingList = useTemplatesStore((s) => s.loadingList)
  const listError = useTemplatesStore((s) => s.listError)
  const deleteError = useTemplatesStore((s) => s.deleteError)

  return (
    <div className="tpl-library">
      <details className="tpl-intro" open>
        <summary>What templates are for</summary>
        <div className="small muted">
          <p>
            Templates let you add new building blocks for worlds without touching the codebase. A world has two
            halves, and templates cover both:
          </p>
          <ul>
            <li>
              <strong>Documents + knowledge graph</strong> come from <strong>departments</strong> and{' '}
              <strong>playbooks</strong>. A <em>department</em> is an org-unit archetype — its charter, the team shapes
              and roles it staffs, and the playbooks it runs (built in: <span className="mono">engineering</span>,{' '}
              <span className="mono">retail</span>). A <em>playbook</em> is the script a project follows — the ordered
              events and deliverables (specs, reviews, tickets, decks) that become the documents in the corpus and
              the edges in the graph (built in: <span className="mono">build_software</span>,{' '}
              <span className="mono">sell_merchandise</span>).
            </li>
            <li>
              <strong>Structured data</strong> comes from <strong>data scenarios</strong>: a causal model of one part
              of the business (e.g. a support queue or sales pipeline) that is sampled into parquet tables and SQL
              views, linked to the same people and projects as the graph, with business questions the data answers
              (built in: <span className="mono">sales_pipeline</span>, <span className="mono">support_ops</span>, …).
            </li>
          </ul>
          <p>
            Pick a kind below, describe the domain, and the agent writes the plugin and a test and runs validation
            until it passes. Once it validates, it shows up in the Worlds form — departments and playbooks under
            Departments / Scenario instances, data scenarios under Structured data — so you can create new worlds, or
            extend an existing one, with it.
          </p>
        </div>
      </details>
      <NewTemplateForm />
      <div className="section-title">Library</div>
      {loadingList && <div className="muted small">loading…</div>}
      {listError && <div className="tpl-hint err">{listError}</div>}
      {deleteError && <div className="tpl-hint err">{deleteError}</div>}
      {!loadingList && entries.length === 0 && <div className="muted small">No templates yet — scaffold one above.</div>}
      <div className="tpl-cards">
        {entries.map((e) => (
          <TemplateCard key={e.slug} entry={e} />
        ))}
      </div>
    </div>
  )
}
