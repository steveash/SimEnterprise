// The Python <-> sidecar bridge (docs/EXPLORER.md §3).
//
// Every UI-facing Python entry point is an `enterprise-sim` subcommand that
// speaks JSON: one-shot commands print a single JSON document, long-running
// ones stream JSON Lines on stdout. This module resolves how to invoke the CLI
// and offers the two call shapes. It holds no state.
import { spawn, type ChildProcess } from 'node:child_process'
import { existsSync } from 'node:fs'
import { join } from 'node:path'

export interface PythonCommand {
  cmd: string
  args: string[]
}

/** Split a command prefix on whitespace, honouring simple double/single quotes. */
export function splitCommand(prefix: string): string[] {
  const out: string[] = []
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(prefix)) !== null) out.push(m[1] ?? m[2] ?? m[3] ?? '')
  return out
}

/**
 * Resolve the `enterprise-sim` invocation:
 *   1. GRAPH_EXPLORER_PYTHON_CMD (a command prefix, e.g. "uv run --project /repo enterprise-sim");
 *   2. `uv run --project <repoRoot> enterprise-sim` when the repo root has a pyproject.toml;
 *   3. `enterprise-sim` on PATH.
 */
export function resolvePython(
  repoRoot: string,
  env: NodeJS.ProcessEnv = process.env,
  exists: (p: string) => boolean = existsSync
): PythonCommand {
  const override = env.GRAPH_EXPLORER_PYTHON_CMD?.trim()
  if (override) {
    const parts = splitCommand(override)
    if (parts.length > 0) return { cmd: parts[0], args: parts.slice(1) }
  }
  if (exists(join(repoRoot, 'pyproject.toml'))) {
    return { cmd: 'uv', args: ['run', '--project', repoRoot, 'enterprise-sim'] }
  }
  return { cmd: 'enterprise-sim', args: [] }
}

/** Build the full argv for `enterprise-sim <args>`. */
export function pythonCommand(repoRoot: string, args: string[]): PythonCommand {
  const base = resolvePython(repoRoot)
  return { cmd: base.cmd, args: [...base.args, ...args] }
}

export interface SpawnOptions {
  cwd?: string
  env?: NodeJS.ProcessEnv
}

export class PythonError extends Error {
  constructor(
    message: string,
    public readonly code: number | null,
    public readonly stderr: string
  ) {
    super(message)
  }
}

/** Run a one-shot JSON command to completion and parse its stdout. */
export function runJson<T = unknown>(repoRoot: string, args: string[], opts: SpawnOptions = {}): Promise<T> {
  const { cmd, args: argv } = pythonCommand(repoRoot, args)
  return new Promise<T>((resolve, reject) => {
    const child = spawn(cmd, argv, {
      cwd: opts.cwd ?? repoRoot,
      env: { ...process.env, ...opts.env },
      stdio: ['ignore', 'pipe', 'pipe']
    })
    let out = ''
    let err = ''
    child.stdout.on('data', (b: Buffer) => (out += b.toString()))
    child.stderr.on('data', (b: Buffer) => (err += b.toString()))
    child.on('error', (e) => reject(new PythonError(`${cmd} failed to start: ${e.message}`, null, err)))
    child.on('close', (code) => {
      if (code !== 0) return reject(new PythonError(`enterprise-sim ${args[0] ?? ''} exited ${code}: ${err.trim()}`, code, err))
      try {
        resolve(JSON.parse(out) as T)
      } catch (e) {
        reject(new PythonError(`enterprise-sim ${args[0] ?? ''} printed non-JSON: ${(e as Error).message}`, code, err))
      }
    })
  })
}

export interface StreamHandle {
  pid: number | undefined
  child: ChildProcess
  /** Resolves with the exit code once the process ends (null if killed by a signal). */
  done: Promise<number | null>
  kill: (signal?: NodeJS.Signals) => void
}

/**
 * Parse a stream of JSON Lines from chunked data. Returns a feeder; every
 * complete line is parsed and handed to `onLine` (malformed lines go to `onJunk`).
 */
export function jsonlParser(onLine: (obj: unknown) => void, onJunk?: (line: string) => void): (chunk: string) => void {
  let buf = ''
  return (chunk: string) => {
    buf += chunk
    let nl: number
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim()
      buf = buf.slice(nl + 1)
      if (!line) continue
      try {
        onLine(JSON.parse(line))
      } catch {
        onJunk?.(line)
      }
    }
  }
}

/** Spawn a streaming command; `onLine` receives each parsed JSONL object. */
export function streamJsonl(
  repoRoot: string,
  args: string[],
  onLine: (obj: unknown) => void,
  opts: SpawnOptions & { onStderr?: (text: string) => void; onJunk?: (line: string) => void } = {}
): StreamHandle {
  const { cmd, args: argv } = pythonCommand(repoRoot, args)
  const child = spawn(cmd, argv, {
    cwd: opts.cwd ?? repoRoot,
    env: { ...process.env, ...opts.env },
    stdio: ['ignore', 'pipe', 'pipe']
  })
  const feed = jsonlParser(onLine, opts.onJunk)
  child.stdout?.on('data', (b: Buffer) => feed(b.toString()))
  child.stderr?.on('data', (b: Buffer) => opts.onStderr?.(b.toString()))
  const done = new Promise<number | null>((resolve) => {
    child.on('error', () => resolve(null))
    child.on('close', (code) => resolve(code))
  })
  return { pid: child.pid, child, done, kill: (signal = 'SIGTERM') => child.kill(signal) }
}
