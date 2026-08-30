import json
import sys

import pytest

from loopgraph_supervisor.arena import FilesystemArena
from loopgraph_supervisor.filesystem_evaluation import evaluate_filesystem_candidate
from loopgraph_supervisor.holdout import FilesystemHoldoutRepository, hash_tree


def write_task(root, split="canary", task_id="hidden-1", exit_code=0):
    directory = root / "coding-supervisor" / split / task_id
    fixture = directory / "fixture"
    fixture.mkdir(parents=True)
    (fixture / "input.txt").write_text("private fixture\n")
    (directory / "task.json").write_text(json.dumps({"schema_version": 1, "id": task_id, "prompt": "host-only prompt", "verifier": [sys.executable, "-c", f"raise SystemExit({exit_code})"], "timeout_seconds": 2, "forbidden_paths": ["golden-answer.txt"]}))
    return directory


def test_filesystem_holdout_hashes_task_and_fixture(tmp_path):
    write_task(tmp_path)
    task = FilesystemHoldoutRepository(tmp_path).tasks("coding-supervisor", "canary")[0]

    assert len(task.task_hash) == 64
    assert len(task.fixture_hash) == 64
    assert str(tmp_path) not in task.task_hash


def test_arena_uses_sibling_copy_and_detects_forbidden_artifact(tmp_path):
    write_task(tmp_path)
    task = FilesystemHoldoutRepository(tmp_path).tasks("coding-supervisor", "canary")[0]
    arena = FilesystemArena(tmp_path / "arena")

    clean = arena.run(task, "baseline", "a" * 64)
    contaminated_fixture = tmp_path / "contaminated"
    contaminated_fixture.mkdir()
    (contaminated_fixture / "input.txt").write_text("private fixture\n")
    (contaminated_fixture / "golden-answer.txt").write_text("leak")
    contaminated_task = type(task)(**{**task.__dict__, "fixture_root": contaminated_fixture, "fixture_hash": hash_tree(contaminated_fixture)})
    contaminated = arena.run(contaminated_task, "candidate", "b" * 64)

    assert clean.passed is True
    assert contaminated.passed is False
    assert contaminated.contamination == ("golden-answer.txt",)
    assert clean.receipt_hash() != contaminated.receipt_hash()


def test_holdout_descriptor_and_fixture_tampering_changes_hash(tmp_path):
    directory = write_task(tmp_path)
    repository = FilesystemHoldoutRepository(tmp_path)
    before = repository.tasks("coding-supervisor", "canary")[0]
    (directory / "fixture" / "input.txt").write_text("tampered\n")
    after = repository.tasks("coding-supervisor", "canary")[0]

    assert before.fixture_hash != after.fixture_hash
    assert before.task_hash != after.task_hash


def test_holdout_rejects_shell_string_verifier(tmp_path):
    directory = write_task(tmp_path)
    document = json.loads((directory / "task.json").read_text())
    document["verifier"] = "python -c pass"
    (directory / "task.json").write_text(json.dumps(document))

    with pytest.raises(ValueError, match="argv"):
        FilesystemHoldoutRepository(tmp_path).tasks("coding-supervisor", "canary")


def test_filesystem_evaluation_binds_arm_receipts_into_candidate_proof(tmp_path):
    write_task(tmp_path, split="validation", task_id="visible")
    write_task(tmp_path, split="canary", task_id="hidden")
    proof = evaluate_filesystem_candidate(FilesystemHoldoutRepository(tmp_path), FilesystemArena(tmp_path / "arena"), "coding-supervisor", "a" * 64, "b" * 64)

    assert proof.validation_pass_rate == 1
    assert proof.canary_pass_rate == 1
    assert proof.regression_count == 0
    assert len(proof.evaluation_evidence_hashes) == 4
    assert all(len(receipt) == 64 for receipt in proof.evaluation_evidence_hashes)


def test_arena_refuses_fixture_changed_after_task_freeze(tmp_path):
    directory = write_task(tmp_path)
    task = FilesystemHoldoutRepository(tmp_path).tasks("coding-supervisor", "canary")[0]
    (directory / "fixture" / "input.txt").write_text("changed after freeze\n")

    with pytest.raises(ValueError, match="changed after task freeze"):
        FilesystemArena(tmp_path / "arena").run(task, "candidate", "b" * 64)


def test_holdout_fixture_rejects_symlink_escape(tmp_path):
    directory = write_task(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("outside")
    (directory / "fixture" / "escape").symlink_to(secret)

    with pytest.raises(ValueError, match="cannot contain symlinks"):
        FilesystemHoldoutRepository(tmp_path).tasks("coding-supervisor", "canary")
