import json

from loopgraph_supervisor.github_adapter import GitHubAdapter
from loopgraph_supervisor.pr_promotion import PreparedPullRequest


class FakeGitHub(GitHubAdapter):
    def __init__(self, outputs):
        super().__init__("ivloli/demo", authorized_reviewers=frozenset({"human"}))
        self.outputs = iter(outputs)

    def _run(self, *args):
        return next(self.outputs)


def test_create_pr_binds_candidate_head_and_base():
    adapter = FakeGitHub(["head", "https://github.com/ivloli/demo/pull/7", json.dumps({"number": 7, "url": "https://github.com/ivloli/demo/pull/7", "baseRefName": "main", "headRefName": "rsi/v2", "headRefOid": "head", "state": "OPEN"})])
    prepared = PreparedPullRequest("candidate", "parent", "head", ("policy.py",), "human")

    pr = adapter.create_pr(prepared, "rsi/v2", "RSI v2", "proof")

    assert pr.number == 7
    assert pr.head_sha == "head"


def test_review_head_change_is_not_approved():
    adapter = FakeGitHub([json.dumps({"headRefOid": "new-head", "reviews": [{"state": "APPROVED", "commitId": "old-head", "author": {"login": "human"}}]})])

    status = adapter.review_status(7, "old-head")

    assert status.approved is False
