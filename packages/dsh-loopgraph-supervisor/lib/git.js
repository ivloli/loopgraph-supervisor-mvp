import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { closeSync, existsSync, lstatSync, openSync, readFileSync, readdirSync, readlinkSync, rmSync, unlinkSync, writeFileSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';
function run(cwd, args) {
    return new Promise((resolve, reject) => {
        execFile('git', ['-C', cwd, ...args], { encoding: 'utf8', maxBuffer: 8 * 1024 * 1024 }, (error, stdout, stderr) => {
            if (error)
                reject(new Error(stderr.trim() || error.message));
            else
                resolve(stdout);
        });
    });
}
export async function gitHead(cwd) {
    return (await run(cwd, ['rev-parse', 'HEAD'])).trim();
}
export async function gitChangedFiles(cwd) {
    const status = await run(cwd, ['status', '--porcelain=v1', '-z']);
    return parsePorcelainPaths(status);
}
export function parsePorcelainPaths(status) {
    if (status.includes('\0')) {
        const records = status.split('\0');
        const paths = [];
        for (let index = 0; index < records.length;) {
            const record = records[index++] ?? '';
            if (record.length < 4)
                continue;
            paths.push(record.slice(3));
            if (/[RC]/.test(record.slice(0, 2))) {
                const original = records[index++];
                if (original)
                    paths.push(original);
            }
        }
        return [...new Set(paths)].sort();
    }
    return status.split('\n').filter(Boolean).map(line => line.slice(3));
}
export async function gitCandidateFingerprint(cwd) {
    const digest = createHash('sha256');
    digest.update(await gitHead(cwd));
    const updatePath = (path, relative) => {
        digest.update('\0path\0').update(relative);
        if (!existsSync(path)) {
            digest.update('\0deleted\0');
            return;
        }
        const stat = lstatSync(path);
        digest.update('\0mode\0').update(String(stat.mode));
        if (stat.isSymbolicLink())
            digest.update('\0symlink\0').update(readlinkSync(path));
        else if (stat.isFile())
            digest.update('\0file\0').update(readFileSync(path));
        else if (stat.isDirectory()) {
            digest.update('\0directory\0');
            for (const name of readdirSync(path).sort())
                updatePath(resolve(path, name), `${relative}/${name}`);
        }
    };
    for (const path of (await gitChangedFiles(cwd)).sort())
        updatePath(resolve(cwd, path), path);
    return digest.digest('hex');
}
export async function gitCandidate(cwd, message, tag) {
    if (tag) {
        try {
            return { sha: (await run(cwd, ['rev-parse', '--verify', `refs/tags/${tag}`])).trim(), files: [] };
        }
        catch { }
    }
    const files = await gitChangedFiles(cwd);
    let sha;
    if (files.length > 0) {
        await run(cwd, ['add', '--', ...files]);
        await run(cwd, ['commit', '-m', message]);
        sha = await gitHead(cwd);
    }
    else {
        const subject = (await run(cwd, ['log', '-1', '--pretty=%s'])).trim();
        if (subject !== message)
            throw new Error('cannot reconcile candidate: clean HEAD does not belong to this run');
        sha = await gitHead(cwd);
    }
    if (tag)
        await run(cwd, ['tag', tag, sha]);
    return { sha, files };
}
export async function prepareGitCandidate(cwd, message) {
    const files = await gitChangedFiles(cwd);
    if (files.length === 0)
        throw new Error('cannot prepare a candidate with no Git changes');
    await run(cwd, ['add', '--', ...files]);
    const tree = (await run(cwd, ['write-tree'])).trim();
    const parent = await gitHead(cwd);
    const sha = (await run(cwd, ['commit-tree', tree, '-p', parent, '-m', message])).trim();
    return { sha, tree, parent, files, fingerprint: await gitCandidateFingerprint(cwd) };
}
export async function promotePreparedCandidate(cwd, candidate, tag) {
    try {
        const existing = (await run(cwd, ['rev-parse', '--verify', `refs/tags/${tag}`])).trim();
        if (existing !== candidate.sha)
            throw new Error('candidate tag does not match the reviewed snapshot');
        return existing;
    }
    catch (error) {
        if (error instanceof Error && error.message === 'candidate tag does not match the reviewed snapshot')
            throw error;
    }
    if (await gitHead(cwd) !== candidate.parent)
        throw new Error('candidate parent changed after review');
    if ((await run(cwd, ['write-tree'])).trim() !== candidate.tree)
        throw new Error('staged candidate changed after review');
    await run(cwd, ['update-ref', 'HEAD', candidate.sha, candidate.parent]);
    await run(cwd, ['tag', tag, candidate.sha]);
    return candidate.sha;
}
export async function gitRollback(cwd, sha) {
    if ((await gitChangedFiles(cwd)).length > 0)
        throw new Error('cannot rollback a dirty Git workspace');
    await run(cwd, ['switch', '--detach', sha]);
}
export function archiveReports(cwd, reportFiles) {
    const reports = [];
    for (const file of reportFiles) {
        const path = resolve(cwd, file);
        if (!existsSync(path))
            continue;
        reports.push({ path: file, content: readFileSync(path, 'utf8') });
        rmSync(path, { force: true });
    }
    return reports;
}
export async function rejectCandidate(cwd, baselineSha, allowedFiles, reportFiles) {
    const changed = await gitChangedFiles(cwd);
    const known = new Set([...allowedFiles, ...reportFiles]);
    const unexpected = changed.filter(file => !known.has(file));
    if (unexpected.length > 0)
        throw new Error(`refusing reject cleanup with out-of-scope changes: ${unexpected.join(', ')}`);
    const restoredFiles = [];
    for (const file of changed.filter(path => allowedFiles.includes(path))) {
        try {
            await run(cwd, ['ls-files', '--error-unmatch', '--', file]);
            await run(cwd, ['restore', '--source', baselineSha, '--staged', '--worktree', '--', file]);
        }
        catch {
            rmSync(resolve(cwd, file), { recursive: true, force: true });
        }
        restoredFiles.push(file);
    }
    const removedReports = archiveReports(cwd, reportFiles);
    return { restoredFiles, removedReports };
}
function lockPath(cwd, commonDir) {
    const directory = isAbsolute(commonDir) ? commonDir : resolve(cwd, commonDir);
    return resolve(directory, 'loopgraph-supervisor.lock');
}
export async function acquireWorkspaceLock(cwd, workflowId) {
    const path = lockPath(cwd, (await run(cwd, ['rev-parse', '--git-common-dir'])).trim());
    try {
        const descriptor = openSync(path, 'wx', 0o600);
        try {
            writeFileSync(descriptor, JSON.stringify({ workflowId, createdAt: new Date().toISOString() }));
        }
        finally {
            closeSync(descriptor);
        }
    }
    catch (error) {
        if (error.code !== 'EEXIST')
            throw error;
        const owner = JSON.parse(readFileSync(path, 'utf8'));
        if (owner.workflowId !== workflowId)
            throw new Error(`workspace is locked by workflow ${owner.workflowId ?? 'unknown'}`);
    }
}
export async function releaseWorkspaceLock(cwd, workflowId) {
    const path = lockPath(cwd, (await run(cwd, ['rev-parse', '--git-common-dir'])).trim());
    try {
        const owner = JSON.parse(readFileSync(path, 'utf8'));
        if (owner.workflowId !== workflowId)
            throw new Error('refusing to release another workflow lock');
        unlinkSync(path);
    }
    catch (error) {
        if (error.code !== 'ENOENT')
            throw error;
    }
}
