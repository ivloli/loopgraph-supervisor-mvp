import { createHash } from 'node:crypto';
import { existsSync, lstatSync, realpathSync } from 'node:fs';
import { dirname, isAbsolute, join, relative, resolve } from 'node:path';
import '@deepseek-ai/dsh-agent';
import '@deepseek-ai/dsh-commands';
import '@deepseek-ai/dsh-user-questions';
import { createUserMessage } from '@deepseek-ai/dsh-llm';
import Schema from '@deepseek-ai/schemastery';
import { acquireWorkspaceLock, archiveReports, gitCandidateFingerprint, gitChangedFiles, gitHead, gitRollback, prepareGitCandidate, promotePreparedCandidate, rejectCandidate, releaseWorkspaceLock } from './git.js';
import { verifyCommands } from './verifier.js';
import { appendLoopEvent, decision, foldState, initialState, loadLoopEvents, withLoopOperationLock } from './ledger.js';
import { loadLoopSpec, loadWorkspaceLoopSpec, loopSpecHash, nextNode, nodeForRole, saveWorkspaceLoopSpec } from './loopspec.js';
export const name = 'dsh-loopgraph-supervisor';
export const inject = ['commands', 'agents', 'sessions', 'userQuestions'];
export const Config = Schema.object({
    maxAttempts: Schema.number().min(1).default(3),
    requirePromotionApproval: Schema.boolean().default(true),
    workflowName: Schema.string(),
    loopSpecPath: Schema.string(),
});
const states = new WeakMap();
function roleNode(state, role) {
    return nodeForRole(state.loopSpec, role);
}
function route(state, outcome, source = state.node) {
    return nextNode(state.loopSpec, source, outcome, Math.max(0, state.attempt - 1));
}
function assertCandidateFile(cwd, path) {
    if (!existsSync(path))
        throw new Error('LoopSpec candidate file does not exist');
    const metadata = lstatSync(path);
    if (!metadata.isFile() || metadata.isSymbolicLink())
        throw new Error('LoopSpec candidate must be a regular non-symlink file');
    const workspace = realpathSync(cwd);
    const candidate = realpathSync(path);
    if (relative(workspace, candidate).startsWith('..'))
        throw new Error('LoopSpec candidate real path escapes the Git workspace');
}
function getState(agent) {
    return foldState(agent, states.get(agent));
}
function setState(agent, state) {
    states.set(agent, state);
}
function emitDecision(agent, state, record) {
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'decision', decision: record });
    const next = { ...state, decisions: [...state.decisions, record] };
    setState(agent, next);
    return next;
}
function transition(agent, state, next, reason) {
    const nextState = { ...state, ...next };
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state: nextState });
    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'evidence', evidence: { type: 'transition', from: state.node, to: nextState.node, reason, attempt: nextState.attempt } });
    setState(agent, nextState);
    return nextState;
}
function promotedFields(state, sha, ingress) {
    const id = `version:${state.workflowId}:${state.attempt}`;
    const promote = roleNode(state, 'promote');
    if (route(state, ingress) !== promote)
        throw new Error(`LoopSpec ${ingress} route does not enter promotion`);
    const activeSpec = state.candidateLoopSpec ?? state.loopSpec;
    const versions = [...(state.versions ?? []), { id, sha, parentId: state.activeVersion, status: 'PROMOTED', loopSpec: activeSpec }];
    const activePromote = nodeForRole(activeSpec, 'promote');
    return { status: 'COMPLETED', node: nextNode(activeSpec, activePromote, 'pass', Math.max(0, state.attempt - 1)), candidateCommit: sha, activeVersion: id, versions, hitlReason: undefined, loopSpec: activeSpec, loopSpecHash: loopSpecHash(activeSpec), candidateLoopSpec: undefined, evolution: undefined };
}
function canonical(value) {
    if (Array.isArray(value))
        return `[${value.map(canonical).join(',')}]`;
    if (value && typeof value === 'object')
        return `{${Object.entries(value).sort(([left], [right]) => left.localeCompare(right)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(',')}}`;
    return JSON.stringify(value);
}
export function specRevision(goal, maxAttempts, acceptance) {
    return createHash('sha256').update(canonical({ goal, maxAttempts, acceptance })).digest('hex');
}
function isCurrentExecution(agent, expected, node) {
    const current = getState(agent);
    return current?.workflowId === expected.workflowId
        && current.attempt === expected.attempt
        && current.status === 'RUNNING'
        && current.node === node;
}
async function applyToCurrentExecution(agent, expected, node, operation) {
    return withLoopOperationLock(agent, async () => {
        if (!isCurrentExecution(agent, expected, node))
            return false;
        await operation();
        return true;
    });
}
function settleTodos(agent, completed) {
    const latest = [...agent.session.events].reverse().find(event => event.type === 'todo/write');
    if (latest?.type !== 'todo/write')
        return;
    const todos = completed
        ? latest.data.todos.map(todo => ({ ...todo, status: 'completed' }))
        : [];
    agent.session.append('todo/write', { todos });
}
function parseStart(rawInput, defaultMaxAttempts) {
    const trimmed = rawInput.trim();
    if (!trimmed)
        throw new Error('usage: /loop start <goal>');
    if (trimmed.startsWith('{')) {
        const input = JSON.parse(trimmed);
        if (!input.goal)
            throw new Error('start payload requires goal');
        return { goal: input.goal, maxAttempts: input.maxAttempts ?? defaultMaxAttempts, acceptance: input.acceptance ?? {} };
    }
    return { goal: trimmed, maxAttempts: defaultMaxAttempts, acceptance: {} };
}
function workflowPrompt(state) {
    return [
        `[loopgraph workflow=${state.workflowId} attempt=${state.attempt}]`,
        state.goal,
        state.acceptance.commands?.length ? `Acceptance commands: ${state.acceptance.commands.join(' && ')}` : '',
        state.acceptance.allowedFiles?.length ? `Allowed files: ${state.acceptance.allowedFiles.join(', ')}` : '',
        'Before reporting success, reconcile any todo list: no item may remain in_progress or pending unless the result status is fail and the summary names the unfinished work.',
        'When the task is complete, end your response with LOOPGRAPH_RESULT: {"status":"pass"|"fail","summary":"..."}.',
    ].filter(Boolean).join('\n');
}
function assistantText(event) {
    const data = event.data;
    return (data.message?.content ?? []).filter(block => block.type === 'text').map(block => block.text ?? '').join('');
}
function resultMarker(text) {
    const match = text.match(/LOOPGRAPH_RESULT:\s*(\{.*\})/s);
    if (!match)
        return undefined;
    try {
        const value = JSON.parse(match[1]);
        return value.status && value.summary ? { status: value.status, summary: value.summary } : undefined;
    }
    catch {
        return undefined;
    }
}
async function runDoublecheckGate(ctx, agent, signal) {
    const command = ctx.commands.find(agent, 'gate');
    if (!command)
        return { passed: false, text: 'dsh-doublecheck is required but not mounted' };
    const execution = await ctx.commands.execute(agent, '/gate run', [], signal);
    const result = execution?.result;
    const text = result?.text ?? '';
    return { passed: result?.kind === 'success' && /verdict:\s*deliverable\b/i.test(text), text };
}
async function handleAssistantResult(ctx, config, agent, state, text, signal) {
    const marker = resultMarker(text);
    if (!marker)
        return;
    if (marker.status === 'fail') {
        await applyToCurrentExecution(agent, state, state.node, () => {
            if (state.attempt >= state.maxAttempts) {
                const waiting = transition(agent, state, { status: 'WAITING_HITL', node: route(state, 'exhausted'), hitlReason: 'FAILURE_REVIEW' }, 'DSH reported failure and retry budget is exhausted');
                emitDecision(agent, waiting, decision('HITL_REQUIRED', 'Why wait for a human?', 'wait_for_human', [marker.summary, 'retry budget exhausted'], [{ type: 'dsh_result', summary: marker.summary }], 'Further automatic changes may compound the failure', 'Wait for approve, retry, or reject'));
                return;
            }
            const failureTarget = route(state, 'fail');
            if (failureTarget === roleNode(state, 'human_gate')) {
                const waiting = transition(agent, state, { status: 'WAITING_HITL', node: failureTarget, hitlReason: 'FAILURE_REVIEW' }, 'active LoopSpec escalated explicit DSH failure to human review');
                emitDecision(agent, waiting, decision('HITL_REQUIRED', 'Why escalate this explicit Agent failure?', 'wait_for_human', [marker.summary, 'active LoopSpec routes execute/fail to human_gate'], [{ type: 'dsh_result', summary: marker.summary }, { type: 'loopspec_route', source: state.node, outcome: 'fail', target: failureTarget, specHash: state.loopSpecHash }], 'Automatic retry would repeat a failure the active policy classifies as non-recoverable', 'Wait for human retry or rejection'));
                return;
            }
            const retried = transition(agent, state, { node: failureTarget, attempt: state.attempt + 1, candidateFingerprint: null, preparedCandidate: null }, 'DSH reported a failed result');
            emitDecision(agent, retried, decision('RETRY', 'Why retry?', 'retry', [marker.summary, 'retry budget remains'], [{ type: 'dsh_result', summary: marker.summary }], 'The next DSH turn may modify more files', 'Apply the failure feedback and re-run the task'));
            agent.followup(createUserMessage({ content: [{ type: 'text', text: `${workflowPrompt(retried)}\nPrevious failure feedback: ${marker.summary}` }], source: { kind: 'plugin', plugin: name } }));
        });
        return;
    }
    let verifying;
    const beganVerification = await applyToCurrentExecution(agent, state, state.node, () => {
        const verify = roleNode(state, 'verify');
        verifying = transition(agent, state, { node: state.node === verify ? verify : route(state, 'pass') }, 'DSH reported a candidate result');
    });
    if (!beganVerification || !verifying)
        return;
    const cwd = agent.session.header.cwd;
    if (!cwd) {
        await applyToCurrentExecution(agent, verifying, roleNode(verifying, 'verify'), () => {
            const waiting = transition(agent, verifying, { status: 'WAITING_HITL', node: roleNode(verifying, 'human_gate'), hitlReason: 'QUALITY_REVIEW' }, 'acceptance verification requires a workspace');
            emitDecision(agent, waiting, decision('HITL_REQUIRED', 'Why require human review?', 'wait_for_human', ['Session has no workspace for acceptance commands'], [], 'No independent command evidence exists', 'Provide a verifiable workspace'));
        });
        return;
    }
    if (verifying.evolution?.kind === 'loopspec') {
        try {
            assertCandidateFile(cwd, verifying.evolution.candidatePath);
            const candidateSpec = loadLoopSpec(verifying.evolution.candidatePath);
            if (candidateSpec.spec_id !== verifying.loopSpec.spec_id || candidateSpec.revision !== verifying.loopSpec.revision + 1 || candidateSpec.predecessor_hash !== verifying.loopSpecHash)
                throw new Error('LoopSpec candidate does not bind the active predecessor');
            let bound;
            const applied = await applyToCurrentExecution(agent, verifying, roleNode(verifying, 'verify'), () => {
                bound = transition(agent, verifying, { candidateLoopSpec: candidateSpec }, 'LoopSpec candidate passed schema, graph, and predecessor validation');
                appendLoopEvent(agent, { workflowId: verifying.workflowId, kind: 'evidence', evidence: { type: 'loopspec_gate', passed: true, specId: candidateSpec.spec_id, revision: candidateSpec.revision, predecessorHash: candidateSpec.predecessor_hash, candidateHash: loopSpecHash(candidateSpec) } });
            });
            if (!applied || !bound)
                return;
            verifying = bound;
        }
        catch (error) {
            await applyToCurrentExecution(agent, verifying, roleNode(verifying, 'verify'), () => {
                const waiting = transition(agent, verifying, { status: 'WAITING_HITL', node: route(verifying, 'exhausted'), hitlReason: 'QUALITY_REVIEW' }, 'LoopSpec candidate failed graph or predecessor validation');
                emitDecision(agent, waiting, decision('LOOPSPEC_GATE_FAILED', 'Why stop this evolution candidate?', 'wait_for_human', [error instanceof Error ? error.message : String(error)], [], 'An invalid graph cannot become active', 'Reject or request a corrected LoopSpec candidate'));
            });
            return;
        }
    }
    const commands = verifying.acceptance.commands ?? [];
    const commandVerification = verifying.evolution?.kind === 'loopspec' && commands.length === 0 ? { passed: true, evidence: [] } : await verifyCommands(cwd, commands);
    const appliedCommands = await applyToCurrentExecution(agent, verifying, roleNode(verifying, 'verify'), () => {
        appendLoopEvent(agent, { workflowId: verifying.workflowId, kind: 'evidence', evidence: { type: 'acceptance_commands', passed: commandVerification.passed, commands: commandVerification.evidence } });
        if (!commandVerification.passed) {
            const summary = commands.length === 0 ? 'No acceptance commands were configured' : 'At least one acceptance command failed';
            if (verifying.attempt >= verifying.maxAttempts) {
                const waiting = transition(agent, verifying, { status: 'WAITING_HITL', node: route(verifying, 'exhausted'), hitlReason: 'QUALITY_REVIEW' }, summary);
                emitDecision(agent, waiting, decision('HITL_REQUIRED', 'Why require human review?', 'wait_for_human', [summary], [{ type: 'acceptance_commands', commands: commandVerification.evidence }], 'Promotion without independent command evidence would be synthetic success', 'Review or correct the acceptance contract'));
                return;
            }
            const retry = transition(agent, verifying, { node: route(verifying, 'retry'), attempt: verifying.attempt + 1, candidateFingerprint: null, preparedCandidate: null }, summary);
            emitDecision(agent, retry, decision('RETRY', 'Why retry after command verification?', 'retry', [summary], [{ type: 'acceptance_commands', commands: commandVerification.evidence }], 'The next attempt may modify the workspace again', 'Correct the implementation or test environment'));
            agent.followup(createUserMessage({ content: [{ type: 'text', text: `${workflowPrompt(retry)}\nIndependent acceptance evidence:\n${JSON.stringify(commandVerification.evidence)}` }], source: { kind: 'plugin', plugin: name } }));
        }
    });
    if (!appliedCommands || !commandVerification.passed)
        return;
    const gate = await runDoublecheckGate(ctx, agent, signal);
    const appliedGate = await applyToCurrentExecution(agent, verifying, roleNode(verifying, 'verify'), () => {
        appendLoopEvent(agent, { workflowId: verifying.workflowId, kind: 'evidence', evidence: { type: 'doublecheck_gate', passed: gate.passed, output: gate.text.slice(-4000) } });
        if (!gate.passed) {
            if (verifying.attempt >= verifying.maxAttempts) {
                const waiting = transition(agent, verifying, { status: 'WAITING_HITL', node: route(verifying, 'exhausted'), hitlReason: 'QUALITY_REVIEW' }, 'doublecheck gate rejected the candidate');
                emitDecision(agent, waiting, decision('HITL_REQUIRED', 'Why require human review?', 'wait_for_human', ['dsh-doublecheck gate rejected the delivery'], [{ type: 'doublecheck_gate', output: gate.text }], 'Promotion would bypass the quality gate', 'Wait for human review'));
                return;
            }
            const retry = transition(agent, verifying, { node: route(verifying, 'retry'), attempt: verifying.attempt + 1, candidateFingerprint: null, preparedCandidate: null }, 'doublecheck gate rejected the candidate');
            emitDecision(agent, retry, decision('RETRY', 'Why retry after the quality gate?', 'retry', ['dsh-doublecheck returned a rework result'], [{ type: 'doublecheck_gate', output: gate.text }], 'The next change may broaden the diff', 'Ask DSH to rework the delivery'));
            agent.followup(createUserMessage({ content: [{ type: 'text', text: `${workflowPrompt(retry)}\nQuality gate feedback:\n${gate.text}` }], source: { kind: 'plugin', plugin: name } }));
        }
    });
    if (!appliedGate || !gate.passed)
        return;
    const reportsArchived = await applyToCurrentExecution(agent, verifying, roleNode(verifying, 'verify'), () => {
        const reports = archiveReports(cwd, ['gate-report.md', 'doublecheck-spec.md', 'doublecheck-report.md']);
        for (const report of reports)
            appendLoopEvent(agent, { workflowId: verifying.workflowId, kind: 'evidence', evidence: { type: 'archived_report', path: report.path, content: report.content.slice(-10000), verdict: 'deliverable' } });
    });
    if (!reportsArchived)
        return;
    const changed = await gitChangedFiles(cwd);
    const allowed = verifying.acceptance.allowedFiles ?? [];
    const scopePassed = allowed.length > 0 && changed.length > 0 && changed.every(file => allowed.includes(file));
    const verified = verifying;
    await applyToCurrentExecution(agent, verified, roleNode(verified, 'verify'), async () => {
        appendLoopEvent(agent, { workflowId: verified.workflowId, kind: 'evidence', evidence: { type: 'git_scope', changedFiles: changed, allowedFiles: allowed, passed: scopePassed } });
        if (!scopePassed) {
            const waiting = transition(agent, verified, { node: route(verified, 'approve'), status: 'WAITING_HITL', hitlReason: 'SCOPE_REVIEW' }, 'Git scope is empty, unbounded, or exceeds allowed files');
            emitDecision(agent, waiting, decision('HITL_REQUIRED', 'Why require human review?', 'wait_for_human', ['Git scope did not satisfy the explicit allowed-file contract'], [{ type: 'git_scope', changedFiles: changed, allowedFiles: allowed }], 'Promotion could include unrelated or unverifiable changes', 'Review the diff manually'));
            return;
        }
        if (verified.evolution?.kind === 'loopspec')
            assertCandidateFile(cwd, verified.evolution.candidatePath);
        const prepared = await prepareGitCandidate(cwd, `${config.requirePromotionApproval ? 'loopgraph: human-approved promote' : 'loopgraph: promote'} ${verified.workflowId} attempt ${verified.attempt}`);
        const preparedState = { sha: prepared.sha, tree: prepared.tree, parent: prepared.parent, files: prepared.files };
        if (config.requirePromotionApproval || verified.evolution?.kind === 'loopspec') {
            const waiting = transition(agent, verified, { node: route(verified, 'approve'), status: 'WAITING_HITL', hitlReason: 'PROMOTION_REVIEW', candidateFingerprint: prepared.fingerprint, preparedCandidate: preparedState }, 'verified candidate requires human promotion approval');
            emitDecision(agent, waiting, decision('PROMOTION_REVIEW_REQUIRED', 'Why pause before Git commit?', 'wait_for_human', ['Acceptance commands passed', 'dsh-doublecheck gate passed', 'Git scope passed', 'Human review is required by policy'], [{ type: 'git_scope', changedFiles: changed, allowedFiles: allowed }, { type: 'acceptance_commands', commands: commandVerification.evidence }, { type: 'approval_binding', attempt: verified.attempt, specRevision: verified.specRevision, candidateFingerprint: prepared.fingerprint, candidateCommit: prepared.sha }], 'The human must inspect AI-authored changes before they become a version', 'Approve only this spec revision and immutable candidate snapshot'));
            return;
        }
        if (await gitCandidateFingerprint(cwd) !== prepared.fingerprint)
            throw new Error('candidate changed after verification; refusing automatic promotion');
        const sha = await promotePreparedCandidate(cwd, prepared, `loopgraph-${verified.workflowId}-${verified.attempt}`);
        let promoted = { ...verified, candidateCommit: sha };
        appendLoopEvent(agent, { workflowId: promoted.workflowId, kind: 'evidence', evidence: { type: 'git_candidate', sha, files: prepared.files } });
        promoted = transition(agent, promoted, promotedFields(promoted, sha, 'auto_promote'), 'verification and Git evidence passed');
        emitDecision(agent, promoted, decision('PROMOTE', 'Why promote this candidate?', 'promote', ['DSH result passed', 'dsh-doublecheck gate passed', 'Git scope passed'], [{ type: 'candidate_commit', sha: promoted.candidateCommit ?? '' }], 'Promotion makes the candidate active', 'Mark the candidate as the active workflow version'));
        settleTodos(agent, true);
        await releaseWorkspaceLock(cwd, promoted.workflowId);
    });
}
function lastAssistantText(agent) {
    const event = [...agent.session.events].reverse().find(item => item.type === 'assistant/message');
    return event?.type === 'assistant/message' ? assistantText(event) : '';
}
function loopLogs(agent, limit = 20) {
    return loadLoopEvents(agent)
        .slice(-Math.max(1, Math.min(limit, 50)))
        .map((payload, index) => {
        if (payload.kind === 'state') {
            const state = payload.state;
            return { index, kind: 'state', workflowId: payload.workflowId, status: state?.status, node: state?.node, attempt: state?.attempt, candidateCommit: state?.candidateCommit };
        }
        if (payload.kind === 'decision') {
            const item = payload.decision;
            return { index, kind: 'decision', workflowId: payload.workflowId, type: item?.type, decision: item?.decision, rationale: item?.rationale, evidence: item?.evidence, risk: item?.risk, expectedEffect: item?.expectedEffect, at: item?.at };
        }
        const evidence = { ...payload.evidence };
        if (typeof evidence.output === 'string' && evidence.output.length > 1000)
            evidence.output = `${evidence.output.slice(0, 1000)}...`;
        return { index, kind: 'evidence', workflowId: payload.workflowId, evidence };
    });
}
export function apply(ctx, config) {
    const activeLoopSpec = loadLoopSpec(config.loopSpecPath || undefined);
    const recover = async (agent, current, action, signal = new AbortController().signal) => {
        const cwd = agent.session.header.cwd;
        if (!cwd || !current.baselineCommit)
            return { kind: 'error', text: 'recovery requires a Git workspace and baseline commit' };
        if (action === 'verify-existing') {
            let running;
            await withLoopOperationLock(agent, async () => {
                const state = getState(agent);
                if (!state || state.status !== 'UNCERTAIN')
                    return;
                const verifying = transition(agent, state, { status: 'RUNNING', node: roleNode(state, 'verify'), hitlReason: undefined }, 'human chose to verify the uncertain workspace without re-running DSH');
                running = emitDecision(agent, verifying, decision('UNCERTAIN_VERIFY', 'Why verify the existing workspace?', 'verify_existing', ['A human chose independent verification over automatic retry'], [], 'The workspace may contain a partial candidate', 'Run acceptance, Gate, and Git scope against the existing files'));
            });
            if (!running)
                return { kind: 'error', text: 'recover requires UNCERTAIN status' };
            await handleAssistantResult(ctx, config, agent, running, 'LOOPGRAPH_RESULT: {"status":"pass","summary":"Human requested verification of an uncertain existing workspace."}', signal);
            return { kind: 'success', text: 'uncertain workspace verification completed; inspect /loop status' };
        }
        if (current.status !== 'UNCERTAIN')
            return { kind: 'error', text: 'recover requires UNCERTAIN status' };
        if (action === 'retry-same-attempt') {
            const running = transition(agent, current, { status: 'RUNNING', node: roleNode(current, 'execute'), hitlReason: undefined }, 'human accepted possible duplicate effects and requested same-attempt retry');
            emitDecision(agent, running, decision('UNCERTAIN_RETRY', 'Why retry this uncertain attempt?', 'retry_same_attempt', ['A human explicitly accepted possible duplicate external effects'], [], 'The DSH turn may repeat file or tool side effects', 'Re-run the same workflow attempt with its original contract'));
            agent.followup(createUserMessage({ content: [{ type: 'text', text: `${workflowPrompt(running)}\nThis is an explicitly approved retry of an uncertain attempt. Inspect the existing workspace before changing anything.` }], source: { kind: 'plugin', plugin: name } }));
            return { kind: 'success', text: 'uncertain attempt retry queued' };
        }
        if (action === 'restore-baseline') {
            const cleanup = await rejectCandidate(cwd, current.baselineCommit, current.acceptance.allowedFiles ?? [], ['gate-report.md', 'doublecheck-spec.md', 'doublecheck-report.md']);
            for (const report of cleanup.removedReports)
                appendLoopEvent(agent, { workflowId: current.workflowId, kind: 'evidence', evidence: { type: 'archived_report', path: report.path, content: report.content.slice(-10000) } });
            const failed = transition(agent, current, { status: 'FAILED', node: roleNode(current, 'failed'), hitlReason: undefined }, 'human restored the uncertain workspace baseline');
            emitDecision(agent, failed, decision('UNCERTAIN_RESTORE', 'Why restore the baseline?', 'restore_baseline', ['A human chose to discard uncertain effects'], [{ type: 'cleanup', restoredFiles: cleanup.restoredFiles }], 'The uncertain candidate is discarded', 'Return the workspace to the recorded baseline'));
            await releaseWorkspaceLock(cwd, current.workflowId);
            return { kind: 'success', text: `restored baseline; files: ${cleanup.restoredFiles.join(', ') || 'none'}` };
        }
        if (action === 'abort-preserve') {
            const failed = transition(agent, current, { status: 'FAILED', node: roleNode(current, 'failed'), hitlReason: undefined }, 'human aborted while preserving the uncertain workspace');
            emitDecision(agent, failed, decision('UNCERTAIN_ABORT', 'Why preserve the workspace?', 'abort_preserve', ['A human requested forensic preservation'], [{ type: 'workspace', cwd }], 'The workspace remains dirty and requires manual handling', 'Stop automatic execution without hiding possible effects'));
            await releaseWorkspaceLock(cwd, current.workflowId);
            return { kind: 'success', text: 'aborted; uncertain workspace preserved' };
        }
        return { kind: 'error', text: 'recover action must be verify-existing, retry-same-attempt, restore-baseline, or abort-preserve' };
    };
    const promptRecovery = async (agent, state) => {
        try {
            const answer = await ctx.userQuestions.ask({ agent, questions: [{ id: 'loopgraph-recovery', header: 'LoopGraph 恢复', question: '上一次执行在结果确认前中断，如何处理？', detail: `工作流 ${state.workflowId} 可能已经修改 workspace。系统不会自动重试。`, options: [{ label: '验证现状', description: '不重跑 DSH，直接验证现有 workspace' }, { label: '同 Token 重试', description: '接受可能重复副作用并重跑' }, { label: '恢复 Baseline', description: '归档报告并丢弃候选修改' }, { label: '终止并保留现场', description: '停止流程，保留 workspace 供人工取证' }] }] });
            const selected = answer.answers[0]?.selected[0];
            const actions = { '验证现状': 'verify-existing', '同 Token 重试': 'retry-same-attempt', '恢复 Baseline': 'restore-baseline', '终止并保留现场': 'abort-preserve' };
            if (selected && actions[selected])
                await recover(agent, getState(agent) ?? state, actions[selected]);
        }
        catch (error) {
            appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'evidence', evidence: { type: 'recovery_prompt_unavailable', error: error instanceof Error ? error.message : String(error), command: '/loop recover <action>' } });
        }
    };
    ctx.on('agent/created', async ({ agent }) => {
        let uncertain;
        await withLoopOperationLock(agent, async () => {
            const restored = foldState(agent);
            if (!restored)
                return;
            if (restored.status === 'RUNNING') {
                const waiting = transition(agent, restored, { status: 'UNCERTAIN', node: roleNode(restored, 'human_gate'), hitlReason: 'UNCERTAIN_RECOVERY' }, 'DSH restarted before the workflow result was durably settled');
                uncertain = emitDecision(agent, waiting, decision('UNCERTAIN', 'Why stop automatic recovery?', 'wait_for_human', ['A recovered RUNNING workflow may already have external side effects'], [], 'Automatic retry could duplicate file or tool effects', 'Ask a human to verify, retry, restore, or abort'));
            }
            else {
                states.set(agent, restored);
                const cwd = agent.session.header.cwd;
                if (cwd && restored.status === 'COMPLETED')
                    saveWorkspaceLoopSpec(cwd, restored.loopSpec);
            }
        });
        if (uncertain)
            void promptRecovery(agent, uncertain);
    });
    ctx.on('agent/disposed', ({ agent }) => {
        states.delete(agent);
    });
    ctx.on('session/event', async (session, event) => {
        const agent = ctx.agents.list().find((candidate) => candidate.session === session);
        if (!agent)
            return;
        if (!event)
            return;
        const state = getState(agent);
        if (!state || state.status !== 'RUNNING' || event.type !== 'assistant/message')
            return;
        const text = assistantText(event);
        appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'evidence', evidence: { type: 'assistant_message', attempt: state.attempt, chars: text.length } });
    });
    ctx.on('agent/turn-stopping', async ({ agent, signal }) => {
        const state = getState(agent);
        if (!state || state.status !== 'RUNNING' || state.node !== roleNode(state, 'execute'))
            return;
        await handleAssistantResult(ctx, config, agent, state, lastAssistantText(agent), signal);
    });
    ctx.commands.register({
        name: 'loop',
        description: 'Start, evolve, inspect, pause, resume, approve, reject, or explain a LoopGraph workflow.',
        input: { hint: 'start <goal> | evolve <feedback> | status | logs | pause | resume | retry [feedback] | recover | approve [comment] | reject | explain' },
        recordInput: false,
        handler: async ({ agent, rawInput, signal }) => {
            if (signal.aborted)
                return { kind: 'error', text: 'loop command aborted' };
            const input = rawInput.trim();
            const separator = input.search(/\s/);
            const action = (separator === -1 ? input : input.slice(0, separator)) || 'status';
            const actionInput = separator === -1 ? '' : input.slice(separator).trim();
            const parts = actionInput ? actionInput.split(/\s+/) : [];
            const current = getState(agent);
            if (action === 'start') {
                if (config.workflowName)
                    return { kind: 'error', text: `configured workflow ${config.workflowName} is disabled until terminal outcome, retry, pause/resume, restart reconciliation, and workspace adoption are implemented` };
                return withLoopOperationLock(agent, async () => {
                    const active = getState(agent);
                    if (active && ['RUNNING', 'UNCERTAIN', 'PAUSED', 'WAITING_HITL'].includes(active.status))
                        return { kind: 'error', text: `workflow ${active.workflowId} is still ${active.status}` };
                    const input = parseStart(actionInput, config.maxAttempts);
                    const cwd = agent.session.header.cwd;
                    if (!cwd)
                        return { kind: 'error', text: 'loop start requires a Git workspace' };
                    let baseline;
                    try {
                        baseline = await gitHead(cwd);
                    }
                    catch (error) {
                        return { kind: 'error', text: error instanceof Error ? error.message : String(error) };
                    }
                    const workflowId = `dsh-${agent.id}-${Date.now().toString(36)}`;
                    const baselineVersion = `version:${workflowId}:baseline`;
                    const workflowSpec = loadWorkspaceLoopSpec(cwd, active?.loopSpec ?? activeLoopSpec);
                    if (input.maxAttempts > workflowSpec.max_iterations)
                        return { kind: 'error', text: `maxAttempts ${input.maxAttempts} exceeds active LoopSpec limit ${workflowSpec.max_iterations}` };
                    const state = { ...initialState(workflowId, input.goal, input.maxAttempts, input.acceptance, workflowSpec), specRevision: specRevision(input.goal, input.maxAttempts, input.acceptance), baselineCommit: baseline, activeVersion: baselineVersion, versions: [{ id: baselineVersion, sha: baseline, status: 'BASELINE', loopSpec: workflowSpec }] };
                    const existingChanges = await gitChangedFiles(cwd);
                    if (existingChanges.length > 0)
                        return { kind: 'error', text: `loop start requires a clean Git workspace; found: ${existingChanges.join(', ')}` };
                    try {
                        await acquireWorkspaceLock(cwd, workflowId);
                    }
                    catch (error) {
                        return { kind: 'error', text: error instanceof Error ? error.message : String(error) };
                    }
                    setState(agent, state);
                    appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'state', state });
                    const planned = emitDecision(agent, state, decision('PLAN', 'Why start this workflow?', 'start', ['Human explicitly requested /loop start'], [], 'DSH may modify the workspace', 'Create a durable DSH-native workflow state'));
                    const running = transition(agent, { ...planned, status: 'RUNNING' }, { status: 'RUNNING', node: workflowSpec.entrypoint, attempt: 1 }, 'workflow started');
                    running && agent.followup(createUserMessage({ content: [{ type: 'text', text: workflowPrompt(running) }], source: { kind: 'plugin', plugin: name } }));
                    return { kind: 'success', text: `started ${running.workflowId} attempt ${running.attempt}` };
                });
            }
            if (action === 'evolve') {
                if (!actionInput)
                    return { kind: 'error', text: 'usage: /loop evolve <LoopSpec improvement request>' };
                if (!config.loopSpecPath)
                    return { kind: 'error', text: 'evolve requires configured loopSpecPath' };
                return withLoopOperationLock(agent, async () => {
                    const active = getState(agent);
                    if (active && ['RUNNING', 'UNCERTAIN', 'PAUSED', 'WAITING_HITL'].includes(active.status))
                        return { kind: 'error', text: `workflow ${active.workflowId} is still ${active.status}` };
                    const cwd = agent.session.header.cwd;
                    if (!cwd)
                        return { kind: 'error', text: 'loop evolve requires a Git workspace' };
                    const activeSpec = loadWorkspaceLoopSpec(cwd, active?.loopSpec ?? activeLoopSpec);
                    if (config.maxAttempts > activeSpec.max_iterations)
                        return { kind: 'error', text: `maxAttempts ${config.maxAttempts} exceeds active LoopSpec limit ${activeSpec.max_iterations}` };
                    const activePath = resolve(config.loopSpecPath);
                    const candidatePath = join(dirname(activePath), `v${activeSpec.revision + 1}.json`);
                    const candidateRelative = relative(cwd, candidatePath);
                    if (!candidateRelative || candidateRelative.startsWith('..') || isAbsolute(candidateRelative))
                        return { kind: 'error', text: 'LoopSpec candidate path must be inside the Git workspace' };
                    const realWorkspace = realpathSync(cwd);
                    const realParent = realpathSync(dirname(candidatePath));
                    if (relative(realWorkspace, realParent).startsWith('..'))
                        return { kind: 'error', text: 'LoopSpec candidate real path escapes the Git workspace' };
                    if (existsSync(candidatePath) && lstatSync(candidatePath).isSymbolicLink())
                        return { kind: 'error', text: 'LoopSpec candidate path cannot be a symlink' };
                    const existingChanges = await gitChangedFiles(cwd);
                    if (existingChanges.length > 0)
                        return { kind: 'error', text: `loop evolve requires a clean Git workspace; found: ${existingChanges.join(', ')}` };
                    const baseline = await gitHead(cwd);
                    const workflowId = `evolve-${agent.id}-${Date.now().toString(36)}`;
                    const goal = `Propose a bounded LoopSpec revision for this request: ${actionInput}. Write exactly one candidate artifact to ${candidateRelative}. Preserve spec_id, set revision to ${activeSpec.revision + 1}, and set predecessor_hash to ${loopSpecHash(activeSpec)}. Do not edit the active LoopSpec.`;
                    const acceptance = { commands: [], allowedFiles: [candidateRelative] };
                    const baselineVersion = `version:${workflowId}:baseline`;
                    const state = { ...initialState(workflowId, goal, config.maxAttempts, acceptance, activeSpec), specRevision: specRevision(goal, config.maxAttempts, acceptance), baselineCommit: baseline, activeVersion: baselineVersion, versions: [{ id: baselineVersion, sha: baseline, status: 'BASELINE', loopSpec: activeSpec }], evolution: { kind: 'loopspec', candidatePath, predecessorHash: loopSpecHash(activeSpec) } };
                    try {
                        await acquireWorkspaceLock(cwd, workflowId);
                    }
                    catch (error) {
                        return { kind: 'error', text: error instanceof Error ? error.message : String(error) };
                    }
                    setState(agent, state);
                    appendLoopEvent(agent, { workflowId, kind: 'state', state });
                    const planned = emitDecision(agent, state, decision('EVOLUTION_REQUESTED', 'Why evolve this LoopSpec?', 'propose_candidate', ['Human explicitly requested /loop evolve', actionInput], [{ type: 'loopspec_baseline', revision: activeSpec.revision, hash: loopSpecHash(activeSpec) }], 'DSH may propose an invalid graph, but cannot write the active pointer', 'Create and independently validate one immutable LoopSpec candidate'));
                    const running = transition(agent, { ...planned, status: 'RUNNING' }, { status: 'RUNNING', node: activeSpec.entrypoint, attempt: 1 }, 'LoopSpec evolution started');
                    agent.followup(createUserMessage({ content: [{ type: 'text', text: workflowPrompt(running) }], source: { kind: 'plugin', plugin: name } }));
                    return { kind: 'success', text: `started LoopSpec evolution ${workflowId}; candidate ${candidateRelative}` };
                });
            }
            if (!current)
                return { kind: 'error', text: 'no active loop workflow' };
            const locked = (operation) => withLoopOperationLock(agent, async () => {
                const state = getState(agent);
                return state ? operation(state) : { kind: 'error', text: 'no active loop workflow' };
            });
            if (action === 'status') {
                if (current.status === 'COMPLETED')
                    settleTodos(agent, true);
                if (current.status === 'FAILED')
                    settleTodos(agent, false);
                return { kind: 'success', text: JSON.stringify(getState(agent), null, 2) };
            }
            if (action === 'logs') {
                const requested = Number(parts[0] ?? 20);
                const limit = Number.isSafeInteger(requested) ? requested : 20;
                return { kind: 'success', text: JSON.stringify({ state: getState(agent), events: loopLogs(agent, limit) }, null, 2) };
            }
            if (action === 'explain')
                return { kind: 'success', text: JSON.stringify({ state: getState(agent), recentEvents: loopLogs(agent, 20) }, null, 2) };
            if (action === 'pause') {
                return locked(async (state) => {
                    if (state.status !== 'RUNNING')
                        return { kind: 'error', text: 'pause requires RUNNING status' };
                    transition(agent, state, { status: 'PAUSED' }, 'human pause requested');
                    agent.cancel({ kind: 'user' }, { keepInbox: true });
                    return { kind: 'success', text: 'loop paused; pending work retained' };
                });
            }
            if (action === 'resume') {
                return locked(async (state) => {
                    if (state.status !== 'PAUSED')
                        return { kind: 'error', text: 'resume requires PAUSED status' };
                    const resumed = transition(agent, state, { status: 'RUNNING' }, 'human resume requested');
                    agent.followup(createUserMessage({ content: [{ type: 'text', text: workflowPrompt(resumed) }], source: { kind: 'plugin', plugin: name } }));
                    return { kind: 'success', text: 'loop resumed' };
                });
            }
            if (action === 'retry') {
                return locked(async (state) => {
                    if (state.status !== 'WAITING_HITL')
                        return { kind: 'error', text: 'retry requires WAITING_HITL status' };
                    const humanFeedback = actionInput;
                    const gateEvidence = loadLoopEvents(agent).slice().reverse().find(event => event.kind === 'evidence' && event.evidence?.type === 'doublecheck_gate')?.evidence;
                    const gateFeedback = typeof gateEvidence?.output === 'string' ? gateEvidence.output.slice(-5000) : 'No gate report was recorded.';
                    const retried = transition(agent, state, { status: 'RUNNING', node: route(state, 'retry'), attempt: state.attempt + 1, candidateFingerprint: null, preparedCandidate: null }, 'human requested HITL retry');
                    appendLoopEvent(agent, { workflowId: retried.workflowId, kind: 'evidence', evidence: { type: 'improvement_proposal', basedOnAttempt: state.attempt, problem: gateFeedback, hypothesis: 'Address the quality gate findings without widening the approved file scope', humanFeedback, expectedEvidence: ['acceptance commands pass', 'doublecheck verdict is deliverable', 'Git scope remains bounded'] } });
                    emitDecision(agent, retried, decision('HITL_RETRY', 'Why retry after human review?', 'retry', ['Human explicitly requested another attempt', 'The prior gate supplied actionable rework evidence'], [{ type: 'human_feedback', text: humanFeedback }, { type: 'doublecheck_gate', output: gateFeedback }], 'The next turn may modify the workspace again', 'Apply the improvement proposal and re-run every gate'));
                    agent.followup(createUserMessage({ content: [{ type: 'text', text: `${workflowPrompt(retried)}\n\nPrior quality gate report:\n${gateFeedback}\n\nHuman feedback:\n${humanFeedback || 'Resolve every red gate item and preserve the allowed-file scope.'}` }], source: { kind: 'plugin', plugin: name } }));
                    return { kind: 'success', text: `retrying attempt ${retried.attempt}` };
                });
            }
            if (action === 'recover') {
                if (parts[0] === 'verify-existing')
                    return recover(agent, current, parts[0], signal);
                return locked(state => recover(agent, state, parts[0] ?? '', signal));
            }
            if (action === 'approve') {
                return locked(async (state) => {
                    if (state.status !== 'WAITING_HITL' || state.hitlReason !== 'PROMOTION_REVIEW')
                        return { kind: 'error', text: 'approve requires a verified PROMOTION_REVIEW state' };
                    const cwd = agent.session.header.cwd;
                    if (!cwd)
                        return { kind: 'error', text: 'current session has no Git workspace' };
                    const humanComment = actionInput;
                    try {
                        if (!state.specRevision || state.specRevision !== specRevision(state.goal, state.maxAttempts, state.acceptance))
                            return { kind: 'error', text: 'approval spec revision does not match the current workflow contract' };
                        if (!state.candidateFingerprint || state.candidateFingerprint !== await gitCandidateFingerprint(cwd))
                            return { kind: 'error', text: 'candidate changed after review; re-run verification before approval' };
                        if (!state.preparedCandidate)
                            return { kind: 'error', text: 'approval has no immutable prepared candidate' };
                        if (state.evolution?.kind === 'loopspec') {
                            if (!state.candidateLoopSpec || state.candidateLoopSpec.revision !== state.loopSpec.revision + 1 || state.candidateLoopSpec.predecessor_hash !== state.loopSpecHash)
                                return { kind: 'error', text: 'approval LoopSpec candidate no longer binds the active predecessor' };
                            assertCandidateFile(cwd, state.evolution.candidatePath);
                            const reparsed = loadLoopSpec(state.evolution.candidatePath);
                            if (loopSpecHash(reparsed) !== loopSpecHash(state.candidateLoopSpec))
                                return { kind: 'error', text: 'LoopSpec candidate changed after graph validation; re-run every gate' };
                        }
                        const candidate = { ...state.preparedCandidate, fingerprint: state.candidateFingerprint };
                        const sha = await promotePreparedCandidate(cwd, candidate, `loopgraph-${state.workflowId}-${state.attempt}`);
                        const candidateEvidence = { type: 'git_candidate', sha, files: candidate.files, approvedBy: 'human', ...(state.candidateLoopSpec ? { loopSpecRevision: state.candidateLoopSpec.revision, loopSpecHash: loopSpecHash(state.candidateLoopSpec), predecessorHash: state.candidateLoopSpec.predecessor_hash } : {}), ...(humanComment ? { humanComment } : {}) };
                        appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'evidence', evidence: candidateEvidence });
                        const promoted = transition(agent, state, promotedFields(state, sha, 'approve'), 'human approved verified candidate');
                        if (state.candidateLoopSpec)
                            saveWorkspaceLoopSpec(cwd, state.candidateLoopSpec);
                        const rationale = ['Human explicitly approved after verification and diff review', ...(humanComment ? [`Human comment: ${humanComment}`] : [])];
                        const evidence = [{ type: 'git_candidate', sha, files: candidate.files }, ...(state.candidateLoopSpec ? [{ type: 'loopspec_activation', revision: state.candidateLoopSpec.revision, hash: loopSpecHash(state.candidateLoopSpec), predecessorHash: state.candidateLoopSpec.predecessor_hash }] : []), ...(humanComment ? [{ type: 'human_comment', text: humanComment }] : [])];
                        emitDecision(agent, promoted, decision('HUMAN_APPROVE_PROMOTE', 'Why promote this candidate?', 'promote', rationale, evidence, 'Promotion makes AI-authored changes part of Git history', 'Create the candidate commit and mark the workflow completed'));
                        settleTodos(agent, true);
                        await releaseWorkspaceLock(cwd, promoted.workflowId);
                        return { kind: 'success', text: `approved and promoted ${sha}` };
                    }
                    catch (error) {
                        return { kind: 'error', text: error instanceof Error ? error.message : String(error) };
                    }
                });
            }
            if (action === 'reject') {
                return locked(async (state) => {
                    if (state.status !== 'WAITING_HITL')
                        return { kind: 'error', text: 'reject requires WAITING_HITL status' };
                    const cwd = agent.session.header.cwd;
                    if (!cwd || !state.baselineCommit)
                        return { kind: 'error', text: 'reject cleanup requires a Git workspace and baseline commit' };
                    try {
                        const cleanup = await rejectCandidate(cwd, state.baselineCommit, state.acceptance.allowedFiles ?? [], ['gate-report.md', 'doublecheck-spec.md', 'doublecheck-report.md']);
                        for (const report of cleanup.removedReports)
                            appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'evidence', evidence: { type: 'archived_report', path: report.path, content: report.content.slice(-10000) } });
                        appendLoopEvent(agent, { workflowId: state.workflowId, kind: 'evidence', evidence: { type: 'reject_cleanup', restoredFiles: cleanup.restoredFiles, removedReports: cleanup.removedReports.map(report => report.path), baselineCommit: state.baselineCommit } });
                        const rejected = transition(agent, state, { status: 'FAILED', node: route(state, 'reject'), hitlReason: undefined }, 'human rejection restored the candidate baseline');
                        emitDecision(agent, rejected, decision('HUMAN_REJECT', 'Why discard this candidate?', 'reject', ['Human rejected the candidate after reviewing gate evidence', 'Candidate files were restored to the recorded baseline'], [{ type: 'reject_cleanup', restoredFiles: cleanup.restoredFiles, archivedReports: cleanup.removedReports.map(report => report.path) }], 'The rejected candidate is no longer present in the workspace', 'Keep audit evidence while returning the workspace to a clean baseline'));
                        settleTodos(agent, false);
                        await releaseWorkspaceLock(cwd, state.workflowId);
                        return { kind: 'success', text: `loop rejected; restored ${cleanup.restoredFiles.join(', ') || 'no candidate files'}` };
                    }
                    catch (error) {
                        return { kind: 'error', text: error instanceof Error ? error.message : String(error) };
                    }
                });
            }
            if (action === 'rollback') {
                return locked(async (state) => {
                    if (state.status !== 'COMPLETED')
                        return { kind: 'error', text: 'rollback requires COMPLETED status' };
                    const versionId = parts[0];
                    if (!versionId)
                        return { kind: 'error', text: 'usage: /loop rollback <version-id>' };
                    const target = state.versions?.find(version => version.id === versionId);
                    if (!target)
                        return { kind: 'error', text: 'rollback target is not owned by this workflow' };
                    const cwd = agent.session.header.cwd;
                    if (!cwd)
                        return { kind: 'error', text: 'current session has no workspace' };
                    try {
                        await gitRollback(cwd, target.sha);
                        const targetSpec = target.loopSpec ?? state.loopSpec;
                        const rollbackId = `version:${state.workflowId}:rollback:${Date.now().toString(36)}`;
                        const versions = [...(state.versions ?? []), { id: rollbackId, sha: target.sha, parentId: state.activeVersion, status: 'ROLLED_BACK', loopSpec: targetSpec }];
                        const rolled = transition(agent, state, { status: 'COMPLETED', node: nodeForRole(targetSpec, 'complete'), candidateCommit: target.sha, activeVersion: rollbackId, versions, loopSpec: targetSpec, loopSpecHash: loopSpecHash(targetSpec) }, 'human requested workflow-owned Git rollback');
                        saveWorkspaceLoopSpec(cwd, targetSpec);
                        emitDecision(agent, rolled, decision('ROLLBACK', 'Why change the active version?', 'rollback', ['Target version belongs to this workflow', 'Git workspace was clean'], [{ type: 'version', versionId, sha: target.sha }], 'Rollback restores older code behavior', 'Switch the workspace to the requested workflow version'));
                        return { kind: 'success', text: `rolled back to ${versionId} (${target.sha})` };
                    }
                    catch (error) {
                        return { kind: 'error', text: error instanceof Error ? error.message : String(error) };
                    }
                });
            }
            return { kind: 'error', text: `unknown loop action: ${action}` };
        },
    });
}
