from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import quote

if TYPE_CHECKING:
    from .pr_promotion import PreparedPullRequest


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str
    base_ref: str
    head_ref: str
    head_sha: str
    state: str


@dataclass(frozen=True)
class ReviewStatus:
    number: int
    head_sha: str
    approved: bool
    approving_reviewers: tuple[str, ...]


class GitHubAdapter:
    """Small fail-closed adapter around the reviewed GitHub CLI."""

    def __init__(self, repository: str, gh: str = "gh", authorized_reviewers: frozenset[str] = frozenset()):
        self.repository = repository
        self.gh = gh
        if not authorized_reviewers:
            raise ValueError("GitHub adapter requires an explicit authorized reviewer allowlist")
        self.authorized_reviewers = authorized_reviewers

    def create_pr(self, prepared: PreparedPullRequest, branch: str, title: str, body: str) -> PullRequest:
        if not branch or branch in {"main", "master"}:
            raise ValueError("PR head must be a candidate branch")
        remote_head = self._run("api", f"repos/{self.repository}/git/ref/heads/{quote(branch, safe='')}", "--jq", ".object.sha")
        if remote_head != prepared.candidate_commit:
            raise ValueError("remote candidate branch head does not match the prepared candidate")
        url = self._run("pr", "create", "--repo", self.repository, "--base", "main", "--head", branch, "--title", title, "--body", body)
        document = json.loads(self._run("pr", "view", url.splitlines()[-1], "--repo", self.repository, "--json", "number,url,baseRefName,headRefName,headRefOid,state"))
        pr = PullRequest(int(document["number"]), str(document["url"]), str(document["baseRefName"]), str(document["headRefName"]), str(document["headRefOid"]), str(document["state"]))
        if pr.head_sha != prepared.candidate_commit or pr.base_ref != "main":
            raise ValueError("created PR does not bind the prepared candidate")
        return pr

    def review_status(self, number: int, expected_head_sha: str) -> ReviewStatus:
        document = json.loads(self._run("pr", "view", str(number), "--repo", self.repository, "--json", "headRefOid,reviews"))
        head_sha = str(document["headRefOid"])
        reviews = document.get("reviews", [])
        approved = tuple(str(item.get("author", {}).get("login", "")) for item in reviews if item.get("state") == "APPROVED" and str(item.get("author", {}).get("login", "")) in self.authorized_reviewers and str(item.get("commitId", item.get("commit", ""))) == expected_head_sha)
        return ReviewStatus(number, head_sha, bool(approved) and head_sha == expected_head_sha, tuple(sorted(set(approved))))

    def close_pr(self, number: int) -> None:
        self._run("pr", "close", str(number), "--repo", self.repository, "--delete-branch=false")

    def _run(self, *args: str) -> str:
        result = subprocess.run([self.gh, *args], capture_output=True, text=True, timeout=120, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "gh command failed")
        return result.stdout.strip()
