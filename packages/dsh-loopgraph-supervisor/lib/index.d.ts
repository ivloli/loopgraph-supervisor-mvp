import type { Context } from '@deepseek-ai/cordis';
import '@deepseek-ai/dsh-agent';
import '@deepseek-ai/dsh-commands';
import '@deepseek-ai/dsh-user-questions';
import Schema from '@deepseek-ai/schemastery';
import type { AcceptanceContract } from './model.js';
export declare const name = "dsh-loopgraph-supervisor";
export declare const inject: string[];
export interface Config {
    maxAttempts: number;
    requirePromotionApproval: boolean;
    workflowName?: string;
}
export declare const Config: Schema<Config>;
export declare function specRevision(goal: string, maxAttempts: number, acceptance: AcceptanceContract): string;
export declare function apply(ctx: Context, config: Config): void;
