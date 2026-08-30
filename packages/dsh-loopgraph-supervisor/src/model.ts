export type LoopStatus = 'IDLE' | 'RUNNING' | 'UNCERTAIN' | 'PAUSED' | 'WAITING_HITL' | 'COMPLETED' | 'FAILED'
export type LoopNode = 'EXECUTE' | 'VERIFY' | 'PROMOTE' | 'HITL' | 'COMPLETED' | 'FAILED'

export interface AcceptanceContract {
  readonly commands?: readonly string[]
  readonly allowedFiles?: readonly string[]
}

export interface DecisionRecord {
  readonly id: string
  readonly type: string
  readonly question: string
  readonly decision: string
  readonly rationale: readonly string[]
  readonly evidence: readonly Record<string, unknown>[]
  readonly risk: string
  readonly expectedEffect: string
  readonly at: number
}

export interface LoopState {
  readonly workflowId: string
  readonly status: LoopStatus
  readonly node: LoopNode
  readonly attempt: number
  readonly maxAttempts: number
  readonly goal: string
  readonly acceptance: AcceptanceContract
  readonly decisions: readonly DecisionRecord[]
  readonly baselineCommit?: string
  readonly candidateCommit?: string
  readonly specRevision?: string
  readonly candidateFingerprint?: string | null
  readonly preparedCandidate?: { readonly sha: string; readonly tree: string; readonly parent: string; readonly files: readonly string[] } | null
  readonly hitlReason?: 'PROMOTION_REVIEW' | 'QUALITY_REVIEW' | 'SCOPE_REVIEW' | 'FAILURE_REVIEW' | 'UNCERTAIN_RECOVERY'
  readonly versions?: readonly { readonly id: string; readonly sha: string; readonly parentId?: string; readonly status: 'BASELINE' | 'PROMOTED' | 'ROLLED_BACK' }[]
  readonly activeVersion?: string
}

export interface LoopGraphEvent {
  readonly workflowId: string
  readonly kind: 'state' | 'decision' | 'evidence'
  readonly state?: Partial<LoopState>
  readonly decision?: DecisionRecord
  readonly evidence?: Record<string, unknown>
}
