from __future__ import annotations

from dataclasses import dataclass

from .coordination import CandidateApproval, CoordinationStore
from .evolution_run import EvolutionRunStore
from .git_workspace import GitWorkspace
from .github_adapter import GitHubAdapter, PullRequest, ReviewStatus


@dataclass(frozen=True)
class PreparedPullRequest:
    candidate_id: str
    parent_commit: str
    candidate_commit: str
    changed_paths: tuple[str, ...]
    reviewer: str


class PRPromotionService:
    """Fail-closed gate between coordinated candidates and a Git hosting adapter."""

    def __init__(self, coordination: CoordinationStore):
        self.coordination = coordination

    def prepare(self, workspace: GitWorkspace, candidate_id: str, approval: CandidateApproval) -> PreparedPullRequest:
        self.coordination.authorize_pr(candidate_id, approval)
        candidate = self.coordination.candidate(approval.parent_commit, candidate_id)
        if workspace.commit_parent(candidate.candidate_commit) != candidate.parent_commit:
            raise ValueError("candidate Git parent does not match the coordinated baseline")
        changed_paths = workspace.diff_files(candidate.parent_commit, candidate.candidate_commit)
        if changed_paths != tuple(sorted(candidate.changed_paths)):
            raise ValueError("candidate Git diff does not match coordinated changed paths")
        return PreparedPullRequest(candidate.candidate_id, candidate.parent_commit, candidate.candidate_commit, changed_paths, approval.reviewer)


class GitHubPromotionService:
    def __init__(self, preparation: PRPromotionService, github: GitHubAdapter, runs: EvolutionRunStore):
        self.preparation = preparation
        self.github = github
        self.runs = runs

    def open(self, workspace: GitWorkspace, run_id: str, candidate_id: str, approval: CandidateApproval, branch: str, title: str, body: str) -> PullRequest:
        run = self.runs.get(run_id)
        if run is None or run.status != "PROMOTION_REVIEW" or run.candidate_id != candidate_id or run.proof_hash is None:
            raise ValueError("evolution run does not bind the selected candidate and baseline")
        prepared = self.preparation.prepare(workspace, candidate_id, approval)
        pull_request = self.github.create_pr(prepared, branch, title, body)
        try:
            self.runs.attach_pr(run_id, pull_request.number, pull_request.head_sha)
        except Exception:
            self.github.close_pr(pull_request.number)
            raise
        return pull_request

    def review(self, run_id: str, number: int) -> ReviewStatus:
        run = self.runs.get(run_id)
        if run is None or run.pr_number != number or run.pr_head is None:
            raise ValueError("evolution run has no matching PR head")
        return self.github.review_status(number, run.pr_head)
