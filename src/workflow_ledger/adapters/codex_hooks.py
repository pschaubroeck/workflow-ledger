"""Fail-open Codex lifecycle hook adapter.

Consumes only documented lifecycle metadata; hook payload content and native
identifiers are never written to the store or emitted as diagnostics.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

from ..model import Association, AssetIdentity, Capabilities, stable_id
from ..policy import CollectionPolicy

MAX_PAYLOAD_BYTES = 1024 * 1024
_EVENTS = {"SessionStart", "SessionEnd", "PreToolUse", "PostToolUse", "SubagentStart", "SubagentStop"}
CAPABILITIES = Capabilities(
    "codex-hooks", "0.3.0", "direct", "direct", "unavailable", "unavailable",
    "direct", "inferred", "unavailable", (
        "Only documented session, tool, and subagent lifecycle boundaries are recorded.",
        "Codex hooks do not expose skill lifecycle, model-call, or token-usage events.",
        "Tool and subagent outcomes remain unknown because the portable event contract does not define a reliable result flag.",
        "Hook fixture coverage does not prove live host delivery or installation.",
    ),
)
_DIAGNOSTIC_KEYS = (
    "observed", "filtered_records", "missing_starts", "storage_errors", "lock_timeouts",
    "permission_denied", "storage_full", "storage_unavailable",
    "rejected_events", "missing_ids", "unsupported_events",
)


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 4096 and not any(ord(c) < 32 for c in value)


def _identity(kind: str, native_name: str) -> AssetIdentity:
    # Hash the native asset name too: it may contain an operator-defined profile name.
    return AssetIdentity(f"codex-{kind}", stable_id(kind, native_name), provenance="native")


def _workflow_identity() -> AssetIdentity:
    return AssetIdentity("codex-workflow", "session", provenance="native")


def process_event(payload: dict[str, Any], store: Any) -> dict[str, int]:
    """Map one official-shaped hook payload onto shared invocation markers."""
    counts = {key: 0 for key in _DIAGNOSTIC_KEYS}
    if not isinstance(payload, dict):
        counts["rejected_events"] += 1
        return counts
    event = payload.get("hook_event_name")
    if event not in _EVENTS:
        counts["unsupported_events"] += 1
        return counts
    session_id = payload.get("session_id")
    if not _valid_id(session_id):
        counts["rejected_events"] += 1
        counts["missing_ids"] += 1
        return counts
    result = None
    if event == "SessionStart":
        start_workflow = getattr(store, "start_workflow", None)
        if callable(start_workflow):
            result = start_workflow(session_id, _workflow_identity())
        else:
            result = store.start("workflow", _workflow_identity(),
                                 session_id=session_id, invocation_id="session", evidence="native")
    elif event == "SessionEnd":
        finish_workflow = getattr(store, "finish_workflow", None)
        if callable(finish_workflow):
            result = finish_workflow(session_id, _workflow_identity(), outcome="unknown")
        else:
            workflow = _workflow_handle(store, session_id)
            result = store.finish(workflow["trace_id"], workflow["span_id"], outcome="unknown")
    elif event in {"PreToolUse", "PostToolUse"}:
        tool_id = payload.get("tool_use_id")
        tool_name = payload.get("tool_name")
        if not _valid_id(tool_id) or not _valid_id(tool_name):
            counts["rejected_events"] += 1
            counts["missing_ids"] += 1
            return counts
        invocation_id = "tool:" + tool_id
        handle = store.handle(session_id, invocation_id)
        if event == "PreToolUse":
            workflow = _workflow_handle(store, session_id)
            result = store.start("tool", _identity("tool", tool_name), session_id=session_id,
                                 invocation_id=invocation_id, parent=workflow,
                                 associations=(_workflow_association(workflow),), evidence="native")
        else:
            # The documented PostToolUse envelope does not define a portable error flag.
            result = store.finish(handle["trace_id"], handle["span_id"], outcome="unknown")
    else:
        agent_id = payload.get("agent_id")
        agent_type = payload.get("agent_type")
        if not _valid_id(agent_id) or not _valid_id(agent_type):
            counts["rejected_events"] += 1
            counts["missing_ids"] += 1
            return counts
        turn_id = payload.get("turn_id")
        if turn_id is not None and not _valid_id(turn_id):
            counts["rejected_events"] += 1
            counts["missing_ids"] += 1
            return counts
        invocation_id = "agent:" + agent_id
        if turn_id is not None:
            invocation_id += ":turn:" + turn_id
        handle = store.handle(session_id, invocation_id)
        if event == "SubagentStart":
            workflow = _workflow_handle(store, session_id)
            result = store.start("agent", _identity("agent", agent_type), session_id=session_id,
                                 invocation_id=invocation_id, parent=workflow,
                                 associations=(_workflow_association(workflow),), evidence="native")
        else:
            # A stop event does not establish whether the subagent succeeded.
            result = store.finish(handle["trace_id"], handle["span_id"], outcome="unknown")
    if result.get("filtered"):
        counts["filtered_records"] += 1
    elif result.get("missing_start"):
        counts["missing_starts"] += 1
    elif result.get("recorded"):
        counts["observed"] += 1
    else:
        counts["storage_errors"] += 1
    store_errors = getattr(store, "diagnostics", {})
    counts["storage_errors"] = max(counts["storage_errors"], int(store_errors.get("storage_errors", 0)))
    counts["lock_timeouts"] = int(store_errors.get("lock_timeouts", 0))
    storage_error = getattr(store, "last_storage_error", None)
    if storage_error in {"permission_denied", "storage_full", "storage_unavailable"}:
        counts[storage_error] += 1
    return counts


def _workflow_association(handle: dict) -> Association:
    return Association(kind="workflow", identity=_workflow_identity(), state="invoked", evidence="native",
                       invocation_span_id=handle["span_id"], invocation_trace_id=handle["trace_id"])


def _workflow_handle(store: Any, session_id: str) -> dict:
    resolve = getattr(store, "workflow_handle", None)
    if callable(resolve):
        return resolve(session_id, _workflow_identity())
    return store.handle(session_id, "session")


def _fail_open_error(code: str) -> None:
    sys.stderr.write(json.dumps({"error": code}, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    # This guard deliberately precedes environment inspection, policy reads, stdin, and imports.
    if args and args[0] == "--disabled":
        return 0
    try:
        if os.environ.get("WORKFLOW_LEDGER_ENABLED") != "1":
            return 0
        store_value = os.environ.get("WORKFLOW_LEDGER_STORE")
        if not store_value or not Path(store_value).is_absolute():
            _fail_open_error("invalid_configuration")
            return 0
        policy = None
        policy_path = os.environ.get("WORKFLOW_LEDGER_POLICY")
        if policy_path:
            try:
                policy = CollectionPolicy.load(policy_path)
            except Exception:
                _fail_open_error("policy_error")
                return 0
            if not policy.may_observe_runtime("codex"):
                return 0
        try:
            raw = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
        except Exception:
            _fail_open_error("invalid_payload")
            return 0
        if len(raw) > MAX_PAYLOAD_BYTES:
            _fail_open_error("invalid_payload")
            return 0
        try:
            payload = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError):
            _fail_open_error("invalid_payload")
            return 0
        if not isinstance(payload, dict):
            _fail_open_error("invalid_payload")
            return 0
        from ..retention import resolve_retention_policy
        from ..sessions import SessionStore

        try:
            retention = resolve_retention_policy()
        except (OSError, UnicodeError, ValueError, TypeError):
            _fail_open_error("retention_policy_error")
            return 0
        store = SessionStore(store_value, policy=policy, harness="codex", retention=retention)
        diagnostics = process_event(payload, store)
        if any(diagnostics.values()):
            sys.stderr.write(json.dumps({"diagnostics": diagnostics}, separators=(",", ":")) + "\n")
    except Exception:
        # Exception text can contain payload values or filesystem paths.
        _fail_open_error("hook_failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
