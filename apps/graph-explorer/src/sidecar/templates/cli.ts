// A small `enterprise-sim templates …` runner. Reuses `pythonCommand` from
// `../python.js` (the shared bridge, docs/EXPLORER.md §3) but doesn't use its
// `runJson` directly: `templates validate` legitimately exits 1 for an INVALID
// template while still printing a well-formed report on stdout, and `runJson`
// treats a non-zero exit as a hard failure. This captures stdout/stderr/code
// unconditionally and lets each op decide what a given exit code means.
import { spawn } from 'node:child_process'
import { pythonCommand } from '../python.js'

export interface CliResult {
  code: number | null
  stdout: string
  stderr: string
}

export function runTemplatesCli(repoRoot: string, args: string[], env?: NodeJS.ProcessEnv): Promise<CliResult> {
  const { cmd, args: argv } = pythonCommand(repoRoot, args)
  return new Promise<CliResult>((resolvePromise, reject) => {
    const child = spawn(cmd, argv, {
      cwd: repoRoot,
      env: { ...process.env, ...env },
      stdio: ['ignore', 'pipe', 'pipe']
    })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (b: Buffer) => (stdout += b.toString()))
    child.stderr.on('data', (b: Buffer) => (stderr += b.toString()))
    child.on('error', (e) => reject(new Error(`${cmd} failed to start: ${e.message}`)))
    child.on('close', (code) => resolvePromise({ code, stdout, stderr }))
  })
}

/** Parse `result.stdout` as JSON, with a message naming the command on failure. */
export function parseCliJson<T = unknown>(args: string[], result: CliResult): T {
  try {
    return JSON.parse(result.stdout) as T
  } catch (e) {
    throw new Error(
      `enterprise-sim ${args.join(' ')} printed non-JSON (exit ${result.code}): ${(e as Error).message}\n` +
        `stderr: ${result.stderr.trim()}`
    )
  }
}
