import type { Agent } from '@deepseek-ai/dsh-agent';
import type { DecisionRecord, LoopGraphEvent, LoopState } from './model.js';
export declare function appendLoopEvent(agent: Agent, event: LoopGraphEvent): void;
export declare function withLoopOperationLock<T>(agent: Agent, operation: () => Promise<T>): Promise<T>;
export declare function loadLoopEvents(agent: Agent): LoopGraphEvent[];
export declare function initialState(workflowId: string, goal: string, maxAttempts: number, acceptance: LoopState['acceptance']): LoopState;
export declare function foldState(agent: Agent, fallback?: LoopState): LoopState | undefined;
export declare function decision(type: string, question: string, choice: string, rationale: string[], evidence: Record<string, unknown>[], risk: string, expectedEffect: string): DecisionRecord;
