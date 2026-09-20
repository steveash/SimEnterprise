// The `canUseTool` guard for the template-authoring agent (docs/EXPLORER_TEMPLATES.md §3).
//
// The authoring agent runs with `permissionMode: 'bypassPermissions'`, so this
// guard is the ONLY thing standing between it and the filesystem/shell. Keep it
// conservative and easy to audit:
//   - Read/Glob/Grep may look anywhere under the repo root (never above it).
//   - Write/Edit may only touch files under `<templatesDir>/<slug>/`.
//   - Bash may only run a command whose text starts with one of a small
//     allow-listed set of prefixes (the validate/lint/test loop); everything
//     else — including a prefix match followed by shell chaining — is denied.
//   - Every other tool is denied outright (the harness's `allowedTools` should
//     already keep the agent from offering them, but this is the backstop).
import { isAbsolute, relative, resolve, sep } from 'node:path'

export interface GuardContext {
  /** The repo root (`pyproject.toml`, `enterprise_sim/`, `skills/` live here). */
  repoRoot: string
  /** The templates root (`GRAPH_EXPLORER_TEMPLATES_DIR`, default `<repoRoot>/templates`). */
  templatesDir: string
  /** The template this authoring session is scoped to. */
  slug: string
}

export interface GuardDecision {
  allow: boolean
  /** Present when `allow` is false: a human-readable reason the agent can read. */
  reason?: string
}

/** True iff `target` (resolved) is `root` itself or strictly inside it. */
function isInside(root: string, target: string): boolean {
  const rel = relative(resolve(root), resolve(target))
  return rel === '' || (!rel.startsWith('..') && !isAbsolute(rel))
}

function readPathField(toolName: string, input: Record<string, unknown>): string | undefined {
  // Read uses `file_path`; Glob/Grep use `path` and both are optional (default
  // to the process cwd, which the harness sets to `repoRoot` — safe to allow).
  const field = toolName === 'Read' ? input.file_path : input.path
  return typeof field === 'string' ? field : undefined
}

function decideRead(toolName: string, input: Record<string, unknown>, ctx: GuardContext): GuardDecision {
  const path = readPathField(toolName, input)
  if (path === undefined) return { allow: true } // defaults to cwd (repoRoot)
  if (!isInside(ctx.repoRoot, path)) {
    return { allow: false, reason: `${toolName} may only read inside the repo (${ctx.repoRoot}); got ${path}` }
  }
  return { allow: true }
}

function decideWrite(toolName: string, input: Record<string, unknown>, ctx: GuardContext): GuardDecision {
  const path = input.file_path
  if (typeof path !== 'string' || !path) {
    return { allow: false, reason: `${toolName} requires a file_path` }
  }
  const scope = resolve(ctx.templatesDir, ctx.slug)
  if (!isInside(scope, path)) {
    return {
      allow: false,
      reason: `${toolName} may only touch files under templates/${ctx.slug}/ (${scope}); got ${path}`
    }
  }
  return { allow: true }
}

/** Allow-listed Bash command prefixes (docs/EXPLORER_TEMPLATES.md §3). */
function bashAllowPrefixes(ctx: GuardContext): string[] {
  return [
    'uv run enterprise-sim templates validate',
    'uv run enterprise-sim lint',
    `uv run pytest templates/${ctx.slug}`,
    'uv run python -c'
  ]
}

function decideBash(input: Record<string, unknown>, ctx: GuardContext): GuardDecision {
  const command = input.command
  if (typeof command !== 'string' || !command.trim()) {
    return { allow: false, reason: 'Bash requires a command' }
  }
  const trimmed = command.trim()
  const prefixes = bashAllowPrefixes(ctx)
  if (prefixes.some((p) => trimmed.startsWith(p))) return { allow: true }
  return {
    allow: false,
    reason:
      `Bash command not allow-listed for template authoring. Allowed prefixes: ` +
      prefixes.map((p) => `"${p}"`).join(', ') +
      `. Got: "${trimmed}"`
  }
}

/**
 * Decide whether a tool call from the template-authoring agent may proceed.
 * Pure and synchronous so it is trivial to unit test as an allow/deny matrix.
 */
export function decide(toolName: string, input: Record<string, unknown>, ctx: GuardContext): GuardDecision {
  switch (toolName) {
    case 'Read':
    case 'Glob':
    case 'Grep':
      return decideRead(toolName, input, ctx)
    case 'Write':
    case 'Edit':
      return decideWrite(toolName, input, ctx)
    case 'Bash':
      return decideBash(input, ctx)
    default:
      return { allow: false, reason: `tool "${toolName}" is not permitted during template authoring` }
  }
}
