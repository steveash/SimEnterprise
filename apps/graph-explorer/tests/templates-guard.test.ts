import { describe, it, expect } from 'vitest'
import { decide, type GuardContext } from '../src/sidecar/templates/guard.js'

const ctx: GuardContext = {
  repoRoot: '/repo',
  templatesDir: '/repo/templates',
  slug: 'clinical_trials'
}

describe('templates canUseTool guard: allow/deny matrix', () => {
  // -- Read -------------------------------------------------------------- //
  it('allows Read with no file_path (defaults to cwd)', () => {
    expect(decide('Read', {}, ctx)).toEqual({ allow: true })
  })
  it('allows Read anywhere under the repo root', () => {
    expect(decide('Read', { file_path: '/repo/enterprise_sim/authoring/patterns.py' }, ctx).allow).toBe(true)
    expect(decide('Read', { file_path: '/repo/skills/author-playbook/SKILL.md' }, ctx).allow).toBe(true)
    expect(decide('Read', { file_path: '/repo' }, ctx).allow).toBe(true)
  })
  it('denies Read outside the repo root', () => {
    const d = decide('Read', { file_path: '/etc/passwd' }, ctx)
    expect(d.allow).toBe(false)
    expect(d.reason).toMatch(/repo/)
  })
  it('denies Read via path traversal out of the repo', () => {
    expect(decide('Read', { file_path: '/repo/../secrets.txt' }, ctx).allow).toBe(false)
  })

  // -- Glob / Grep --------------------------------------------------------- //
  it('allows Glob/Grep with no path', () => {
    expect(decide('Glob', { pattern: '**/*.py' }, ctx).allow).toBe(true)
    expect(decide('Grep', { pattern: 'foo' }, ctx).allow).toBe(true)
  })
  it('allows Glob/Grep scoped under the repo', () => {
    expect(decide('Glob', { pattern: '*.py', path: '/repo/templates' }, ctx).allow).toBe(true)
    expect(decide('Grep', { pattern: 'foo', path: '/repo/enterprise_sim' }, ctx).allow).toBe(true)
  })
  it('denies Glob/Grep outside the repo', () => {
    expect(decide('Glob', { pattern: '*', path: '/tmp' }, ctx).allow).toBe(false)
    expect(decide('Grep', { pattern: 'x', path: '/' }, ctx).allow).toBe(false)
  })

  // -- Write / Edit -------------------------------------------------------- //
  it('allows Write/Edit under templates/<slug>/', () => {
    expect(decide('Write', { file_path: '/repo/templates/clinical_trials/plugin.py', content: '' }, ctx).allow).toBe(
      true
    )
    expect(
      decide(
        'Edit',
        { file_path: '/repo/templates/clinical_trials/test_clinical_trials.py', old_string: 'a', new_string: 'b' },
        ctx
      ).allow
    ).toBe(true)
  })
  it('allows Write at the slug directory root itself (template.json)', () => {
    expect(decide('Write', { file_path: '/repo/templates/clinical_trials/template.json', content: '{}' }, ctx).allow).toBe(
      true
    )
  })
  it('denies Write/Edit to a sibling template', () => {
    const d = decide('Write', { file_path: '/repo/templates/other_slug/plugin.py', content: '' }, ctx)
    expect(d.allow).toBe(false)
    expect(d.reason).toMatch(/clinical_trials/)
  })
  it('denies Write/Edit to enterprise_sim/**', () => {
    expect(decide('Write', { file_path: '/repo/enterprise_sim/core/registry/__init__.py', content: '' }, ctx).allow).toBe(
      false
    )
    expect(
      decide('Edit', { file_path: '/repo/enterprise_sim/playbooks/build_software.py', old_string: 'a', new_string: 'b' }, ctx)
        .allow
    ).toBe(false)
  })
  it('denies Write with no file_path', () => {
    expect(decide('Write', { content: 'x' }, ctx).allow).toBe(false)
  })
  it('denies Write via path traversal out of the template dir', () => {
    expect(
      decide('Write', { file_path: '/repo/templates/clinical_trials/../other_slug/plugin.py', content: '' }, ctx).allow
    ).toBe(false)
  })

  // -- Bash ------------------------------------------------------------------ //
  it('allows the exact allow-listed Bash prefixes', () => {
    expect(
      decide(
        'Bash',
        { command: 'uv run enterprise-sim templates validate --dir /repo/templates --slug clinical_trials' },
        ctx
      ).allow
    ).toBe(true)
    expect(decide('Bash', { command: 'uv run enterprise-sim lint clinical_trials_playbook' }, ctx).allow).toBe(true)
    expect(decide('Bash', { command: 'uv run pytest templates/clinical_trials -q' }, ctx).allow).toBe(true)
    expect(decide('Bash', { command: 'uv run python -c "import json; print(1)"' }, ctx).allow).toBe(true)
  })
  it('scopes the pytest prefix to the current slug only', () => {
    expect(decide('Bash', { command: 'uv run pytest templates/other_slug -q' }, ctx).allow).toBe(false)
  })
  it('denies an arbitrary shell command', () => {
    const d = decide('Bash', { command: 'rm -rf /' }, ctx)
    expect(d.allow).toBe(false)
    expect(d.reason).toContain('rm -rf /')
  })
  it('denies a command that merely contains an allowed prefix but does not start with it', () => {
    expect(decide('Bash', { command: 'echo hi && uv run enterprise-sim templates validate' }, ctx).allow).toBe(false)
  })
  it('denies Bash with no command', () => {
    expect(decide('Bash', {}, ctx).allow).toBe(false)
  })

  // -- everything else -------------------------------------------------------- //
  it('denies any tool not in {Read,Glob,Grep,Write,Edit,Bash}', () => {
    expect(decide('WebFetch', { url: 'https://example.com' }, ctx).allow).toBe(false)
    expect(decide('Task', {}, ctx).allow).toBe(false)
  })
})
