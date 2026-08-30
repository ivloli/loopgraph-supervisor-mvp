export declare function gitHead(cwd: string): Promise<string>;
export declare function gitChangedFiles(cwd: string): Promise<string[]>;
export declare function parsePorcelainPaths(status: string): string[];
export declare function gitCandidateFingerprint(cwd: string): Promise<string>;
export declare function gitCandidate(cwd: string, message: string, tag?: string): Promise<{
    sha: string;
    files: string[];
}>;
export interface PreparedGitCandidate {
    readonly sha: string;
    readonly tree: string;
    readonly parent: string;
    readonly files: readonly string[];
    readonly fingerprint: string;
}
export declare function prepareGitCandidate(cwd: string, message: string): Promise<PreparedGitCandidate>;
export declare function promotePreparedCandidate(cwd: string, candidate: PreparedGitCandidate, tag: string): Promise<string>;
export declare function gitRollback(cwd: string, sha: string): Promise<void>;
export interface RejectedCandidateCleanup {
    readonly restoredFiles: string[];
    readonly removedReports: {
        readonly path: string;
        readonly content: string;
    }[];
}
export declare function archiveReports(cwd: string, reportFiles: readonly string[]): {
    readonly path: string;
    readonly content: string;
}[];
export declare function rejectCandidate(cwd: string, baselineSha: string, allowedFiles: readonly string[], reportFiles: readonly string[]): Promise<RejectedCandidateCleanup>;
export declare function acquireWorkspaceLock(cwd: string, workflowId: string): Promise<void>;
export declare function releaseWorkspaceLock(cwd: string, workflowId: string): Promise<void>;
