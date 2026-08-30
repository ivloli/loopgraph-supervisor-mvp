import subprocess

import pytest

from loopgraph_supervisor.coordination import CandidateApproval, CandidateClaim, CoordinatedCandidate, CoordinationStore
from loopgraph_supervisor.git_workspace import GitWorkspace
from loopgraph_supervisor.pr_promotion import PRPromotionService
from loopgraph_supervisor.store import SQLiteStore


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def repository(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.email", "test@example.com")
    git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "policy.py").write_text("FALLBACK = True\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-m", "baseline")
    parent = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "policy.py").write_text("FALLBACK = False\n")
    git(tmp_path, "add", "policy.py")
    git(tmp_path, "commit", "-m", "candidate")
    return parent, git(tmp_path, "rev-parse", "HEAD")


def test_pr_preparation_requires_current_coordination_and_exact_git_diff(tmp_path):
    parent, head = repository(tmp_path)
    store = CoordinationStore(SQLiteStore(":memory:"))
    proposal = CoordinatedCandidate("candidate", parent, head, ("policy.py",), (CandidateClaim("synthetic_fallback", "remove"),))
    store.register(proposal)
    store.select(parent, "candidate")
    approval = CandidateApproval("candidate", parent, head, "human")

    prepared = PRPromotionService(store).prepare(GitWorkspace(str(tmp_path)), "candidate", approval)

    assert prepared.candidate_commit == head
    assert prepared.changed_paths == ("policy.py",)


def test_new_conflicting_candidate_invalidates_pr_preparation(tmp_path):
    parent, head = repository(tmp_path)
    store = CoordinationStore(SQLiteStore(":memory:"))
    store.register(CoordinatedCandidate("remove", parent, head, ("policy.py",), (CandidateClaim("synthetic_fallback", "remove"),)))
    store.select(parent, "remove")
    store.register(CoordinatedCandidate("retain", parent, "other-head", ("policy.py",), (CandidateClaim("synthetic_fallback", "retain"),)))

    with pytest.raises(ValueError, match="selected as the coordinated PR"):
        PRPromotionService(store).prepare(GitWorkspace(str(tmp_path)), "remove", CandidateApproval("remove", parent, head, "human"))
