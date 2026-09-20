// Wire types for the Templates feature (docs/EXPLORER_TEMPLATES.md), shared
// between the sidecar (`src/sidecar/templates/`) and the renderer
// (`src/renderer/views/templates/`). Mirrors the JSON shapes
// `enterprise_sim/templates/{store,validate,catalog,scaffold}.py` produce —
// read those before changing this file.
import type { AgentEvent } from './agent-events.js'

export type TemplateKind = 'department' | 'playbook' | 'bundle'
export type ValidationStatus = 'valid' | 'invalid' | 'unvalidated'

/** The plugin names a template's `plugin.py` registers, by kind. */
export interface TemplateProvides {
  archetypes: string[]
  playbooks: string[]
  processes: string[]
}

/** The last recorded `templates validate` outcome, as stored in `template.json`. */
export interface TemplateValidationInfo {
  status: ValidationStatus
  checked_at: string | null
  summary: string
}

/** One `template.json`, as `templates scaffold`/`templates list` return it. */
export interface Template {
  slug: string
  name: string
  kind: TemplateKind
  description: string
  provides: TemplateProvides
  created_at: string
  updated_at: string
  validation: TemplateValidationInfo
}

/**
 * One row of `templates list`: a parsed template plus its loadability. A
 * template whose `template.json` failed to parse has `loadable: false` and
 * every `Template` field is absent.
 */
export type TemplateListEntry =
  | (Template & { loadable: true; error: null })
  | { slug: string; loadable: false; error: string }

/** One step of a `templates validate` report ({"step","ok","detail"}). */
export interface ValidationStep {
  step: 'import' | 'lint' | 'conformance' | 'archetype_sanity' | 'tests' | 'dry_run'
  ok: boolean
  detail: unknown
}

/** A single lint diagnostic, as it appears in the `lint` step's `detail` array. */
export interface LintDiagnostic {
  code: string
  severity: string
  message: string
  location: string
}

/** The full report `templates validate` prints and returns. */
export interface ValidationReport {
  slug: string
  ok: boolean
  steps: ValidationStep[]
  provides: TemplateProvides
}

export interface CatalogArchetype {
  name: string
  charter: string
  playbooks: string[]
  source: string // "builtin" | `template:${slug}`
}

export interface CatalogPlaybook {
  name: string
  vertical: string
  deliverables: string[]
  source: string
}

export interface CatalogProcess {
  name: string
  emits: string[]
  requests: string[]
  source: string
}

/** `templates catalog`: built-ins + templates, one flat catalog. */
export interface TemplatesCatalog {
  archetypes: CatalogArchetype[]
  playbooks: CatalogPlaybook[]
  processes: CatalogProcess[]
}

/** One file in a template's directory, for the read-only file viewer. */
export interface TemplateFile {
  path: string // relative to the template directory, e.g. "plugin.py"
  content: string
}

export interface TemplatesReadResult {
  slug: string
  files: TemplateFile[]
}

// ---------------------------------------------------------------------------
// Op params/results (src/sidecar/templates/index.ts)
// ---------------------------------------------------------------------------

export interface TemplatesScaffoldParams {
  slug: string
  kind: TemplateKind
  name: string
  description?: string
}

export interface TemplatesValidateParams {
  slug: string
}

export interface TemplatesDeleteParams {
  slug: string
}

export interface TemplatesDeleteResult {
  ok: true
  slug: string
}

export interface TemplatesReadParams {
  slug: string
}

export interface TemplatesChatParams {
  slug: string
  prompt: string
  model?: string
  conversationId: string
}

export interface TemplatesCancelChatParams {
  id: string
}

/**
 * The authoring chat's event stream: the same {@link AgentEvent} kinds the
 * Explore chat streams, plus `template` — emitted whenever a `templates
 * validate` Bash tool call completes, carrying the parsed report so the
 * panel's status pill (and the validation card) update live.
 */
export type TemplateAgentEvent = AgentEvent | { kind: 'template'; slug: string; validation: ValidationReport }
