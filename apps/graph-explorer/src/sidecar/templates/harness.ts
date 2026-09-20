// The template-authoring agent (docs/EXPLORER_TEMPLATES.md §3): a Claude Agent
// SDK `query()` turn with file tools (Read/Glob/Grep/Write/Edit/Bash) guarded by
// `./guard.js`, and the `author-playbook` skill as its instructions. This
// mirrors `../agent/harness.ts`'s message->event mapping (duplicated minimally
// rather than editing that file — see the task's file-ownership split) and adds
// one thing the graph chat doesn't need: watching for a `templates validate`
// Bash call's result and surfacing it as a structured `template` event.
import { readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { query } from '@anthropic-ai/claude-agent-sdk'
import type { TemplateAgentEvent, ValidationReport } from '../../shared/templates-protocol.js'
import { decide, type GuardContext } from './guard.js'

export interface TemplateChatContext {
  repoRoot: string
  templatesDir: string
  slug: string
}

const SKILL_PATH = ['skills', 'author-playbook', 'SKILL.md']
const PATTERNS_HINT = 'enterprise_sim/authoring/patterns.py'
const BUILD_SOFTWARE_HINT = 'enterprise_sim/playbooks/build_software.py'

function readSkill(repoRoot: string): string {
  const path = join(repoRoot, ...SKILL_PATH)
  if (!existsSync(path)) {
    return `(skills/author-playbook/SKILL.md not found at ${path} — author against enterprise_sim.authoring directly.)`
  }
  try {
    return readFileSync(path, 'utf-8')
  } catch (e) {
    return `(failed to read skills/author-playbook/SKILL.md: ${(e as Error).message})`
  }
}

function buildSystemPrompt(ctx: TemplateChatContext): string {
  const relTemplatesDir = ctx.templatesDir.startsWith(ctx.repoRoot)
    ? ctx.templatesDir.slice(ctx.repoRoot.length).replace(/^[/\\]/, '')
    : ctx.templatesDir
  const validateCmd = `uv run enterprise-sim templates validate --dir ${ctx.templatesDir} --slug ${ctx.slug}`
  return `You are authoring a SimEnterprise TEMPLATE: an external plugin that adds a new
department archetype and/or scenario playbook, living OUTSIDE the package (so it
can be created, validated and deleted without touching the codebase).

TEMPLATE LAYOUT — this template lives at \`${relTemplatesDir}/${ctx.slug}/\`:
  template.json        metadata (already written by scaffold; do not hand-edit
                        "slug" or "kind" — everything else, including
                        "description", is yours to refine)
  plugin.py             registers ARCHETYPES / PLAYBOOKS / PROCESSES on import —
                        same shape as ${BUILD_SOFTWARE_HINT} and
                        enterprise_sim/archetypes/engineering.py
  test_<slug>.py        pytest: run_playbook + assert_conforms + check_playbook
                        (+ a golden snapshot once the shape is settled)

VALIDATE with:
  ${validateCmd}
Run it after every substantive change and read its JSON report: six steps
(import, lint, conformance, archetype_sanity, tests, dry_run), each
{"step","ok","detail"}. Iterate until "ok": true for the whole report. A failing
"lint" or "conformance" step's "detail" tells you exactly what to fix.

HARD RULE: you may only write or edit files under \`${relTemplatesDir}/${ctx.slug}/\`.
NEVER touch \`enterprise_sim/**\` — if the built-in engine seems to be missing a
primitive you need, you are almost certainly missing a pattern from the skill
below; reread it rather than reaching into engine internals.

Reference patterns worth reading for examples in the exact shape to copy:
  - ${PATTERNS_HINT}       three worked playbooks (build_software, sell_merchandise, run_clinical_study)
  - ${BUILD_SOFTWARE_HINT}  a built-in playbook module in the exact shape plugin.py should follow

When you're done (report is "ok": true), end with a short summary of what the
template does and what you validated.

---- skills/author-playbook/SKILL.md (authoritative model + validation loop) ----

${readSkill(ctx.repoRoot)}`
}

/** Full text of a tool_result's content, for JSON parsing (not truncated). */
function resultText(content: unknown): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((c) => (typeof c === 'object' && c && 'text' in c ? String((c as { text: unknown }).text) : ''))
      .join('')
  }
  return JSON.stringify(content)
}

function previewToolResult(content: unknown): string {
  const text = resultText(content)
  return text.length > 400 ? text.slice(0, 400) + '…' : text
}

/** True iff `looksLike` an object with the validate report's top-level shape. */
function isValidationReport(v: unknown): v is ValidationReport {
  return (
    typeof v === 'object' &&
    v !== null &&
    typeof (v as Record<string, unknown>).slug === 'string' &&
    typeof (v as Record<string, unknown>).ok === 'boolean' &&
    Array.isArray((v as Record<string, unknown>).steps)
  )
}

/**
 * Scan a Bash tool result's text for the `templates validate` JSON report.
 * The CLI prints exactly one JSON document to stdout and a human line to
 * stderr; a Bash tool's captured output may interleave both, so this tries
 * every line (last match wins) rather than assuming stdout is isolated.
 */
function extractValidationReport(text: string): ValidationReport | null {
  let found: ValidationReport | null = null
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed.startsWith('{')) continue
    try {
      const parsed = JSON.parse(trimmed)
      if (isValidationReport(parsed)) found = parsed
    } catch {
      /* not JSON, or not this line */
    }
  }
  return found
}

function isTemplatesValidateCommand(input: unknown): boolean {
  if (!input || typeof input !== 'object') return false
  const command = (input as Record<string, unknown>).command
  return typeof command === 'string' && command.includes('templates validate')
}

/**
 * Run one template-authoring turn. Streams {@link TemplateAgentEvent}s via
 * `onEvent`. Returns the session id (via the `done` event) so the caller can
 * pass it as `resume` on the next turn for multi-turn memory, keyed by
 * conversationId — same pattern as the Explore chat's `chatSessions` map.
 */
export async function runTemplateChat(
  ctx: TemplateChatContext,
  opts: {
    prompt: string
    model?: string
    resume?: string | null
    signal?: AbortSignal
    onEvent: (e: TemplateAgentEvent) => void
  }
): Promise<void> {
  const guardCtx: GuardContext = { repoRoot: ctx.repoRoot, templatesDir: ctx.templatesDir, slug: ctx.slug }

  let sessionId: string | null = opts.resume ?? null
  // toolUseId -> true for a Bash call whose command mentions "templates validate";
  // resolved against the matching tool_result to emit a `template` event.
  const pendingValidate = new Set<string>()

  const q = query({
    prompt: opts.prompt,
    options: {
      model: opts.model ?? 'sonnet',
      cwd: ctx.repoRoot,
      env: { ...process.env, ENTERPRISE_SIM_PLUGIN_PATH: ctx.templatesDir },
      systemPrompt: buildSystemPrompt(ctx),
      allowedTools: ['Read', 'Glob', 'Grep', 'Write', 'Edit', 'Bash'],
      permissionMode: 'bypassPermissions',
      canUseTool: async (toolName, input) => {
        const d = decide(toolName, input, guardCtx)
        return d.allow ? { behavior: 'allow', updatedInput: input } : { behavior: 'deny', message: d.reason ?? 'denied' }
      },
      maxTurns: 40,
      ...(opts.resume ? { resume: opts.resume } : {})
    }
  })

  if (opts.signal) {
    opts.signal.addEventListener('abort', () => {
      void q.interrupt().catch(() => {})
    })
  }

  try {
    for await (const msg of q) {
      if (opts.signal?.aborted) break
      switch (msg.type) {
        case 'system':
          if ('session_id' in msg && msg.session_id) sessionId = msg.session_id
          break
        case 'assistant': {
          for (const block of msg.message.content) {
            if (block.type === 'text' && block.text) {
              opts.onEvent({ kind: 'text', text: block.text })
            } else if (block.type === 'thinking' && 'thinking' in block && block.thinking) {
              opts.onEvent({ kind: 'thinking', text: String(block.thinking) })
            } else if (block.type === 'tool_use') {
              if (block.name === 'Bash' && isTemplatesValidateCommand(block.input)) pendingValidate.add(block.id)
              opts.onEvent({ kind: 'tool_use', id: block.id, name: block.name, input: block.input })
            }
          }
          break
        }
        case 'user': {
          const content = msg.message.content
          if (Array.isArray(content)) {
            for (const block of content) {
              if (typeof block === 'object' && block && 'type' in block && block.type === 'tool_result') {
                const tr = block as { tool_use_id: string; is_error?: boolean; content: unknown }
                opts.onEvent({ kind: 'tool_result', id: tr.tool_use_id, ok: !tr.is_error, preview: previewToolResult(tr.content) })
                if (pendingValidate.has(tr.tool_use_id)) {
                  pendingValidate.delete(tr.tool_use_id)
                  const report = extractValidationReport(resultText(tr.content))
                  if (report) opts.onEvent({ kind: 'template', slug: ctx.slug, validation: report })
                }
              }
            }
          }
          break
        }
        case 'result': {
          if ('session_id' in msg && msg.session_id) sessionId = msg.session_id
          const usage = 'usage' in msg ? msg.usage : undefined
          opts.onEvent({ kind: 'done', sessionId, usage })
          return
        }
      }
    }
    opts.onEvent({ kind: 'done', sessionId })
  } catch (e) {
    opts.onEvent({ kind: 'error', message: (e as Error).message })
    opts.onEvent({ kind: 'done', sessionId })
  }
}
