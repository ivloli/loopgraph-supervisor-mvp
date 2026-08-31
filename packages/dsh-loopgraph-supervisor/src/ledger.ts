import type { Agent } from '@deepseek-ai/dsh-agent'
import { loadLoopSpec, loopSpecHash, nodeForRole } from './loopspec.js'
import { createHash, randomUUID } from 'node:crypto'
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, statSync, truncateSync, unlinkSync, writeSync } from 'node:fs'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import type { DecisionRecord, LoopGraphEvent, LoopState } from './model.js'

interface StoredRecord {
  readonly time: number
  readonly sequence?: number
  readonly previousChecksum?: string
  readonly checksum?: string
  readonly event: LoopGraphEvent
}

interface ExclusiveLock {
  readonly descriptor: number
  readonly path: string
  readonly token: string
}

const lockWait = new Int32Array(new SharedArrayBuffer(4))

function ledgerPath(agent: Agent): string {
  const root = process.env.DSH_HOME ?? join(homedir(), '.dsh')
  const directory = join(root, 'loopgraph')
  if (!existsSync(root)) {
    mkdirSync(root, { recursive: true })
    fsyncDirectory(dirname(root))
  }
  if (!existsSync(directory)) {
    mkdirSync(directory)
    fsyncDirectory(root)
  }
  return join(directory, `${agent.id}.jsonl`)
}

export function appendLoopEvent(agent: Agent, event: LoopGraphEvent): void {
  const path = ledgerPath(agent)
  const lock = acquireExclusiveLock(`${path}.lock`, 'writer')
  try {
    repairTornTail(path)
    const records = readRecords(path)
    const previousChecksum = ledgerTipChecksum(records)
    const body = {
      time: Date.now(),
      sequence: records.length + 1,
      ...(previousChecksum ? { previousChecksum } : {}),
      event,
    }
    const record = { ...body, checksum: checksum(body) }
    const existed = existsSync(path)
    const descriptor = openSync(path, 'a', 0o600)
    try {
      writeAll(descriptor, `${JSON.stringify(record)}\n`)
      fsyncSync(descriptor)
    } finally {
      closeSync(descriptor)
    }
    if (!existed) fsyncDirectory(dirname(path))
  } finally {
    releaseExclusiveLock(lock)
  }
}

export async function withLoopOperationLock<T>(agent: Agent, operation: () => Promise<T>): Promise<T> {
  const lock = await acquireExclusiveLockAsync(`${ledgerPath(agent)}.operation.lock`, 'operation')
  try {
    return await operation()
  } finally {
    releaseExclusiveLock(lock)
  }
}

function repairTornTail(path: string): void {
  let content: string
  try {
    content = readFileSync(path, 'utf8')
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return
    throw error
  }
  if (!content || content.endsWith('\n')) return
  const boundary = content.lastIndexOf('\n') + 1
  try {
    JSON.parse(content.slice(boundary))
    const descriptor = openSync(path, 'a', 0o600)
    try {
      writeAll(descriptor, '\n')
      fsyncSync(descriptor)
    } finally {
      closeSync(descriptor)
    }
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error
    truncateSync(path, boundary)
    const descriptor = openSync(path, 'r', 0o600)
    try { fsyncSync(descriptor) } finally { closeSync(descriptor) }
  }
}

export function loadLoopEvents(agent: Agent): LoopGraphEvent[] {
  return readRecords(ledgerPath(agent)).map(record => record.event)
}

function readRecords(path: string): StoredRecord[] {
  try {
    const content = readFileSync(path, 'utf8')
    if (!content) return []
    const terminated = content.endsWith('\n')
    const lines = (terminated ? content.slice(0, -1) : content).split('\n')
    const records: StoredRecord[] = []
    let tip: string | undefined
    let checksummed = false
    for (const [index, line] of lines.entries()) {
      try {
        const record = JSON.parse(line) as StoredRecord
        validateRecord(record, index, tip, checksummed, records.at(-1))
        records.push(record)
        tip = record.checksum ?? checksum({ ...(tip ? { previousChecksum: tip } : {}), time: record.time, event: record.event })
        checksummed ||= record.checksum !== undefined
      } catch (error) {
        if (!terminated && index === lines.length - 1 && error instanceof SyntaxError) break
        throw error
      }
    }
    return records
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code
    if (code === 'ENOENT') return []
    throw error
  }
}

function validateRecord(record: StoredRecord, index: number, priorChecksum: string | undefined, checksummed: boolean, priorRecord?: StoredRecord): void {
  if (!record.event || typeof record.time !== 'number') throw new Error(`invalid loop ledger record at line ${index + 1}`)
  if (record.checksum === undefined && record.sequence === undefined) {
    if (checksummed) throw new Error(`legacy loop ledger record after checksum chain at line ${index + 1}`)
    return
  }
  if (record.sequence !== index + 1 || typeof record.checksum !== 'string') throw new Error(`invalid loop ledger sequence at line ${index + 1}`)
  const legacyImmediateChecksum = !checksummed && priorRecord
    ? checksum({ time: priorRecord.time, event: priorRecord.event })
    : undefined
  if (record.previousChecksum !== priorChecksum && record.previousChecksum !== legacyImmediateChecksum) throw new Error(`broken loop ledger checksum chain at line ${index + 1}`)
  const body = {
    time: record.time,
    sequence: record.sequence,
    ...(record.previousChecksum ? { previousChecksum: record.previousChecksum } : {}),
    event: record.event,
  }
  if (checksum(body) !== record.checksum) throw new Error(`loop ledger checksum mismatch at line ${index + 1}`)
}

function checksum(value: object): string {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex')
}

function ledgerTipChecksum(records: readonly StoredRecord[]): string | undefined {
  let tip: string | undefined
  for (const record of records) {
    tip = record.checksum ?? checksum({ ...(tip ? { previousChecksum: tip } : {}), time: record.time, event: record.event })
  }
  return tip
}

function acquireExclusiveLock(lockPath: string, label: string): ExclusiveLock {
  for (let attempt = 0; attempt < 500; attempt += 1) {
    const lock = tryAcquireExclusiveLock(lockPath)
    if (lock) return lock
    if (staleLock(lockPath) && reapStaleLock(lockPath)) continue
    Atomics.wait(lockWait, 0, 0, 10)
  }
  throw new Error(`timed out acquiring loop ledger ${label} lock: ${lockPath}`)
}

async function acquireExclusiveLockAsync(lockPath: string, label: string): Promise<ExclusiveLock> {
  for (let attempt = 0; attempt < 500; attempt += 1) {
    const lock = tryAcquireExclusiveLock(lockPath)
    if (lock) return lock
    if (staleLock(lockPath) && reapStaleLock(lockPath)) continue
    await new Promise(resolve => setTimeout(resolve, 10))
  }
  throw new Error(`timed out acquiring loop ledger ${label} lock: ${lockPath}`)
}

function tryAcquireExclusiveLock(lockPath: string): ExclusiveLock | undefined {
  let descriptor: number
  try {
    descriptor = openSync(lockPath, 'wx', 0o600)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') return undefined
    throw error
  }
  const token = randomUUID()
  try {
    writeAll(descriptor, JSON.stringify({ pid: process.pid, token, createdAt: Date.now() }))
    fsyncSync(descriptor)
    return { descriptor, path: lockPath, token }
  } catch (error) {
    closeSync(descriptor)
    try { unlinkSync(lockPath) } catch {}
    throw error
  }
}

function staleLock(path: string): boolean {
  try {
    const owner = JSON.parse(readFileSync(path, 'utf8')) as { pid?: number }
    if (typeof owner.pid !== 'number') return Date.now() - statSync(path).mtimeMs > 30_000
    try {
      process.kill(owner.pid, 0)
      return false
    } catch (error) {
      return (error as NodeJS.ErrnoException).code === 'ESRCH'
    }
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return false
    try { return Date.now() - statSync(path).mtimeMs > 30_000 } catch (statError) {
      if ((statError as NodeJS.ErrnoException).code === 'ENOENT') return false
      throw statError
    }
  }
}

function reapStaleLock(path: string): boolean {
  const reaperPath = `${path}.reaper`
  let reaper: number
  try {
    reaper = openSync(reaperPath, 'wx', 0o600)
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') return false
    throw error
  }
  try {
    if (!staleLock(path)) return false
    try { unlinkSync(path) } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
    return true
  } finally {
    closeSync(reaper)
    try { unlinkSync(reaperPath) } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }
  }
}

function releaseExclusiveLock(lock: ExclusiveLock): void {
  try {
    const owner = JSON.parse(readFileSync(lock.path, 'utf8')) as { token?: string }
    if (owner.token !== lock.token) throw new Error(`refusing to release loop ledger lock owned by another writer: ${lock.path}`)
    unlinkSync(lock.path)
  } finally {
    closeSync(lock.descriptor)
  }
}

function writeAll(descriptor: number, text: string): void {
  const buffer = Buffer.from(text, 'utf8')
  let offset = 0
  while (offset < buffer.length) {
    const written = writeSync(descriptor, buffer, offset, buffer.length - offset)
    if (written <= 0) throw new Error('loop ledger write made no progress')
    offset += written
  }
}

function fsyncDirectory(path: string): void {
  const descriptor = openSync(path, 'r')
  try {
    try { fsyncSync(descriptor) } catch (error) {
      if (!['EINVAL', 'ENOTSUP'].includes((error as NodeJS.ErrnoException).code ?? '')) throw error
    }
  } finally {
    closeSync(descriptor)
  }
}

export function initialState(workflowId: string, goal: string, maxAttempts: number, acceptance: LoopState['acceptance'], loopSpec = loadLoopSpec()): LoopState {
  return { workflowId, status: 'IDLE', node: loopSpec.entrypoint, attempt: 0, maxAttempts, goal, acceptance, decisions: [], loopSpec, loopSpecHash: loopSpecHash(loopSpec) }
}

export function foldState(agent: Agent, fallback?: LoopState): LoopState | undefined {
  let state = fallback
  for (const payload of loadLoopEvents(agent)) {
    if (payload.kind === 'state') {
      state = state ? { ...state, ...payload.state } : payload.state as LoopState
    }
    if (payload.kind === 'decision' && state && payload.decision) state = { ...state, decisions: [...state.decisions, payload.decision] }
  }
  if (state && !(state as Partial<LoopState>).loopSpec) {
    const loopSpec = loadLoopSpec()
    state = { ...state, loopSpec, loopSpecHash: loopSpecHash(loopSpec) }
  }
  if (state) {
    const legacyRoles: Record<string, string> = { EXECUTE: 'execute', VERIFY: 'verify', HITL: 'human_gate', PROMOTE: 'promote', COMPLETED: 'complete', FAILED: 'failed' }
    const role = legacyRoles[state.node]
    if (role) state = { ...state, node: nodeForRole(state.loopSpec, role) }
  }
  return state
}

export function decision(type: string, question: string, choice: string, rationale: string[], evidence: Record<string, unknown>[], risk: string, expectedEffect: string): DecisionRecord {
  return { id: `decision:${type}:${Date.now()}`, type, question, decision: choice, rationale, evidence, risk, expectedEffect, at: Date.now() }
}
