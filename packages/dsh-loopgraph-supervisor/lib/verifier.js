import { execFile } from 'node:child_process';
function runCommand(cwd, command) {
    return new Promise((resolve) => {
        execFile('/bin/sh', ['-lc', command], { cwd, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, timeout: 300_000 }, (error, stdout, stderr) => {
            const exitCode = typeof error?.code === 'number' ? error.code : error ? 1 : 0;
            resolve({ command, exitCode, passed: exitCode === 0, stdout: stdout.slice(-4000), stderr: stderr.slice(-4000) });
        });
    });
}
export async function verifyCommands(cwd, commands) {
    if (commands.length === 0)
        return { passed: false, evidence: [] };
    const evidence = [];
    for (const command of commands)
        evidence.push(await runCommand(cwd, command));
    return { passed: evidence.every(item => item.passed), evidence };
}
