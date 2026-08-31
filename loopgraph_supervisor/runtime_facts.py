from __future__ import annotations

import hashlib
import importlib.metadata
import json
from collections import Counter
from pathlib import Path
from typing import Any

from .git_workspace import GitWorkspace


def summarize_dsh_run(result: Any, *, model: str, workspace: str, expected_session_id: str) -> dict[str, object]:
    """Project DSH Agent-runtime activity into bounded, non-payload evidence."""
    session_id = str(result.session_id)
    if session_id != expected_session_id:
        raise RuntimeError("DeepSeek Harness returned an unexpected session identity")
    events = result.events
    notifications = result.notifications
    if not isinstance(events, list) or not isinstance(notifications, list):
        raise RuntimeError("DeepSeek Harness runtime did not expose event evidence")

    event_types = Counter(str(event.get("type", "unknown")) for event in events if isinstance(event, dict))
    notification_methods = Counter(str(item.method) for item in notifications)
    tool_event_types = sorted(name for name in event_types if "tool" in name.casefold())
    canonical_events = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode()
    root = Path(workspace).resolve()
    git = GitWorkspace(str(root))
    changed_files = git.changed_files() if git.available else []
    return {
        "type": "dsh_runtime",
        "adapter": "deepseek-harness-sdk",
        "sdk_version": importlib.metadata.version("deepseek-harness-sdk"),
        "model": model,
        "session_id": session_id,
        "finish_reason": result.finish_reason,
        "event_count": len(events),
        "event_types": dict(sorted(event_types.items())),
        "notification_count": len(notifications),
        "notification_methods": dict(sorted(notification_methods.items())),
        "tool_event_types": tool_event_types,
        "event_stream_hash": hashlib.sha256(canonical_events).hexdigest(),
        "workspace_hash": hashlib.sha256(str(root).encode()).hexdigest(),
        "changed_files": changed_files,
        "payloads_persisted": False,
    }
