"""Read-only, best-effort importer for a narrow Codex rollout JSONL shape.

No transcript text is searched for agent/skill attribution. Session timestamps
describe an observation window, not active execution time. The cumulative token
shape is version-sensitive and is not a public stable Codex API.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path

from ..model import AssetIdentity, Capabilities, SpanRecord, TokenUsage, stable_id, timestamp_ns
from ..policy import CollectionPolicy

CAPABILITIES = Capabilities(
    "codex-artifacts", "0.2.0", "inferred", "unavailable", "unavailable", "unavailable",
    "unavailable", "inferred", "direct", (
        "Only session observation windows and final cumulative token counts are imported.",
        "An imported rollout file does not prove that a configured session was executed.",
        "Rollout JSONL shape is not a stable public API; unknown shapes are diagnosed.",
        "Active time, tool timing, model-call usage, and skill/agent attribution are unavailable.",
        "Token-counter resets are treated as ambiguous; affected counts are withheld.",
        "Session outcome and capture completeness are unknown.",
    ),
)
_FIELDS = {"input_tokens": "input_tokens", "cached_input_tokens": "cached_input_tokens",
           "output_tokens": "output_tokens", "reasoning_tokens": "reasoning_output_tokens"}


@dataclass
class ImportResult:
    records: list[SpanRecord] = field(default_factory=list)
    diagnostics: dict = field(default_factory=lambda: {
        "lines": 0, "malformed_records": 0, "unsupported_records": 0,
        "usage_snapshots": 0, "duplicate_snapshots": 0, "counter_resets": 0,
        "invalid_token_fields": 0, "missing_session_id": 0, "source_errors": 0,
        "filtered_records": 0, "disabled": 0,
    })


def import_codex(path: str | Path, policy: CollectionPolicy | None = None) -> ImportResult:
    result = ImportResult()
    policy = policy if policy is not None else CollectionPolicy()
    if not policy.may_observe_runtime("codex"):
        result.diagnostics["disabled"] += 1
        return result
    session_id, version = None, None
    earliest_time = latest_time = None
    latest_snapshot = None
    previous_known: dict[str, int] = {}
    reset_fields: set[str] = set()
    try:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                result.diagnostics["lines"] += 1
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                        raise ValueError("Unsupported record")
                except (ValueError, TypeError):
                    result.diagnostics["malformed_records"] += 1
                    continue
                instant = timestamp_ns(row.get("timestamp"))
                if instant is not None:
                    earliest_time = instant if earliest_time is None else min(earliest_time, instant)
                    latest_time = instant if latest_time is None else max(latest_time, instant)
                payload = row["payload"]
                if row.get("type") == "session_meta":
                    incoming_id = payload.get("id")
                    if isinstance(incoming_id, str) and incoming_id:
                        if session_id is not None and session_id != incoming_id:
                            # A file claiming multiple sessions cannot provide an unambiguous total.
                            result.diagnostics["unsupported_records"] += 1
                            return result
                        session_id = incoming_id
                    if isinstance(payload.get("cli_version"), str):
                        version = payload["cli_version"][:256]
                elif row.get("type") == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    total = info.get("total_token_usage") if isinstance(info, dict) else None
                    if not isinstance(total, dict):
                        result.diagnostics["unsupported_records"] += 1
                        continue
                    snapshot = {}
                    for target, native in _FIELDS.items():
                        value = total.get(native)
                        if value is not None and (type(value) is not int or value < 0):
                            result.diagnostics["invalid_token_fields"] += 1
                            value = None
                        snapshot[target] = value
                        previous = previous_known.get(target)
                        if previous is not None and value is not None and value < previous:
                            reset_fields.add(target)
                        if value is not None:
                            previous_known[target] = value
                    result.diagnostics["usage_snapshots"] += 1
                    if latest_snapshot == snapshot:
                        result.diagnostics["duplicate_snapshots"] += 1
                    latest_snapshot = snapshot
                elif row.get("type") not in {"response_item", "event_msg", "turn_context"}:
                    result.diagnostics["unsupported_records"] += 1
    except (OSError, UnicodeError):
        result.diagnostics["source_errors"] += 1
        return result
    if session_id is None:
        result.diagnostics["missing_session_id"] += 1
        return result
    result.diagnostics["counter_resets"] = len(reset_fields)
    usage = None
    if latest_snapshot is not None:
        latest = latest_snapshot.copy()
        for name in reset_fields:
            latest[name] = None
        # Invalid subset fields do not invalidate independently observed primary fields.
        for subset, parent in (("cached_input_tokens", "input_tokens"),
                               ("reasoning_tokens", "output_tokens")):
            if latest[subset] is not None and latest[parent] is not None and latest[subset] > latest[parent]:
                latest[subset] = None
                result.diagnostics["invalid_token_fields"] += 1
        usage = TokenUsage(**latest, scope="run", source="native_cumulative_snapshot")
    identity = AssetIdentity("codex", stable_id("codex/session-asset", session_id), provenance="native")
    record = SpanRecord(
        trace_id=stable_id("codex/session", session_id),
        span_id=stable_id("codex/root", session_id, 16), kind="workflow", name="codex.session",
        harness="codex", adapter=CAPABILITIES.adapter, source_version=version,
        start_ns=earliest_time, end_ns=latest_time,
        duration_quality="inferred" if earliest_time is not None else "unavailable", usage=usage,
        identity=identity, lifecycle="observed",
    )
    if policy.allows(record):
        result.records.append(record)
    else:
        result.diagnostics["filtered_records"] += 1
    return result
