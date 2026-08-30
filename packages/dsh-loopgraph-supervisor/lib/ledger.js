import { createHash, randomUUID } from 'node:crypto';
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, statSync, truncateSync, unlinkSync, writeSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
const lockWait = new Int32Array(new SharedArrayBuffer(4));
function ledgerPath(agent) {
    const root = process.env.DSH_HOME ?? join(homedir(), '.dsh');
    const directory = join(root, 'loopgraph');
    if (!existsSync(root)) {
        mkdirSync(root, { recursive: true });
        fsyncDirectory(dirname(root));
    }
    if (!existsSync(directory)) {
        mkdirSync(directory);
        fsyncDirectory(root);
    }
    return join(directory, `${agent.id}.jsonl`);
}
export function appendLoopEvent(agent, event) {
    const path = ledgerPath(agent);
    const lock = acquireExclusiveLock(`${path}.lock`, 'writer');
    try {
        repairTornTail(path);
        const records = readRecords(path);
        const previousChecksum = ledgerTipChecksum(records);
        const body = {
            time: Date.now(),
            sequence: records.length + 1,
            ...(previousChecksum ? { previousChecksum } : {}),
            event,
        };
        const record = { ...body, checksum: checksum(body) };
        const existed = existsSync(path);
        const descriptor = openSync(path, 'a', 0o600);
        try {
            writeAll(descriptor, `${JSON.stringify(record)}\n`);
            fsyncSync(descriptor);
        }
        finally {
            closeSync(descriptor);
        }
        if (!existed)
            fsyncDirectory(dirname(path));
    }
    finally {
        releaseExclusiveLock(lock);
    }
}
export async function withLoopOperationLock(agent, operation) {
    const lock = await acquireExclusiveLockAsync(`${ledgerPath(agent)}.operation.lock`, 'operation');
    try {
        return await operation();
    }
    finally {
        releaseExclusiveLock(lock);
    }
}
function repairTornTail(path) {
    let content;
    try {
        content = readFileSync(path, 'utf8');
    }
    catch (error) {
        if (error.code === 'ENOENT')
            return;
        throw error;
    }
    if (!content || content.endsWith('\n'))
        return;
    const boundary = content.lastIndexOf('\n') + 1;
    try {
        JSON.parse(content.slice(boundary));
        const descriptor = openSync(path, 'a', 0o600);
        try {
            writeAll(descriptor, '\n');
            fsyncSync(descriptor);
        }
        finally {
            closeSync(descriptor);
        }
    }
    catch (error) {
        if (!(error instanceof SyntaxError))
            throw error;
        truncateSync(path, boundary);
        const descriptor = openSync(path, 'r', 0o600);
        try {
            fsyncSync(descriptor);
        }
        finally {
            closeSync(descriptor);
        }
    }
}
export function loadLoopEvents(agent) {
    return readRecords(ledgerPath(agent)).map(record => record.event);
}
function readRecords(path) {
    try {
        const content = readFileSync(path, 'utf8');
        if (!content)
            return [];
        const terminated = content.endsWith('\n');
        const lines = (terminated ? content.slice(0, -1) : content).split('\n');
        const records = [];
        let tip;
        let checksummed = false;
        for (const [index, line] of lines.entries()) {
            try {
                const record = JSON.parse(line);
                validateRecord(record, index, tip, checksummed, records.at(-1));
                records.push(record);
                tip = record.checksum ?? checksum({ ...(tip ? { previousChecksum: tip } : {}), time: record.time, event: record.event });
                checksummed ||= record.checksum !== undefined;
            }
            catch (error) {
                if (!terminated && index === lines.length - 1 && error instanceof SyntaxError)
                    break;
                throw error;
            }
        }
        return records;
    }
    catch (error) {
        const code = error.code;
        if (code === 'ENOENT')
            return [];
        throw error;
    }
}
function validateRecord(record, index, priorChecksum, checksummed, priorRecord) {
    if (!record.event || typeof record.time !== 'number')
        throw new Error(`invalid loop ledger record at line ${index + 1}`);
    if (record.checksum === undefined && record.sequence === undefined) {
        if (checksummed)
            throw new Error(`legacy loop ledger record after checksum chain at line ${index + 1}`);
        return;
    }
    if (record.sequence !== index + 1 || typeof record.checksum !== 'string')
        throw new Error(`invalid loop ledger sequence at line ${index + 1}`);
    const legacyImmediateChecksum = !checksummed && priorRecord
        ? checksum({ time: priorRecord.time, event: priorRecord.event })
        : undefined;
    if (record.previousChecksum !== priorChecksum && record.previousChecksum !== legacyImmediateChecksum)
        throw new Error(`broken loop ledger checksum chain at line ${index + 1}`);
    const body = {
        time: record.time,
        sequence: record.sequence,
        ...(record.previousChecksum ? { previousChecksum: record.previousChecksum } : {}),
        event: record.event,
    };
    if (checksum(body) !== record.checksum)
        throw new Error(`loop ledger checksum mismatch at line ${index + 1}`);
}
function checksum(value) {
    return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}
function ledgerTipChecksum(records) {
    let tip;
    for (const record of records) {
        tip = record.checksum ?? checksum({ ...(tip ? { previousChecksum: tip } : {}), time: record.time, event: record.event });
    }
    return tip;
}
function acquireExclusiveLock(lockPath, label) {
    for (let attempt = 0; attempt < 500; attempt += 1) {
        const lock = tryAcquireExclusiveLock(lockPath);
        if (lock)
            return lock;
        if (staleLock(lockPath) && reapStaleLock(lockPath))
            continue;
        Atomics.wait(lockWait, 0, 0, 10);
    }
    throw new Error(`timed out acquiring loop ledger ${label} lock: ${lockPath}`);
}
async function acquireExclusiveLockAsync(lockPath, label) {
    for (let attempt = 0; attempt < 500; attempt += 1) {
        const lock = tryAcquireExclusiveLock(lockPath);
        if (lock)
            return lock;
        if (staleLock(lockPath) && reapStaleLock(lockPath))
            continue;
        await new Promise(resolve => setTimeout(resolve, 10));
    }
    throw new Error(`timed out acquiring loop ledger ${label} lock: ${lockPath}`);
}
function tryAcquireExclusiveLock(lockPath) {
    let descriptor;
    try {
        descriptor = openSync(lockPath, 'wx', 0o600);
    }
    catch (error) {
        if (error.code === 'EEXIST')
            return undefined;
        throw error;
    }
    const token = randomUUID();
    try {
        writeAll(descriptor, JSON.stringify({ pid: process.pid, token, createdAt: Date.now() }));
        fsyncSync(descriptor);
        return { descriptor, path: lockPath, token };
    }
    catch (error) {
        closeSync(descriptor);
        try {
            unlinkSync(lockPath);
        }
        catch { }
        throw error;
    }
}
function staleLock(path) {
    try {
        const owner = JSON.parse(readFileSync(path, 'utf8'));
        if (typeof owner.pid !== 'number')
            return Date.now() - statSync(path).mtimeMs > 30_000;
        try {
            process.kill(owner.pid, 0);
            return false;
        }
        catch (error) {
            return error.code === 'ESRCH';
        }
    }
    catch (error) {
        if (error.code === 'ENOENT')
            return false;
        try {
            return Date.now() - statSync(path).mtimeMs > 30_000;
        }
        catch (statError) {
            if (statError.code === 'ENOENT')
                return false;
            throw statError;
        }
    }
}
function reapStaleLock(path) {
    const reaperPath = `${path}.reaper`;
    let reaper;
    try {
        reaper = openSync(reaperPath, 'wx', 0o600);
    }
    catch (error) {
        if (error.code === 'EEXIST')
            return false;
        throw error;
    }
    try {
        if (!staleLock(path))
            return false;
        try {
            unlinkSync(path);
        }
        catch (error) {
            if (error.code !== 'ENOENT')
                throw error;
        }
        return true;
    }
    finally {
        closeSync(reaper);
        try {
            unlinkSync(reaperPath);
        }
        catch (error) {
            if (error.code !== 'ENOENT')
                throw error;
        }
    }
}
function releaseExclusiveLock(lock) {
    try {
        const owner = JSON.parse(readFileSync(lock.path, 'utf8'));
        if (owner.token !== lock.token)
            throw new Error(`refusing to release loop ledger lock owned by another writer: ${lock.path}`);
        unlinkSync(lock.path);
    }
    finally {
        closeSync(lock.descriptor);
    }
}
function writeAll(descriptor, text) {
    const buffer = Buffer.from(text, 'utf8');
    let offset = 0;
    while (offset < buffer.length) {
        const written = writeSync(descriptor, buffer, offset, buffer.length - offset);
        if (written <= 0)
            throw new Error('loop ledger write made no progress');
        offset += written;
    }
}
function fsyncDirectory(path) {
    const descriptor = openSync(path, 'r');
    try {
        try {
            fsyncSync(descriptor);
        }
        catch (error) {
            if (!['EINVAL', 'ENOTSUP'].includes(error.code ?? ''))
                throw error;
        }
    }
    finally {
        closeSync(descriptor);
    }
}
export function initialState(workflowId, goal, maxAttempts, acceptance) {
    return { workflowId, status: 'IDLE', node: 'EXECUTE', attempt: 0, maxAttempts, goal, acceptance, decisions: [] };
}
export function foldState(agent, fallback) {
    let state = fallback;
    for (const payload of loadLoopEvents(agent)) {
        if (payload.kind === 'state') {
            state = state ? { ...state, ...payload.state } : payload.state;
        }
        if (payload.kind === 'decision' && state && payload.decision)
            state = { ...state, decisions: [...state.decisions, payload.decision] };
    }
    return state;
}
export function decision(type, question, choice, rationale, evidence, risk, expectedEffect) {
    return { id: `decision:${type}:${Date.now()}`, type, question, decision: choice, rationale, evidence, risk, expectedEffect, at: Date.now() };
}
