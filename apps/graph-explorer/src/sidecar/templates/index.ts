// Templates feature ops (docs/EXPLORER_TEMPLATES.md §3). Registers itself with
// the sidecar's feature-op registry (`../ops.js`) on import — wired in via the
// one-line import in `../features.ts`.
//
// Templates dir resolution: `GRAPH_EXPLORER_TEMPLATES_DIR` if set, else
// `<repoRoot>/templates` (docs/EXPLORER.md §4), created lazily. Every spawned
// Python process (CLI calls here, and Bash calls the authoring agent runs)
// gets `ENTERPRISE_SIM_PLUGIN_PATH` set to that same directory, so a template's
// own archetype/playbook/process names resolve during `templates validate`,
// `templates catalog`, and the dry-run step.
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { registerOps, type OpContext, type OpHandler } from '../ops.js'
import { parseCliJson, runTemplatesCli } from './cli.js'
import { runTemplateChat } from './harness.js'
import type {
  Template,
  TemplateListEntry,
  TemplatesCatalog,
  TemplatesReadResult,
  ValidationReport
} from '../../shared/templates-protocol.js'

/** request id (of a `templatesChat` call) -> its AbortController. */
const chatAborts = new Map<string, AbortController>()
/** `${slug}:${conversationId}` -> SDK session id, for multi-turn `resume`. */
const chatSessions = new Map<string, string>()

function resolveTemplatesDir(repoRoot: string): string {
  const dir = process.env.GRAPH_EXPLORER_TEMPLATES_DIR || join(repoRoot, 'templates')
  try {
    mkdirSync(dir, { recursive: true })
  } catch {
    /* best-effort; a real failure surfaces on the next fs op below */
  }
  return dir
}

function pluginPathEnv(templatesDir: string): NodeJS.ProcessEnv {
  return { ENTERPRISE_SIM_PLUGIN_PATH: templatesDir }
}

function requireString(v: unknown, name: string): string {
  if (typeof v !== 'string' || !v) throw new Error(`${name} is required`)
  return v
}

async function opTemplatesList(ctx: OpContext): Promise<TemplateListEntry[]> {
  const dir = resolveTemplatesDir(ctx.repoRoot)
  const args = ['templates', 'list', '--dir', dir]
  const result = await runTemplatesCli(ctx.repoRoot, args, pluginPathEnv(dir))
  if (result.code !== 0) throw new Error(`enterprise-sim templates list exited ${result.code}: ${result.stderr.trim()}`)
  return parseCliJson<TemplateListEntry[]>(args, result)
}

async function opTemplatesScaffold(ctx: OpContext): Promise<Template> {
  const slug = requireString(ctx.params.slug, 'slug')
  const kind = requireString(ctx.params.kind, 'kind')
  const name = requireString(ctx.params.name, 'name')
  const description = typeof ctx.params.description === 'string' ? ctx.params.description : ''
  const dir = resolveTemplatesDir(ctx.repoRoot)
  const args = [
    'templates',
    'scaffold',
    '--dir',
    dir,
    '--slug',
    slug,
    '--kind',
    kind,
    '--name',
    name,
    '--description',
    description
  ]
  const result = await runTemplatesCli(ctx.repoRoot, args, pluginPathEnv(dir))
  if (result.code !== 0) {
    let message = `enterprise-sim templates scaffold exited ${result.code}`
    try {
      const parsed = JSON.parse(result.stdout) as { error?: string }
      if (parsed.error) message = parsed.error
    } catch {
      if (result.stderr.trim()) message = result.stderr.trim()
    }
    throw new Error(message)
  }
  return parseCliJson<Template>(args, result)
}

async function opTemplatesValidate(ctx: OpContext): Promise<ValidationReport> {
  const slug = requireString(ctx.params.slug, 'slug')
  const dir = resolveTemplatesDir(ctx.repoRoot)
  const args = ['templates', 'validate', '--dir', dir, '--slug', slug]
  // Exit code is 0 (valid) or 1 (invalid) for a normal report — both print a
  // full JSON report on stdout, so parse first and only treat a genuinely
  // unparsable stdout as a hard failure.
  const result = await runTemplatesCli(ctx.repoRoot, args, pluginPathEnv(dir))
  try {
    return JSON.parse(result.stdout) as ValidationReport
  } catch {
    throw new Error(
      `enterprise-sim templates validate exited ${result.code} with unparseable output: ${result.stderr.trim()}`
    )
  }
}

async function opTemplatesDelete(ctx: OpContext): Promise<{ ok: true; slug: string }> {
  const slug = requireString(ctx.params.slug, 'slug')
  const dir = resolveTemplatesDir(ctx.repoRoot)
  const args = ['templates', 'delete', '--dir', dir, '--slug', slug]
  const result = await runTemplatesCli(ctx.repoRoot, args, pluginPathEnv(dir))
  if (result.code !== 0) {
    let message = `enterprise-sim templates delete exited ${result.code}`
    try {
      const parsed = JSON.parse(result.stdout) as { error?: string }
      if (parsed.error) message = parsed.error
    } catch {
      if (result.stderr.trim()) message = result.stderr.trim()
    }
    throw new Error(message)
  }
  return { ok: true, slug }
}

/** Files worth showing the file viewer, in a stable order. */
const HIDDEN_PREFIXES = ['.', '__pycache__']

async function opTemplatesRead(ctx: OpContext): Promise<TemplatesReadResult> {
  const slug = requireString(ctx.params.slug, 'slug')
  const dir = resolveTemplatesDir(ctx.repoRoot)
  const templateDir = join(dir, slug)
  if (!existsSync(templateDir) || !statSync(templateDir).isDirectory()) {
    throw new Error(`no template ${JSON.stringify(slug)} under ${dir}`)
  }
  const names = readdirSync(templateDir)
    .filter((n) => !HIDDEN_PREFIXES.some((p) => n.startsWith(p)))
    .filter((n) => statSync(join(templateDir, n)).isFile())
    .sort()
  const files = names.map((path) => ({ path, content: readFileSync(join(templateDir, path), 'utf-8') }))
  return { slug, files }
}

async function opTemplatesCatalog(ctx: OpContext): Promise<TemplatesCatalog> {
  const dir = resolveTemplatesDir(ctx.repoRoot)
  const args = ['templates', 'catalog', '--dir', dir]
  const result = await runTemplatesCli(ctx.repoRoot, args, pluginPathEnv(dir))
  if (result.code !== 0) throw new Error(`enterprise-sim templates catalog exited ${result.code}: ${result.stderr.trim()}`)
  return parseCliJson<TemplatesCatalog>(args, result)
}

async function opTemplatesChat(ctx: OpContext): Promise<{ finished: true }> {
  const slug = requireString(ctx.params.slug, 'slug')
  const prompt = requireString(ctx.params.prompt, 'prompt')
  const model = typeof ctx.params.model === 'string' ? ctx.params.model : undefined
  const conversationId = requireString(ctx.params.conversationId, 'conversationId')
  const dir = resolveTemplatesDir(ctx.repoRoot)
  const convoKey = `${slug}:${conversationId}`
  const ac = new AbortController()
  chatAborts.set(ctx.requestId, ac)
  try {
    await runTemplateChat(
      { repoRoot: ctx.repoRoot, templatesDir: dir, slug },
      {
        prompt,
        model,
        resume: chatSessions.get(convoKey) ?? null,
        signal: ac.signal,
        onEvent: (event) => {
          if (event.kind === 'done' && event.sessionId) chatSessions.set(convoKey, event.sessionId)
          ctx.stream(event)
        }
      }
    )
  } finally {
    chatAborts.delete(ctx.requestId)
  }
  return { finished: true }
}

async function opTemplatesCancelChat(ctx: OpContext): Promise<{ cancelled: true }> {
  const id = requireString(ctx.params.id, 'id')
  chatAborts.get(id)?.abort()
  return { cancelled: true }
}

const ops: Record<string, OpHandler> = {
  templatesList: opTemplatesList,
  templatesScaffold: opTemplatesScaffold,
  templatesValidate: opTemplatesValidate,
  templatesDelete: opTemplatesDelete,
  templatesRead: opTemplatesRead,
  templatesCatalog: opTemplatesCatalog,
  templatesChat: opTemplatesChat,
  templatesCancelChat: opTemplatesCancelChat
}

registerOps(ops)

export { resolveTemplatesDir }
