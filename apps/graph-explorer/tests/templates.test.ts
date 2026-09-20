import { describe, it, expect, beforeAll, afterAll } from 'vitest'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'
import { getOp, type OpContext } from '../src/sidecar/ops.js'
import '../src/sidecar/templates/index.js' // registers the templates* ops
import { runTemplateChat } from '../src/sidecar/templates/harness.js'
import { resolvePython } from '../src/sidecar/python.js'
import type { Template, TemplateListEntry, TemplatesCatalog, ValidationReport } from '../src/shared/templates-protocol.js'

// Repo root: two levels above this app (pyproject.toml lives there).
const REPO_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..', '..')

/** True when `uv run enterprise-sim --help` actually runs — skip-guard for the CLI tests. */
function uvAvailable(): boolean {
  try {
    const { cmd, args } = resolvePython(REPO_ROOT)
    execFileSync(cmd, [...args, '--help'], { cwd: REPO_ROOT, stdio: 'ignore', timeout: 30_000 })
    return true
  } catch {
    return false
  }
}

const cliEnabled = uvAvailable()

function ctxFor(templatesRoot: string, params: Record<string, unknown>): OpContext {
  return {
    requestId: 'test',
    params,
    ensureLoaded: async () => {
      throw new Error('unused')
    },
    stream: () => {},
    runsRoot: '/unused',
    repoRoot: REPO_ROOT
  }
}

describe.skipIf(!cliEnabled)('templates ops: end-to-end through the real Python CLI', () => {
  let templatesRoot: string

  beforeAll(() => {
    templatesRoot = mkdtempSync(join(tmpdir(), 'esim-templates-'))
  })
  afterAll(() => {
    rmSync(templatesRoot, { recursive: true, force: true })
  })

  it('templatesList on an empty/missing dir is []', async () => {
    process.env.GRAPH_EXPLORER_TEMPLATES_DIR = join(templatesRoot, 'nope')
    const handler = getOp('templatesList')!
    const out = (await handler(ctxFor(templatesRoot, {}))) as TemplateListEntry[]
    expect(out).toEqual([])
  })

  it('scaffold -> validate is green and fills provides (bundle kind)', async () => {
    process.env.GRAPH_EXPLORER_TEMPLATES_DIR = templatesRoot
    const scaffold = getOp('templatesScaffold')!
    const template = (await scaffold(
      ctxFor(templatesRoot, { slug: 'demo_bundle', kind: 'bundle', name: 'Demo Bundle', description: 'a demo' })
    )) as Template
    expect(template.slug).toBe('demo_bundle')
    expect(template.validation.status).toBe('unvalidated')

    const validate = getOp('templatesValidate')!
    const report = (await validate(ctxFor(templatesRoot, { slug: 'demo_bundle' }))) as ValidationReport
    expect(report.ok, JSON.stringify(report.steps, null, 2)).toBe(true)
    expect(report.provides.archetypes).toContain('demo_bundle')
    expect(report.provides.playbooks).toContain('demo_bundle_playbook')
    for (const step of report.steps) expect(step.ok, `${step.step}: ${JSON.stringify(step.detail)}`).toBe(true)
  }, 60_000)

  it('templatesList now reports the scaffolded template as valid', async () => {
    const list = getOp('templatesList')!
    const out = (await list(ctxFor(templatesRoot, {}))) as TemplateListEntry[]
    const entry = out.find((e) => e.slug === 'demo_bundle')
    expect(entry).toBeDefined()
    expect(entry!.loadable).toBe(true)
    if (entry!.loadable) expect(entry!.validation.status).toBe('valid')
  })

  it('templatesRead returns the scaffolded files with contents', async () => {
    const read = getOp('templatesRead')!
    const out = (await read(ctxFor(templatesRoot, { slug: 'demo_bundle' }))) as { slug: string; files: { path: string }[] }
    const paths = out.files.map((f) => f.path).sort()
    expect(paths).toEqual(['plugin.py', 'template.json', 'test_demo_bundle.py'])
  })

  it('templatesCatalog includes the scaffolded template, attributed to it', async () => {
    const catalog = getOp('templatesCatalog')!
    const out = (await catalog(ctxFor(templatesRoot, {}))) as TemplatesCatalog
    const arch = out.archetypes.find((a) => a.name === 'demo_bundle')
    expect(arch?.source).toBe('template:demo_bundle')
  }, 30_000)

  it('an intentionally broken template reports the failing step without throwing', async () => {
    const scaffold = getOp('templatesScaffold')!
    await scaffold(ctxFor(templatesRoot, { slug: 'broken_one', kind: 'playbook', name: 'Broken', description: '' }))

    // Corrupt the plugin so import blows up.
    const { writeFileSync } = await import('node:fs')
    writeFileSync(join(templatesRoot, 'broken_one', 'plugin.py'), 'raise RuntimeError("nope")\n')

    const validate = getOp('templatesValidate')!
    const report = (await validate(ctxFor(templatesRoot, { slug: 'broken_one' }))) as ValidationReport
    expect(report.ok).toBe(false)
    expect(report.steps).toHaveLength(1)
    expect(report.steps[0].step).toBe('import')
    expect(report.steps[0].ok).toBe(false)
  }, 30_000)

  it('templatesDelete removes the template directory', async () => {
    const del = getOp('templatesDelete')!
    const out = (await del(ctxFor(templatesRoot, { slug: 'broken_one' }))) as { ok: true; slug: string }
    expect(out).toEqual({ ok: true, slug: 'broken_one' })
    const list = getOp('templatesList')!
    const after = (await list(ctxFor(templatesRoot, {}))) as TemplateListEntry[]
    expect(after.some((e) => e.slug === 'broken_one')).toBe(false)
  })

  it('templatesDelete on a missing slug throws', async () => {
    const del = getOp('templatesDelete')!
    await expect(del(ctxFor(templatesRoot, { slug: 'never_existed' }))).rejects.toThrow()
  })

  afterAll(() => {
    delete process.env.GRAPH_EXPLORER_TEMPLATES_DIR
  })
})

// ---------------------------------------------------------------------------
// GATED: a real live authoring-agent turn. Runs only with ANTHROPIC_API_KEY
// (and a working Python CLI, since the agent's Bash tool needs it) present.
// ---------------------------------------------------------------------------

const liveEnabled = !!process.env.ANTHROPIC_API_KEY && cliEnabled

describe.skipIf(!liveEnabled)('template authoring agent (gated on ANTHROPIC_API_KEY)', () => {
  it(
    'scaffolds nothing itself but can read the skill and reach done',
    async () => {
      const templatesRoot = mkdtempSync(join(tmpdir(), 'esim-templates-live-'))
      try {
        // Pre-scaffold via the CLI op so the agent has something to validate.
        process.env.GRAPH_EXPLORER_TEMPLATES_DIR = templatesRoot
        const scaffoldOp = getOp('templatesScaffold')!
        await scaffoldOp(ctxFor(templatesRoot, { slug: 'live_demo', kind: 'playbook', name: 'Live Demo', description: 'x' }))

        const events: unknown[] = []
        await runTemplateChat(
          { repoRoot: REPO_ROOT, templatesDir: templatesRoot, slug: 'live_demo' },
          {
            prompt: 'Run the validate command once and summarize the result in one sentence. Do not edit any files.',
            model: 'haiku',
            onEvent: (e) => events.push(e)
          }
        )

        expect((events as { kind: string }[]).some((e) => e.kind === 'done')).toBe(true)
      } finally {
        delete process.env.GRAPH_EXPLORER_TEMPLATES_DIR
        rmSync(templatesRoot, { recursive: true, force: true })
      }
    },
    120_000
  )
})
