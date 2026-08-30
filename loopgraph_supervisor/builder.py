from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class CandidateBuildRequest:
    candidate_id: str
    active_spec: dict[str, object]
    validation_context: tuple[dict[str, object], ...]
    allowed_kind: str = "loopspec"
    improvement_request: str = ""


@dataclass(frozen=True)
class CandidateBuildResult:
    candidate_id: str
    kind: str
    rationale: str
    document: dict[str, object]
    session_id: str

    def evidence(self) -> dict[str, str | int]:
        canonical = json.dumps(self.document, sort_keys=True, separators=(",", ":"))
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "session_id": self.session_id,
            "document_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "document_key_count": len(self.document),
        }


class CandidateBuilder(Protocol):
    def build(self, request: CandidateBuildRequest) -> CandidateBuildResult: ...


def parse_builder_response(response: str, expected_candidate_id: str, allowed_kind: str) -> CandidateBuildResult:
    if len(response.encode()) > 64 * 1024:
        raise ValueError("Builder response exceeds the 64 KiB candidate limit")
    try:
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError(f"duplicate JSON key: {key}")
                result[key] = value
            return result

        document: Any = json.loads(response, object_pairs_hook=unique_object, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"non-finite JSON value: {value}")))
    except json.JSONDecodeError as error:
        raise ValueError("Builder response must be one JSON object with no prose or Markdown") from error
    if not isinstance(document, dict) or set(document) != {"candidate_id", "kind", "rationale", "document"}:
        raise ValueError("Builder response has an unexpected shape")
    if document["candidate_id"] != expected_candidate_id or document["kind"] != allowed_kind:
        raise ValueError("Builder response does not match the requested candidate identity and kind")
    if not isinstance(document["rationale"], str) or not document["rationale"].strip() or not isinstance(document["document"], dict):
        raise ValueError("Builder response requires rationale and candidate document")
    return CandidateBuildResult(expected_candidate_id, allowed_kind, document["rationale"], document["document"], "")


def builder_prompt(request: CandidateBuildRequest) -> str:
    if any("canary" in json.dumps(item).lower() or "holdout" in json.dumps(item).lower() for item in request.validation_context):
        raise ValueError("Builder validation context contains forbidden holdout metadata")
    return json.dumps(
        {
            "instruction": "Propose one bounded LoopSpec revision. Return exactly one JSON object and no prose.",
            "output_schema": {"candidate_id": request.candidate_id, "kind": request.allowed_kind, "rationale": "non-empty string", "document": "LoopSpec v1 object"},
            "active_spec": request.active_spec,
            "binding_rules": {
                "spec_id": "copy active_spec.spec_id exactly",
                "revision": "active_spec.revision + 1 exactly",
                "predecessor_hash": "copy the active predecessor content hash exactly; do not compute or invent a new hash",
            },
            "improvement_request": request.improvement_request,
            "validation_context": list(request.validation_context),
            "forbidden": ["holdout", "canary", "promotion authority", "evaluator changes"],
        },
        ensure_ascii=False,
    )


class DockerDeepSeekCandidateBuilder:
    """Runs the official SDK against a receipt-gated Docker runtime launcher."""

    def __init__(self, runtime, model: str = "deepseek-v4-flash"):
        from .docker_runtime import DockerSdkRuntime

        if type(runtime) is not DockerSdkRuntime or runtime.relay is None or not runtime.relay.active:
            raise ValueError("Docker DSH Builder requires a live receipt-gated DockerSdkRuntime with controlled egress")
        self.runtime = runtime
        self.model = model

    def build(self, request: CandidateBuildRequest) -> CandidateBuildResult:
        from deepseek_harness import DeepSeekHarness  # type: ignore[import-untyped]

        session_id = f"evolution-builder-{request.candidate_id}"
        workspace = self.runtime.root / "workspace"
        config = {
            "provider": "deepseek-official",
            "model": self.model,
            "max_tokens": 8192,
            "cwd": "/workspace",
            "runtime_cwd": str(workspace),
            "runtime_bin": str(self.runtime.wrapper()),
            "api_key": "builder-relay-placeholder",
            "base_url": "http://egress:8080",
            "request_timeout_seconds": 300,
        }
        with DeepSeekHarness(**config) as harness:
            run = harness.run(builder_prompt(request), session_id=session_id)
        parsed = parse_builder_response(run.final_response or "", request.candidate_id, request.allowed_kind)
        return CandidateBuildResult(parsed.candidate_id, parsed.kind, parsed.rationale, parsed.document, session_id)


class DeepSeekCandidateBuilder:
    """Removed unsafe host-process Builder path; use DockerDeepSeekCandidateBuilder."""

    def __init__(self, *args, **kwargs):
        raise ValueError("host-process DSH Builder is permanently disabled; use the receipt-gated Docker Builder")
