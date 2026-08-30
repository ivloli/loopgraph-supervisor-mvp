export interface CommandEvidence {
    readonly command: string;
    readonly exitCode: number;
    readonly passed: boolean;
    readonly stdout: string;
    readonly stderr: string;
}
export declare function verifyCommands(cwd: string, commands: readonly string[]): Promise<{
    passed: boolean;
    evidence: CommandEvidence[];
}>;
