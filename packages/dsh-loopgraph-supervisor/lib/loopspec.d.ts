export type NodeKind = 'dsh_execute' | 'verifier' | 'human_gate' | 'promotion' | 'terminal';
export type Outcome = 'pass' | 'fail' | 'retry' | 'approve' | 'auto_promote' | 'reject' | 'exhausted';
export interface LoopSpec {
    readonly schema_version: 1;
    readonly spec_id: string;
    readonly revision: number;
    readonly predecessor_hash: string | null;
    readonly entrypoint: string;
    readonly max_iterations: number;
    readonly nodes: readonly {
        readonly id: string;
        readonly kind: NodeKind;
        readonly role?: string;
    }[];
    readonly edges: readonly {
        readonly source: string;
        readonly target: string;
        readonly outcomes: readonly Outcome[];
    }[];
}
export declare function loopSpecHash(spec: LoopSpec): string;
export declare function loadLoopSpec(path?: string): LoopSpec;
export declare function loadWorkspaceLoopSpec(workspace: string, fallback: LoopSpec): LoopSpec;
export declare function saveWorkspaceLoopSpec(workspace: string, spec: LoopSpec): void;
export declare function nodeForRole(spec: LoopSpec, role: string): string;
export declare function nextNode(spec: LoopSpec, source: string, outcome: Outcome, iteration: number): string;
export declare function validateLoopSpec(spec: LoopSpec): void;
