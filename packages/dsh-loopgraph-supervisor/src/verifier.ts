import { execFile } from 'node:child_process'

export interface CommandEvidence {
  readonly command: string
  readonly exitCode: number
  readonly passed: boolean
  readonly stdout: string
  readonly stderr: string
}

function runCommand(cwd: string, command: string): Promise<CommandEvidence> {
  return new Promise((resolve) => {
    execFile('/bin/sh', ['-lc', command], { cwd, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, timeout: 300_000 }, (error, stdout, stderr) => {
      const exitCode = typeof error?.code === 'number' ? error.code : error ? 1 : 0
      resolve({ command, exitCode, passed: exitCode === 0, stdout: stdout.slice(-4000), stderr: stderr.slice(-4000) })
    })
  })
}

export async function verifyCommands(cwd: string, commands: readonly string[]): Promise<{ passed: boolean; evidence: CommandEvidence[] }> {
  if (commands.length === 0) return { passed: false, evidence: [] }
  const evidence: CommandEvidence[] = []
  for (const command of commands) evidence.push(await runCommand(cwd, command))
  return { passed: evidence.every(item => item.passed), evidence }
}
