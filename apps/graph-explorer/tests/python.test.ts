import { describe, expect, it } from 'vitest'
import { jsonlParser, resolvePython, splitCommand } from '../src/sidecar/python.js'
import { registerOps, getOp, registeredOps } from '../src/sidecar/ops.js'

describe('python bridge: command resolution', () => {
  it('honours GRAPH_EXPLORER_PYTHON_CMD as a command prefix', () => {
    const cmd = resolvePython('/repo', { GRAPH_EXPLORER_PYTHON_CMD: 'uv run --project "/my repo" enterprise-sim' }, () => true)
    expect(cmd).toEqual({ cmd: 'uv', args: ['run', '--project', '/my repo', 'enterprise-sim'] })
  })
  it('uses uv run --project when the repo has a pyproject', () => {
    expect(resolvePython('/repo', {}, (p) => p === '/repo/pyproject.toml')).toEqual({
      cmd: 'uv',
      args: ['run', '--project', '/repo', 'enterprise-sim']
    })
  })
  it('falls back to enterprise-sim on PATH', () => {
    expect(resolvePython('/repo', {}, () => false)).toEqual({ cmd: 'enterprise-sim', args: [] })
  })
  it('splits quoted prefixes', () => {
    expect(splitCommand(`a 'b c' "d e" f`)).toEqual(['a', 'b c', 'd e', 'f'])
  })
})

describe('python bridge: JSONL parsing', () => {
  it('reassembles objects across chunk boundaries and skips junk', () => {
    const got: unknown[] = []
    const junk: string[] = []
    const feed = jsonlParser((o) => got.push(o), (l) => junk.push(l))
    feed('{"a":1}\n{"b"')
    feed(':2}\nnot json\n\n{"c":3}')
    expect(got).toEqual([{ a: 1 }, { b: 2 }])
    expect(junk).toEqual(['not json'])
    feed('\n')
    expect(got).toEqual([{ a: 1 }, { b: 2 }, { c: 3 }])
  })
})

describe('feature op registry', () => {
  it('registers and looks up handlers', async () => {
    registerOps({ __test_op: async (ctx) => ({ echo: ctx.params.x }) })
    expect(registeredOps()).toContain('__test_op')
    const h = getOp('__test_op')!
    const out = await h({
      requestId: '1',
      params: { x: 42 },
      ensureLoaded: async () => {
        throw new Error('unused')
      },
      stream: () => {},
      runsRoot: '/runs',
      repoRoot: '/repo'
    })
    expect(out).toEqual({ echo: 42 })
    expect(getOp('nope')).toBeUndefined()
  })
})
