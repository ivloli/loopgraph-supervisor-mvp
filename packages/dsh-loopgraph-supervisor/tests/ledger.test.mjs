import assert from 'node:assert/strict'
import test from 'node:test'
import { execFileSync, spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { appendFileSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { appendLoopEvent, decision, foldState, initialState, loadLoopEvents, withLoopOperationLock } from '../lib/ledger.js'
import { archiveReports, gitCandidateFingerprint, parsePorcelainPaths, prepareGitCandidate, rejectCandidate } from '../lib/git.js'
import { apply, specRevision } from '../lib/index.js'
import { loadLoopSpec, loadWorkspaceLoopSpec, loopSpecHash, nextNode, nodeForRole, validateLoopSpec } from '../lib/loopspec.js'
import { verifyCommands } from '../lib/verifier.js'

test('creates an explainable initial state and decision', () => {
  const state = initialState('wf-1', 'run tests', 3, { commands: ['pytest -q'] })
  const record = decision('PLAN', 'Why start?', 'start', ['human requested it'], [], 'workspace may change', 'create durable state')
  assert.equal(state.node, 'execute')
  assert.equal(state.maxAttempts, 3)
  assert.equal(record.decision, 'start')
})

test('shared LoopSpec vectors match the TypeScript interpreter', () => {
  const spec = loadLoopSpec()
  const vectors = JSON.parse(readFileSync(join(process.cwd(), '../../configs/loopspecs/coding-supervisor/test-vectors.json'), 'utf8'))
  for (const vector of vectors) {
    const source = nodeForRole(spec, vector.source_role)
    const expected = nodeForRole(spec, vector.expected_role)
    assert.equal(nextNode(spec, source, vector.outcome, vector.iteration), expected)
  }
})

test('B default LoopSpec is identical to the A repository artifact', () => {
  const packaged = JSON.parse(readFileSync(join(process.cwd(), 'loopspec.v1.json'), 'utf8'))
  const shared = JSON.parse(readFileSync(join(process.cwd(), '../../configs/loopspecs/coding-supervisor/v1.json'), 'utf8'))
  assert.deepEqual(packaged, shared)
  assert.equal(loopSpecHash(packaged), readFileSync(join(process.cwd(), '../../configs/loopspecs/coding-supervisor/v1.sha256'), 'utf8').trim())
})

test('legacy uppercase node ids migrate onto the active LoopSpec roles', () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-legacy-node-'))
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    const agent = { id: 'legacy-node', session: { events: [] } }
    const state = initialState('legacy', 'resume', 2, {})
    const legacy = { ...state, node: 'HITL' }
    delete legacy.loopSpec
    delete legacy.loopSpecHash
    appendLoopEvent(agent, { workflowId: legacy.workflowId, kind: 'state', state: legacy })
    assert.equal(foldState(agent).node, 'hitl')
    assert.equal(foldState(agent).loopSpec.revision, 1)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('LoopSpec validator rejects non-terminating graph', () => {
  const spec = structuredClone(loadLoopSpec())
  spec.edges = spec.edges.filter(edge => edge.source !== 'promote')
  assert.throws(() => validateLoopSpec(spec), /unreachable|no outgoing edge|terminating path/)
})

test('/loop evolve persists a DSH-native LoopSpec evolution request', async () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-evolve-'))
  const repo = join(root, 'repo')
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    execFileSync('mkdir', ['-p', join(repo, 'configs', 'loopspecs', 'coding-supervisor')])
    execFileSync('git', ['init'], { cwd: repo })
    execFileSync('git', ['config', 'user.name', 'LoopGraph Test'], { cwd: repo })
    execFileSync('git', ['config', 'user.email', 'loopgraph@example.test'], { cwd: repo })
    const specPath = join(repo, 'configs', 'loopspecs', 'coding-supervisor', 'v1.json')
    writeFileSync(specPath, JSON.stringify(loadLoopSpec(), null, 2))
    execFileSync('git', ['add', '.'], { cwd: repo })
    execFileSync('git', ['commit', '-m', 'baseline'], { cwd: repo })
    const events = []
    const followups = []
    const agent = { id: 'session-evolve', session: { header: { cwd: repo }, events, append(type, data) { events.push({ type, data }) } }, followup(message) { followups.push(message) } }
    let loopCommand
    const ctx = { on() {}, agents: { list: () => [agent] }, userQuestions: { ask: async () => ({ answers: [] }) }, commands: { register(command) { loopCommand = command } } }
    apply(ctx, { maxAttempts: 2, requirePromotionApproval: true, loopSpecPath: specPath })

    const result = await loopCommand.handler({ agent, rawInput: 'evolve escalate explicit verifier failure to human review', signal: new AbortController().signal })
    const state = foldState(agent)

    assert.equal(result.kind, 'success')
    assert.equal(state.status, 'RUNNING')
    assert.equal(state.node, 'execute')
    assert.equal(state.evolution.kind, 'loopspec')
    assert.match(state.evolution.candidatePath, /v2\.json$/)
    assert.equal(state.evolution.predecessorHash, state.loopSpecHash)
    assert.equal(followups.length, 1)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('/loop evolve validates and human-activates a v2 LoopSpec', async () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-evolve-activate-'))
  const repo = join(root, 'repo')
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    execFileSync('mkdir', ['-p', join(repo, 'configs', 'loopspecs', 'coding-supervisor')])
    execFileSync('git', ['init'], { cwd: repo })
    execFileSync('git', ['config', 'user.name', 'LoopGraph Test'], { cwd: repo })
    execFileSync('git', ['config', 'user.email', 'loopgraph@example.test'], { cwd: repo })
    const active = loadLoopSpec()
    const specPath = join(repo, 'configs', 'loopspecs', 'coding-supervisor', 'v1.json')
    writeFileSync(specPath, JSON.stringify(active, null, 2))
    execFileSync('git', ['add', '.'], { cwd: repo })
    execFileSync('git', ['commit', '-m', 'baseline'], { cwd: repo })
    const events = []
    const handlers = {}
    const agent = { id: 'session-evolve-activate', session: { header: { cwd: repo }, events, append(type, data) { events.push({ type, data }) } }, followup() {}, cancel() {} }
    let loopCommand
    const ctx = {
      on(name, handler) { handlers[name] = handler },
      agents: { list: () => [agent] },
      userQuestions: { ask: async () => ({ answers: [] }) },
      commands: {
        register(command) { loopCommand = command },
        find() { return {} },
        execute: async () => ({ result: { kind: 'success', text: 'Verdict: deliverable' } }),
      },
    }
    apply(ctx, { maxAttempts: 2, requirePromotionApproval: true, loopSpecPath: specPath })
    const started = await loopCommand.handler({ agent, rawInput: 'evolve send explicit verifier failure to human review', signal: new AbortController().signal })
    assert.equal(started.kind, 'success')
    const candidatePath = join(repo, 'configs', 'loopspecs', 'coding-supervisor', 'v2.json')
    const candidate = structuredClone(active)
    candidate.revision = 2
    candidate.predecessor_hash = loopSpecHash(active)
    candidate.edges = candidate.edges.map(edge => edge.source === 'execute' && edge.outcomes.includes('fail') ? { ...edge, target: 'hitl' } : edge)
    writeFileSync(candidatePath, JSON.stringify(candidate, null, 2))
    events.push({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'LOOPGRAPH_RESULT: {"status":"pass","summary":"v2 ready"}' }] } } })
    await handlers['agent/turn-stopping']({ agent, signal: new AbortController().signal })
    const waiting = foldState(agent)
    assert.equal(waiting.status, 'WAITING_HITL')
    assert.equal(waiting.hitlReason, 'PROMOTION_REVIEW')
    assert.equal(waiting.candidateLoopSpec.revision, 2)

    const approved = await loopCommand.handler({ agent, rawInput: 'approve reviewed graph diff and evidence', signal: new AbortController().signal })
    const activated = foldState(agent)
    assert.equal(approved.kind, 'success')
    assert.equal(activated.status, 'COMPLETED')
    assert.equal(activated.loopSpec.revision, 2)
    assert.equal(activated.loopSpecHash, loopSpecHash(candidate))
    assert.equal(activated.node, 'complete')
    assert.equal(loadWorkspaceLoopSpec(repo, active).revision, 2)

    const failureEvents = []
    const failureHandlers = {}
    const failureAgent = { id: 'session-v2-failure', session: { header: { cwd: repo }, events: failureEvents, append(type, data) { failureEvents.push({ type, data }) } }, followup() {}, cancel() {} }
    let failureCommand
    const failureCtx = { on(name, handler) { failureHandlers[name] = handler }, agents: { list: () => [failureAgent] }, userQuestions: { ask: async () => ({ answers: [] }) }, commands: { register(command) { failureCommand = command } } }
    apply(failureCtx, { maxAttempts: 2, requirePromotionApproval: true, loopSpecPath: specPath })
    const failureStarted = await failureCommand.handler({ agent: failureAgent, rawInput: 'start task with a missing authoritative contract', signal: new AbortController().signal })
    assert.equal(failureStarted.kind, 'success')
    failureEvents.push({ type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'LOOPGRAPH_RESULT: {"status":"fail","summary":"required contract is missing"}' }] } } })
    await failureHandlers['agent/turn-stopping']({ agent: failureAgent, signal: new AbortController().signal })
    const escalated = foldState(failureAgent)
    assert.equal(escalated.status, 'WAITING_HITL')
    assert.equal(escalated.hitlReason, 'FAILURE_REVIEW')
    assert.equal(escalated.attempt, 1)
    const failureRejected = await failureCommand.handler({ agent: failureAgent, rawInput: 'reject expected escalation evidence captured', signal: new AbortController().signal })
    assert.equal(failureRejected.kind, 'success')

    const rolledBack = await loopCommand.handler({ agent, rawInput: `rollback version:${activated.workflowId}:baseline`, signal: new AbortController().signal })
    assert.equal(rolledBack.kind, 'success')
    assert.equal(loadWorkspaceLoopSpec(repo, active).revision, 1)

    const nextEvents = []
    const nextAgent = { id: 'session-evolve-next', session: { header: { cwd: repo }, events: nextEvents, append(type, data) { nextEvents.push({ type, data }) } }, followup() {}, cancel() {} }
    let nextCommand
    const nextCtx = { on() {}, agents: { list: () => [nextAgent] }, userQuestions: { ask: async () => ({ answers: [] }) }, commands: { register(command) { nextCommand = command } } }
    apply(nextCtx, { maxAttempts: 2, requirePromotionApproval: true, loopSpecPath: specPath })
    const next = await nextCommand.handler({ agent: nextAgent, rawInput: 'start verify active LoopSpec persistence', signal: new AbortController().signal })
    assert.equal(next.kind, 'success')
    assert.equal(foldState(nextAgent).loopSpec.revision, 1)

  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('preserves the first path character from porcelain status', () => {
  assert.deepEqual(parsePorcelainPaths(' M calculator.py\n?? new file.py\n'), ['calculator.py', 'new file.py'])
})

test('parses NUL-delimited rename records and paths with spaces', () => {
  assert.deepEqual(parsePorcelainPaths('R  new name.py\0old name.py\0?? another file.py\0'), ['another file.py', 'new name.py', 'old name.py'])
})

test('restores state from the durable sidecar ledger without session events', () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-ledger-'))
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    const agent = { id: 'session-test', session: { events: [] } }
    const state = initialState('wf-sidecar', 'verify recovery', 2, {})
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state })
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state: { status: 'RUNNING', attempt: 1 } })
    assert.equal(loadLoopEvents(agent).length, 2)
    assert.equal(foldState(agent).status, 'RUNNING')
    assert.equal(foldState(agent).attempt, 1)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('acceptance commands fail closed and record exit codes', async () => {
  assert.equal((await verifyCommands(process.cwd(), [])).passed, false)
  const result = await verifyCommands(process.cwd(), ['true', 'false'])
  assert.equal(result.passed, false)
  assert.deepEqual(result.evidence.map(item => item.exitCode), [0, 1])
})

test('ledger recovers the valid prefix before a torn final line', () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-torn-'))
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    const agent = { id: 'session-torn', session: { events: [] } }
    const state = initialState('wf-torn', 'recover prefix', 2, {})
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state })
    appendFileSync(join(root, 'loopgraph', 'session-torn.jsonl'), '{"time":')
    assert.equal(foldState(agent).workflowId, 'wf-torn')
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'evidence', evidence: { type: 'recovered' } })
    assert.equal(loadLoopEvents(agent).length, 2)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('ledger rejects a newline-terminated malformed record', () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-malformed-'))
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    const agent = { id: 'session-malformed', session: { events: [] } }
    const state = initialState('wf-malformed', 'reject corruption', 2, {})
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state })
    appendFileSync(join(root, 'loopgraph', 'session-malformed.jsonl'), '{"time":}\n')
    assert.throws(() => loadLoopEvents(agent), SyntaxError)
    assert.throws(() => appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'evidence', evidence: { type: 'must-not-append' } }), SyntaxError)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('ledger reads legacy records and rejects checksum tampering', () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-checksum-'))
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    const agent = { id: 'session-checksum', session: { events: [] } }
    const state = initialState('wf-checksum', 'verify checksums', 2, {})
    const path = join(root, 'loopgraph', 'session-checksum.jsonl')
    execFileSync('mkdir', ['-p', join(root, 'loopgraph')])
    const legacy = [
      { time: 1, event: { workflowId: state.workflowId, kind: 'state', state } },
      { time: 2, event: { workflowId: state.workflowId, kind: 'evidence', evidence: { type: 'legacy' } } },
    ]
    writeFileSync(path, `${legacy.map(JSON.stringify).join('\n')}\n`)
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'evidence', evidence: { type: 'verified' } })
    assert.equal(loadLoopEvents(agent).length, 3)

    const lines = readFileSync(path, 'utf8').trim().split('\n')
    const tamperedLegacy = JSON.parse(lines[0])
    tamperedLegacy.event.state.goal = 'tampered legacy goal'
    writeFileSync(path, `${[JSON.stringify(tamperedLegacy), lines[1], lines[2]].join('\n')}\n`)
    assert.throws(() => loadLoopEvents(agent), /broken.*checksum chain/)

    const tampered = JSON.parse(lines[2])
    tampered.event.evidence.type = 'tampered'
    writeFileSync(path, `${[lines[0], lines[1], JSON.stringify(tampered)].join('\n')}\n`)
    assert.throws(() => loadLoopEvents(agent), /checksum mismatch/)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('ledger reads the prior mixed-chain checksum format', () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-mixed-'))
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    const agent = { id: 'session-mixed', session: { events: [] } }
    const first = { time: 1, event: { workflowId: 'wf-mixed', kind: 'evidence', evidence: { index: 1 } } }
    const second = { time: 2, event: { workflowId: 'wf-mixed', kind: 'evidence', evidence: { index: 2 } } }
    const previousChecksum = createHash('sha256').update(JSON.stringify(second)).digest('hex')
    const body = { time: 3, sequence: 3, previousChecksum, event: { workflowId: 'wf-mixed', kind: 'evidence', evidence: { index: 3 } } }
    const third = { ...body, checksum: createHash('sha256').update(JSON.stringify(body)).digest('hex') }
    execFileSync('mkdir', ['-p', join(root, 'loopgraph')])
    writeFileSync(join(root, 'loopgraph', 'session-mixed.jsonl'), `${[first, second, third].map(JSON.stringify).join('\n')}\n`)
    assert.equal(loadLoopEvents(agent).length, 3)
    appendLoopEvent(agent, { workflowId: 'wf-mixed', kind: 'evidence', evidence: { index: 4 } })
    assert.equal(loadLoopEvents(agent).length, 4)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('ledger serializes concurrent cross-process writers', async () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-concurrent-'))
  const agent = { id: 'session-concurrent', session: { events: [] } }
  const worker = `
    import { appendLoopEvent } from ${JSON.stringify(new URL('../lib/ledger.js', import.meta.url).href)};
    const agent = { id: 'session-concurrent', session: { events: [] } };
    for (let index = 0; index < 20; index += 1) appendLoopEvent(agent, { workflowId: 'wf-concurrent', kind: 'evidence', evidence: { worker: process.argv[1], index } });
  `
  const run = id => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ['--input-type=module', '-e', worker, id], { env: { ...process.env, DSH_HOME: root } })
    child.on('error', reject)
    child.on('exit', code => code === 0 ? resolve() : reject(new Error(`worker ${id} exited ${code}`)))
  })
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    execFileSync('mkdir', ['-p', join(root, 'loopgraph')])
    writeFileSync(join(root, 'loopgraph', 'session-concurrent.jsonl.lock'), JSON.stringify({ pid: 999999, token: 'stale' }))
    await Promise.all([run('a'), run('b'), run('c')])
    assert.equal(loadLoopEvents(agent).length, 60)
    const records = readFileSync(join(root, 'loopgraph', 'session-concurrent.jsonl'), 'utf8').trim().split('\n').map(JSON.parse)
    assert.deepEqual(records.map(record => record.sequence), Array.from({ length: 60 }, (_, index) => index + 1))
    assert.ok(records.every(record => typeof record.checksum === 'string' && record.checksum.length === 64))
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('operation lock serializes asynchronous workflow decisions', async () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-operation-'))
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  try {
    const agent = { id: 'session-operation', session: { events: [] } }
    let active = 0
    let maximum = 0
    const operation = () => withLoopOperationLock(agent, async () => {
      active += 1
      maximum = Math.max(maximum, active)
      await new Promise(resolve => setTimeout(resolve, 25))
      active -= 1
    })
    await Promise.all([operation(), operation(), operation()])
    assert.equal(maximum, 1)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('reject archives reports and restores only the candidate scope', async () => {
  const repo = mkdtempSync(join(tmpdir(), 'loopgraph-reject-'))
  const git = (...args) => execFileSync('git', args, { cwd: repo, encoding: 'utf8' }).trim()
  try {
    git('init')
    git('config', 'user.name', 'LoopGraph Test')
    git('config', 'user.email', 'loopgraph@example.test')
    writeFileSync(join(repo, 'artifact.txt'), 'baseline\n')
    git('add', 'artifact.txt')
    git('commit', '-m', 'baseline')
    const baseline = git('rev-parse', 'HEAD')
    writeFileSync(join(repo, 'artifact.txt'), 'candidate\n')
    writeFileSync(join(repo, 'gate-report.md'), 'rework required\n')

    const cleanup = await rejectCandidate(repo, baseline, ['artifact.txt'], ['gate-report.md'])

    assert.equal(readFileSync(join(repo, 'artifact.txt'), 'utf8'), 'baseline\n')
    assert.deepEqual(cleanup.restoredFiles, ['artifact.txt'])
    assert.equal(cleanup.removedReports[0].content, 'rework required\n')
    assert.equal(git('status', '--porcelain'), '')
  } finally {
    rmSync(repo, { recursive: true, force: true })
  }
})

test('reject refuses unknown out-of-scope changes', async () => {
  const repo = mkdtempSync(join(tmpdir(), 'loopgraph-reject-scope-'))
  const git = (...args) => execFileSync('git', args, { cwd: repo, encoding: 'utf8' }).trim()
  try {
    git('init')
    git('config', 'user.name', 'LoopGraph Test')
    git('config', 'user.email', 'loopgraph@example.test')
    writeFileSync(join(repo, 'artifact.txt'), 'baseline\n')
    git('add', 'artifact.txt')
    git('commit', '-m', 'baseline')
    writeFileSync(join(repo, 'unexpected.txt'), 'human work\n')
    await assert.rejects(() => rejectCandidate(repo, git('rev-parse', 'HEAD'), ['artifact.txt'], ['gate-report.md']), /out-of-scope/)
  } finally {
    rmSync(repo, { recursive: true, force: true })
  }
})

test('successful gate reports are archived outside the candidate diff', () => {
  const workspace = mkdtempSync(join(tmpdir(), 'loopgraph-reports-'))
  try {
    writeFileSync(join(workspace, 'gate-report.md'), 'Verdict: deliverable\n')
    const reports = archiveReports(workspace, ['gate-report.md'])
    assert.equal(reports[0].content, 'Verdict: deliverable\n')
    assert.throws(() => readFileSync(join(workspace, 'gate-report.md')), /ENOENT/)
  } finally {
    rmSync(workspace, { recursive: true, force: true })
  }
})

test('approve persists the optional human comment in evidence and the decision ledger', async () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-approve-'))
  const repo = join(root, 'repo')
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  const git = (...args) => execFileSync('git', args, { cwd: repo, encoding: 'utf8' }).trim()
  try {
    execFileSync('mkdir', [repo])
    git('init')
    git('config', 'user.name', 'LoopGraph Test')
    git('config', 'user.email', 'loopgraph@example.test')
    writeFileSync(join(repo, 'artifact.txt'), 'baseline\n')
    git('add', 'artifact.txt')
    git('commit', '-m', 'baseline')
    const baseline = git('rev-parse', 'HEAD')
    writeFileSync(join(repo, 'artifact.txt'), 'candidate\n')
    const prepared = await prepareGitCandidate(repo, 'loopgraph: human-approved promote wf-approve attempt 2')

    const events = []
    const agent = {
      id: 'session-approve',
      session: {
        header: { cwd: repo },
        events,
        append(type, data) { events.push({ type, data }) },
      },
    }
    const state = {
      ...initialState('wf-approve', 'promote candidate', 2, { allowedFiles: ['artifact.txt'] }),
      status: 'WAITING_HITL',
      node: 'hitl',
      attempt: 2,
      baselineCommit: baseline,
      hitlReason: 'PROMOTION_REVIEW',
      specRevision: specRevision('promote candidate', 2, { allowedFiles: ['artifact.txt'] }),
      candidateFingerprint: prepared.fingerprint,
      preparedCandidate: { sha: prepared.sha, tree: prepared.tree, parent: prepared.parent, files: prepared.files },
      activeVersion: 'version:wf-approve:baseline',
      versions: [{ id: 'version:wf-approve:baseline', sha: baseline, status: 'BASELINE' }],
    }
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state })

    let loopCommand
    const ctx = {
      on() {},
      get() { return undefined },
      agents: { list: () => [agent] },
      userQuestions: { ask: async () => ({ answers: [] }) },
      commands: { register(command) { loopCommand = command } },
    }
    apply(ctx, { maxAttempts: 2, requirePromotionApproval: true })
    const comment = 'Reviewed  startup behavior\nand evidence'
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state: { acceptance: { allowedFiles: ['artifact.txt', 'unreviewed.txt'] } } })
    const staleSpec = await loopCommand.handler({ agent, rawInput: `approve ${comment}`, signal: new AbortController().signal })
    assert.equal(staleSpec.kind, 'error')
    assert.match(staleSpec.text, /spec revision/)
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state: { acceptance: state.acceptance } })
    writeFileSync(join(repo, 'artifact.txt'), 'changed after review\n')
    const stale = await loopCommand.handler({ agent, rawInput: `approve ${comment}`, signal: new AbortController().signal })
    assert.equal(stale.kind, 'error')
    assert.match(stale.text, /candidate changed after review/)
    assert.equal(foldState(agent).status, 'WAITING_HITL')
    writeFileSync(join(repo, 'artifact.txt'), 'candidate\n')
    const result = await loopCommand.handler({ agent, rawInput: `approve ${comment}`, signal: new AbortController().signal })

    assert.equal(result.kind, 'success')
    const records = loadLoopEvents(agent)
    const candidate = records.find(event => event.kind === 'evidence' && event.evidence?.type === 'git_candidate')
    const approval = records.find(event => event.kind === 'decision' && event.decision?.type === 'HUMAN_APPROVE_PROMOTE')
    assert.equal(candidate.evidence.humanComment, comment)
    assert.deepEqual(approval.decision.evidence.at(-1), { type: 'human_comment', text: comment })
    assert.match(approval.decision.rationale.at(-1), /Reviewed  startup behavior\nand evidence/)
    assert.equal(foldState(agent).status, 'COMPLETED')
    assert.equal(git('status', '--porcelain'), '')
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('command operation lock rejects concurrent starts and illegal state transitions', async () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-commands-'))
  const repo = join(root, 'repo')
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  const git = (...args) => execFileSync('git', args, { cwd: repo, encoding: 'utf8' }).trim()
  try {
    execFileSync('mkdir', [repo])
    git('init')
    git('config', 'user.name', 'LoopGraph Test')
    git('config', 'user.email', 'loopgraph@example.test')
    writeFileSync(join(repo, 'artifact.txt'), 'baseline\n')
    git('add', 'artifact.txt')
    git('commit', '-m', 'baseline')

    const events = []
    const agent = {
      id: 'session-commands',
      session: { header: { cwd: repo }, events, append(type, data) { events.push({ type, data }) } },
      followup() {},
      cancel() {},
    }
    let loopCommand
    const ctx = {
      on() {},
      get() { return undefined },
      agents: { list: () => [agent] },
      userQuestions: { ask: async () => ({ answers: [] }) },
      commands: { register(command) { loopCommand = command } },
    }
    apply(ctx, { maxAttempts: 2, requirePromotionApproval: true })
    const invoke = rawInput => loopCommand.handler({ agent, rawInput, signal: new AbortController().signal })
    const starts = await Promise.all([invoke('start first goal'), invoke('start second goal')])
    assert.deepEqual(starts.map(result => result.kind).sort(), ['error', 'success'])
    assert.equal(loadLoopEvents(agent).filter(event => event.kind === 'decision' && event.decision?.type === 'PLAN').length, 1)

    assert.equal((await invoke('resume')).kind, 'error')
    assert.equal((await invoke('rollback version:missing')).kind, 'error')
    assert.equal((await invoke('pause')).kind, 'success')
    assert.equal((await invoke('pause')).kind, 'error')
    assert.equal((await invoke('resume')).kind, 'success')
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('verify-existing recovery can auto-promote without nesting the operation lock', async () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-recover-promote-'))
  const repo = join(root, 'repo')
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  const git = (...args) => execFileSync('git', args, { cwd: repo, encoding: 'utf8' }).trim()
  try {
    execFileSync('mkdir', [repo])
    git('init')
    git('config', 'user.name', 'LoopGraph Test')
    git('config', 'user.email', 'loopgraph@example.test')
    writeFileSync(join(repo, 'artifact.txt'), 'baseline\n')
    git('add', 'artifact.txt')
    git('commit', '-m', 'baseline')
    const baseline = git('rev-parse', 'HEAD')
    writeFileSync(join(repo, 'artifact.txt'), 'candidate\n')

    const events = []
    const agent = {
      id: 'session-recover-promote',
      session: { header: { cwd: repo }, events, append(type, data) { events.push({ type, data }) } },
      followup() {},
      cancel() {},
    }
    const state = {
      ...initialState('wf-recover-promote', 'verify existing', 2, { commands: ['true'], allowedFiles: ['artifact.txt'] }),
      status: 'UNCERTAIN', node: 'hitl', attempt: 1, baselineCommit: baseline, hitlReason: 'UNCERTAIN_RECOVERY',
      activeVersion: 'version:wf-recover-promote:baseline',
      versions: [{ id: 'version:wf-recover-promote:baseline', sha: baseline, status: 'BASELINE' }],
    }
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state })
    let loopCommand
    const ctx = {
      on() {},
      get() { return undefined },
      agents: { list: () => [agent] },
      userQuestions: { ask: async () => ({ answers: [] }) },
      commands: {
        register(command) { loopCommand = command },
        find() { return {} },
        execute: async () => ({ result: { kind: 'success', text: 'Verdict: deliverable' } }),
      },
    }
    apply(ctx, { maxAttempts: 2, requirePromotionApproval: false })
    const result = await loopCommand.handler({ agent, rawInput: 'recover verify-existing', signal: new AbortController().signal })
    assert.equal(result.kind, 'success')
    assert.equal(foldState(agent).status, 'COMPLETED')
    assert.equal(git('status', '--porcelain'), '')
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('pause prevents an in-flight verification result from overwriting state', async () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-pause-race-'))
  const repo = join(root, 'repo')
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  const git = (...args) => execFileSync('git', args, { cwd: repo, encoding: 'utf8' }).trim()
  try {
    execFileSync('mkdir', [repo])
    git('init')
    git('config', 'user.name', 'LoopGraph Test')
    git('config', 'user.email', 'loopgraph@example.test')
    writeFileSync(join(repo, 'artifact.txt'), 'candidate\n')
    git('add', 'artifact.txt')
    git('commit', '-m', 'baseline')
    const baseline = git('rev-parse', 'HEAD')

    const assistant = { type: 'assistant/message', data: { message: { content: [{ type: 'text', text: 'LOOPGRAPH_RESULT: {"status":"pass","summary":"candidate ready"}' }] } } }
    const events = [assistant]
    const agent = {
      id: 'session-pause-race',
      session: { header: { cwd: repo }, events, append(type, data) { events.push({ type, data }) } },
      followup() {},
      cancel() {},
    }
    const state = {
      ...initialState('wf-pause-race', 'pause verification', 2, { commands: ['sleep 0.15'], allowedFiles: ['artifact.txt'] }),
      status: 'RUNNING', node: 'execute', attempt: 1, baselineCommit: baseline,
    }
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state })
    let loopCommand
    const handlers = {}
    const ctx = {
      on(name, handler) { handlers[name] = handler },
      get() { return undefined },
      agents: { list: () => [agent] },
      userQuestions: { ask: async () => ({ answers: [] }) },
      commands: {
        register(command) { loopCommand = command },
        find() { return {} },
        execute: async () => ({ result: { kind: 'success', text: 'Verdict: deliverable' } }),
      },
    }
    apply(ctx, { maxAttempts: 2, requirePromotionApproval: true })
    const signal = new AbortController().signal
    const verification = handlers['agent/turn-stopping']({ agent, signal })
    await new Promise(resolve => setTimeout(resolve, 25))
    assert.equal((await loopCommand.handler({ agent, rawInput: 'pause', signal })).kind, 'success')
    await verification
    assert.equal(foldState(agent).status, 'PAUSED')
    assert.equal(loadLoopEvents(agent).some(event => event.kind === 'evidence' && event.evidence?.type === 'doublecheck_gate'), false)
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})

test('named workflow delegation fails closed until lifecycle adoption is implemented', async () => {
  const root = mkdtempSync(join(tmpdir(), 'loopgraph-named-disabled-'))
  const repo = join(root, 'repo')
  const previous = process.env.DSH_HOME
  process.env.DSH_HOME = root
  const git = (...args) => execFileSync('git', args, { cwd: repo, encoding: 'utf8' }).trim()
  try {
    execFileSync('mkdir', [repo])
    git('init')
    git('config', 'user.name', 'LoopGraph Test')
    git('config', 'user.email', 'loopgraph@example.test')
    writeFileSync(join(repo, 'artifact.txt'), 'baseline\n')
    git('add', 'artifact.txt')
    git('commit', '-m', 'baseline')
    const events = []
    const agent = {
      id: 'session-named-disabled',
      session: { header: { cwd: repo }, events, append(type, data) { events.push({ type, data }) } },
      followup() {},
      cancel() {},
    }
    let loopCommand
    const ctx = {
      on() {},
      get() { return undefined },
      agents: { list: () => [agent] },
      userQuestions: { ask: async () => ({ answers: [] }) },
      commands: { register(command) { loopCommand = command } },
    }
    apply(ctx, { maxAttempts: 2, requirePromotionApproval: true, workflowName: 'named-coder' })
    const result = await loopCommand.handler({ agent, rawInput: 'start change artifact', signal: new AbortController().signal })
    assert.equal(result.kind, 'error')
    assert.match(result.text, /disabled until terminal outcome/)
    assert.equal(loadLoopEvents(agent).length, 0)
    assert.equal(git('status', '--porcelain'), '')
  } finally {
    if (previous === undefined) delete process.env.DSH_HOME
    else process.env.DSH_HOME = previous
    rmSync(root, { recursive: true, force: true })
  }
})
