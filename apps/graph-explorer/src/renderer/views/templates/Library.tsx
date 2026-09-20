// Left pane of the Templates view (docs/EXPLORER_TEMPLATES.md §4): the "New
// template" form and the card library.
import { useState } from 'react'
import { useTemplatesStore, openAuthoringChat } from '../../store-templates.js'
import type { TemplateKind, TemplateListEntry } from '../../../shared/templates-protocol.js'

const KINDS: { id: TemplateKind; label: string }[] = [
  { id: 'department', label: 'Department' },
  { id: 'playbook', label: 'Playbook' },
  { id: 'bundle', label: 'Bundle (both)' }
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
          <button key={k.id} className={kind === k.id ? 'active' : ''} onClick={() => setKind(k.id)}>
            {k.label}
          </button>
        ))}
      </div>
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

  const provides = [...entry.provides.archetypes, ...entry.provides.playbooks, ...entry.provides.processes]

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
