import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
function canonical(value) {
    if (Array.isArray(value))
        return `[${value.map(canonical).join(',')}]`;
    if (value && typeof value === 'object')
        return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(',')}}`;
    return JSON.stringify(value);
}
export function loopSpecHash(spec) {
    return createHash('sha256').update(canonical(spec)).digest('hex');
}
export function loadLoopSpec(path = fileURLToPath(new URL('../loopspec.v1.json', import.meta.url))) {
    const raw = JSON.parse(readFileSync(path, 'utf8'));
    const allowed = new Set(['schema_version', 'spec_id', 'revision', 'predecessor_hash', 'entrypoint', 'max_iterations', 'nodes', 'edges']);
    if (Object.keys(raw).some(key => !allowed.has(key)))
        throw new Error('LoopSpec contains unknown fields');
    const value = {
        schema_version: raw.schema_version,
        spec_id: raw.spec_id,
        revision: raw.revision,
        predecessor_hash: raw.predecessor_hash,
        entrypoint: raw.entrypoint,
        max_iterations: raw.max_iterations,
        nodes: raw.nodes,
        edges: raw.edges,
    };
    if (value.nodes.some(node => Object.keys(node).some(key => !['id', 'kind', 'role'].includes(key))) || value.edges.some(edge => Object.keys(edge).some(key => !['source', 'target', 'outcomes'].includes(key))))
        throw new Error('LoopSpec nodes or edges contain unknown fields');
    validateLoopSpec(value);
    return value;
}
function activePath(workspace) {
    const root = process.env.DSH_HOME || join(homedir(), '.dsh');
    const key = createHash('sha256').update(workspace).digest('hex');
    return join(root, 'loopgraph', 'active-specs', `${key}.json`);
}
export function loadWorkspaceLoopSpec(workspace, fallback) {
    const path = activePath(workspace);
    return existsSync(path) ? loadLoopSpec(path) : fallback;
}
export function saveWorkspaceLoopSpec(workspace, spec) {
    validateLoopSpec(spec);
    const path = activePath(workspace);
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    const temporary = `${path}.${process.pid}.tmp`;
    writeFileSync(temporary, `${JSON.stringify(spec, null, 2)}\n`, { mode: 0o600 });
    renameSync(temporary, path);
}
export function nodeForRole(spec, role) {
    const matches = spec.nodes.filter(node => node.role === role || (!node.role && node.id === role));
    if (matches.length !== 1)
        throw new Error(`LoopSpec has no unique runtime role: ${role}`);
    return matches[0].id;
}
export function nextNode(spec, source, outcome, iteration) {
    if (iteration < 0)
        throw new Error('LoopSpec iteration cannot be negative');
    if (iteration >= spec.max_iterations && ['fail', 'retry'].includes(outcome))
        throw new Error(`LoopSpec iteration limit exceeded: ${spec.max_iterations}`);
    const matches = spec.edges.filter(edge => edge.source === source && edge.outcomes.includes(outcome));
    if (matches.length !== 1)
        throw new Error(`LoopSpec has no unique edge for ${source} on ${outcome}`);
    return matches[0].target;
}
export function validateLoopSpec(spec) {
    if (spec.schema_version !== 1 || !spec.spec_id || !Number.isSafeInteger(spec.revision) || spec.revision < 1 || !Number.isSafeInteger(spec.max_iterations) || spec.max_iterations < 1)
        throw new Error('invalid LoopSpec identity or limits');
    const ids = spec.nodes.map(node => node.id);
    if (new Set(ids).size !== ids.length || !ids.includes(spec.entrypoint))
        throw new Error('invalid LoopSpec nodes or entrypoint');
    const kinds = new Set(['dsh_execute', 'verifier', 'human_gate', 'promotion', 'terminal']);
    const validOutcomes = new Set(['pass', 'fail', 'retry', 'approve', 'auto_promote', 'reject', 'exhausted']);
    const requiredOutcomes = { dsh_execute: ['pass', 'fail', 'exhausted'], verifier: ['fail', 'retry', 'approve', 'auto_promote', 'exhausted'], human_gate: ['approve', 'retry', 'reject'], promotion: ['pass'] };
    const routes = new Set();
    const outgoing = new Map();
    const nodeOutcomes = new Map();
    for (const node of spec.nodes) {
        if (!kinds.has(node.kind))
            throw new Error(`unsupported LoopSpec node kind: ${node.kind}`);
        outgoing.set(node.id, new Set());
        nodeOutcomes.set(node.id, new Set());
    }
    for (const edge of spec.edges) {
        if (!ids.includes(edge.source) || !ids.includes(edge.target) || edge.outcomes.length === 0)
            throw new Error('LoopSpec edge references unknown nodes or no outcomes');
        outgoing.get(edge.source)?.add(edge.target);
        for (const outcome of edge.outcomes) {
            if (!validOutcomes.has(outcome))
                throw new Error(`unsupported LoopSpec outcome: ${outcome}`);
            const key = `${edge.source}\0${outcome}`;
            if (routes.has(key))
                throw new Error(`duplicate LoopSpec route: ${edge.source}/${outcome}`);
            routes.add(key);
            nodeOutcomes.get(edge.source)?.add(outcome);
        }
    }
    const reachable = new Set([spec.entrypoint]);
    const queue = [spec.entrypoint];
    while (queue.length)
        for (const target of outgoing.get(queue.shift()) ?? [])
            if (!reachable.has(target)) {
                reachable.add(target);
                queue.push(target);
            }
    if (reachable.size !== ids.length)
        throw new Error('LoopSpec contains unreachable nodes');
    const terminals = spec.nodes.filter(node => node.kind === 'terminal').map(node => node.id);
    if (!terminals.length)
        throw new Error('LoopSpec requires a terminal node');
    for (const node of spec.nodes) {
        const count = outgoing.get(node.id)?.size ?? 0;
        if (node.kind === 'terminal' && count)
            throw new Error(`terminal node has outgoing edges: ${node.id}`);
        if (node.kind !== 'terminal' && !count)
            throw new Error(`non-terminal node has no outgoing edge: ${node.id}`);
        for (const outcome of requiredOutcomes[node.kind] ?? [])
            if (!nodeOutcomes.get(node.id)?.has(outcome))
                throw new Error(`node ${node.id} is missing required outcome: ${outcome}`);
    }
    const canTerminate = new Set(terminals);
    let changed = true;
    while (changed) {
        changed = false;
        for (const [source, targets] of outgoing)
            if (!canTerminate.has(source) && [...targets].some(target => canTerminate.has(target))) {
                canTerminate.add(source);
                changed = true;
            }
    }
    if (canTerminate.size !== ids.length)
        throw new Error('LoopSpec contains nodes with no terminating path');
    if (spec.entrypoint !== nodeForRole(spec, 'execute'))
        throw new Error('LoopSpec entrypoint must have the execute role');
    const roleKinds = { execute: 'dsh_execute', verify: 'verifier', human_gate: 'human_gate', promote: 'promotion', complete: 'terminal', failed: 'terminal' };
    for (const [role, kind] of Object.entries(roleKinds)) {
        const matches = spec.nodes.filter(node => node.role === role);
        if (matches.length !== 1 || matches[0].kind !== kind)
            throw new Error(`LoopSpec role ${role} requires one ${kind} node`);
    }
    const roles = Object.fromEntries(Object.keys(roleKinds).map(role => [role, nodeForRole(spec, role)]));
    const requiredRoutes = [
        ['execute', 'pass', 'verify'], ['execute', 'fail', 'execute'], ['execute', 'exhausted', 'human_gate'],
        ['verify', 'retry', 'execute'], ['verify', 'approve', 'human_gate'], ['verify', 'auto_promote', 'promote'], ['verify', 'exhausted', 'human_gate'],
        ['human_gate', 'approve', 'promote'], ['human_gate', 'retry', 'execute'], ['human_gate', 'reject', 'failed'], ['promote', 'pass', 'complete'],
    ];
    for (const [sourceRole, outcome, targetRole] of requiredRoutes)
        if (nextNode(spec, roles[sourceRole], outcome, 0) !== roles[targetRole])
            throw new Error(`LoopSpec route ${sourceRole}/${outcome} must target ${targetRole}`);
    const verifierFail = nextNode(spec, roles.verify, 'fail', 0);
    if (![roles.execute, roles.human_gate].includes(verifierFail))
        throw new Error('LoopSpec verifier/fail must target execute or human_gate');
}
