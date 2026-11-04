"""Cross-process, cooperative JSONL lifecycle markers for skills and agents."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import re
import time
import uuid

from .model import Association, AssetIdentity, SpanLink, SpanRecord, stable_id
from .policy import CollectionPolicy
from .retention import (
    RetentionPolicy,
    StoreLockTimeout,
    append_record,
    read_archived_records,
    read_store_records,
    resolve_retention_policy,
    store_lock,
)


def _identity_json(identity: AssetIdentity) -> str:
    return json.dumps({
        "namespace": identity.namespace,
        "logical_id": identity.logical_id,
        "version": identity.version,
        "provenance": identity.provenance,
    }, sort_keys=True, separators=(",", ":"))


class SessionStore:
    """Record explicit start/finish markers with fresh reads under a POSIX lock."""

    LOCK_TIMEOUT_SECONDS = 2.0

    def __init__(
        self,
        path: str | Path,
        policy: CollectionPolicy | None = None,
        harness: str = "codex",
        retention: RetentionPolicy | None = None,
    ):
        if os.name == "nt":
            raise OSError("Session markers require POSIX advisory locks")
        self.path = Path(path)
        self.policy = policy or CollectionPolicy()
        self.harness = harness
        self.retention = resolve_retention_policy(retention)
        self.diagnostics = {"storage_errors": 0, "lock_timeouts": 0}
        self.last_storage_error: str | None = None

    @staticmethod
    def handle(session_id: str, invocation_id: str) -> dict:
        if not isinstance(session_id, str) or not session_id or not isinstance(invocation_id, str) or not invocation_id:
            raise ValueError("Invalid native marker identifiers")
        return {
            "trace_id": stable_id("workflow-ledger-session", session_id, 32),
            "span_id": stable_id("workflow-ledger-invocation", invocation_id, 16),
        }

    def _transaction(self, mutate, *, archive_keys=None, archive_predicate=None,
                    archive_selector=None):
        self.last_storage_error = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with store_lock(self.path, self.LOCK_TIMEOUT_SECONDS):
                records_list, _ = read_store_records(self.path, include_archives=False)
                archived, _ = read_archived_records(
                    self.path, keys=archive_keys, predicate=archive_predicate,
                    selector=archive_selector,
                ) if archive_keys is not None or archive_predicate is not None else ([], 0)
                records = {record.key: record for record in archived}
                records.update({record.key: record for record in records_list})
                result, changed = mutate(records)
                if changed:
                    record = result.pop("_record")
                    append_record(
                        self.path,
                        json.dumps(record.to_dict(), separators=(",", ":")) + "\n",
                        records,
                        self.retention,
                    )
                return result
        except StoreLockTimeout:
            self.diagnostics["lock_timeouts"] += 1
            self.last_storage_error = "lock_timeout"
            return None
        except OSError as exc:
            self.diagnostics["storage_errors"] += 1
            if exc.errno in {errno.EACCES, errno.EPERM}:
                self.last_storage_error = "permission_denied"
            elif exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
                self.last_storage_error = "storage_full"
            else:
                self.last_storage_error = "storage_unavailable"
            return None
        except (UnicodeError, ValueError, TypeError):
            self.diagnostics["storage_errors"] += 1
            self.last_storage_error = "storage_unavailable"
            return None

    def latest_workflow(self, session_id: str, identity: AssetIdentity) -> dict | None:
        """Return the newest selected workflow segment for a native session."""
        trace_id = stable_id("workflow-ledger-session", session_id, 32)

        def query(records):
            candidates = [
                record for record in records.values()
                if record.trace_id == trace_id and record.kind == "workflow"
                and record.identity == identity and self.policy.allows(record)
            ]
            if not candidates:
                return ({"found": False}, False)
            record = max(candidates, key=lambda item: (item.start_ns, item.span_id))
            return ({
                "found": True,
                "trace_id": record.trace_id,
                "span_id": record.span_id,
                "end_ns": record.end_ns,
            }, False)

        result = self._transaction(
            query,
            archive_predicate=lambda record: (
                record.trace_id == trace_id and record.kind == "workflow"
                and record.identity == identity
            ),
            archive_selector=(trace_id, "workflow", _identity_json(identity)),
        )
        if result is None or not result["found"]:
            return None
        return result

    def start_workflow(self, session_id: str, identity: AssetIdentity) -> dict:
        """Start a new workflow segment after a completed native session segment."""
        latest = self.latest_workflow(session_id, identity)
        if latest is not None and latest["end_ns"] is None:
            return {
                "trace_id": latest["trace_id"],
                "span_id": latest["span_id"],
                "recorded": True,
                "filtered": False,
            }
        # Derive the resume key from the segment being resumed. Concurrent
        # duplicate SessionStart events then converge on one idempotent marker.
        invocation_id = "session" if latest is None else f"session:resume:{latest['span_id']}"
        return self.start(
            "workflow", identity, session_id=session_id,
            invocation_id=invocation_id, evidence="native",
        )

    def workflow_handle(self, session_id: str, identity: AssetIdentity) -> dict:
        """Resolve child events to the latest workflow segment for this session."""
        latest = self.latest_workflow(session_id, identity)
        if latest is not None:
            return {"trace_id": latest["trace_id"], "span_id": latest["span_id"]}
        return self.handle(session_id, "session")

    def finish_workflow(
        self,
        session_id: str,
        identity: AssetIdentity,
        *,
        outcome: str = "unknown",
        timestamp: int | None = None,
    ) -> dict:
        """Finish the latest workflow segment with one store transaction."""
        if not self.policy.may_observe_runtime(self.harness):
            return {"recorded": False, "missing_start": False, "filtered": True}
        if outcome not in {"ok", "error", "cancelled", "unknown"}:
            raise ValueError("Invalid marker outcome")
        if timestamp is not None and (type(timestamp) is not int or timestamp < 0):
            raise ValueError("Invalid marker timestamp")
        trace_id = stable_id("workflow-ledger-session", session_id, 32)
        fallback = self.handle(session_id, "session")
        ended_ns = time.time_ns() if timestamp is None else timestamp

        def mutate(records):
            candidates = [
                record for record in records.values()
                if record.trace_id == trace_id and record.kind == "workflow"
                and record.identity == identity and self.policy.allows(record)
            ]
            record = max(candidates, key=lambda item: (item.start_ns, item.span_id)) if candidates else None
            if record is None:
                record = records.get(f"{fallback['trace_id']}/{fallback['span_id']}")
            if record is None:
                return ({"recorded": False, "missing_start": True}, False)
            if not self.policy.allows(record):
                return ({"recorded": False, "missing_start": False, "filtered": True}, False)
            if record.end_ns is not None:
                return ({"recorded": True, "missing_start": False}, False)
            terminal = {"ok": "completed", "unknown": "completed", "error": "failed", "cancelled": "cancelled"}[outcome]
            changed = SpanRecord.from_dict({**record.to_dict(), "end_ns": ended_ns,
                                            "outcome": outcome, "lifecycle": terminal,
                                            "duration_quality": "inferred"})
            return ({"recorded": True, "missing_start": False, "_record": changed}, True)

        return self._transaction(
            mutate,
            archive_predicate=lambda record: (
                record.trace_id == trace_id and record.kind == "workflow"
                and record.identity == identity
            ),
            archive_selector=(trace_id, "workflow", _identity_json(identity)),
        ) or {"recorded": False}

    def start(
        self,
        kind: str,
        identity: AssetIdentity,
        *,
        session_id: str | None = None,
        invocation_id: str | None = None,
        retry_handle: dict | None = None,
        parent: dict | None = None,
        associations: tuple = (),
        links: tuple[SpanLink, ...] = (),
        evidence: str = "caller_declared",
        timestamp: int | None = None,
    ) -> dict:
        if retry_handle is not None:
            if (session_id is not None or invocation_id is not None
                    or not isinstance(retry_handle, dict)
                    or set(retry_handle) != {"trace_id", "span_id"}
                    or not isinstance(retry_handle["trace_id"], str)
                    or not re.fullmatch(r"[0-9a-f]{32}", retry_handle["trace_id"])
                    or int(retry_handle["trace_id"], 16) == 0
                    or not isinstance(retry_handle["span_id"], str)
                    or not re.fullmatch(r"[0-9a-f]{16}", retry_handle["span_id"])
                    or int(retry_handle["span_id"], 16) == 0):
                raise ValueError("Invalid retry handle")
            ids = dict(retry_handle)
        else:
            if session_id is None:
                raise ValueError("Missing marker session")
            invocation = str(uuid.uuid4()) if invocation_id is None else invocation_id
            ids = self.handle(session_id, invocation)
        result = {**ids, "recorded": False, "filtered": False}
        if not self.policy.may_observe_runtime(self.harness):
            result["filtered"] = True
            return result
        if kind not in {"workflow", "agent", "skill", "model", "tool", "wait"} or not isinstance(identity, AssetIdentity):
            raise ValueError("Invalid marker kind or identity")
        if evidence not in {"native", "caller_declared"}:
            raise ValueError("Invalid marker evidence")
        if parent is not None:
            if not isinstance(parent, dict) or not {"trace_id", "span_id"} <= parent.keys():
                raise ValueError("Invalid parent handle")
            parent = {"trace_id": parent["trace_id"], "span_id": parent["span_id"]}
            if (not isinstance(parent["trace_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", parent["trace_id"])
                    or int(parent["trace_id"], 16) == 0 or not isinstance(parent["span_id"], str)
                    or not re.fullmatch(r"[0-9a-f]{16}", parent["span_id"]) or int(parent["span_id"], 16) == 0):
                raise ValueError("Invalid parent handle")
            if parent["trace_id"] != ids["trace_id"]:
                raise ValueError("Parent belongs to another trace")
            if parent["span_id"] == ids["span_id"]:
                raise ValueError("Operation cannot parent itself")
        if not isinstance(associations, tuple) or not all(isinstance(a, Association) for a in associations):
            raise ValueError("Invalid associations")
        if not isinstance(links, tuple) or not all(isinstance(link, SpanLink) for link in links):
            raise ValueError("Invalid span links")
        if links and kind != "wait":
            raise ValueError("Only wait markers can contain dependency links")
        if any(link.trace_id == ids["trace_id"] and link.span_id == ids["span_id"] for link in links):
            raise ValueError("Operation cannot wait for itself")
        if timestamp is not None and (type(timestamp) is not int or timestamp < 0):
            raise ValueError("Invalid marker timestamp")
        started_ns = time.time_ns() if timestamp is None else timestamp

        def mutate(records):
            key = f"{ids['trace_id']}/{ids['span_id']}"
            prior = records.get(key)
            if prior is not None:
                if (prior.kind != kind or prior.identity != identity or prior.name != identity.key
                        or prior.parent_span_id != (parent["span_id"] if parent else None)
                        or prior.links != links):
                    return ({"_identity_collision": True}, False)
                if not self.policy.allows(prior):
                    return ({**ids, "recorded": False, "filtered": True}, False)
                return ({**ids, "recorded": True, "filtered": False}, False)
            parent_record = records.get(f"{parent['trace_id']}/{parent['span_id']}") if parent else None
            if parent is not None and parent_record is None and evidence == "caller_declared":
                return ({**ids, "recorded": False, "filtered": False, "missing_parent": True}, False)
            if parent_record is not None and not self.policy.allows(parent_record) and evidence == "caller_declared":
                return ({**ids, "recorded": False, "filtered": False, "missing_parent": True}, False)
            inherited = list(associations)
            parent_span_id = parent["span_id"] if parent else None
            if parent_record is not None:
                inherited = list(parent_record.associations) + inherited
                parent_identity_measured = parent_record.identity and any(
                    association.kind == parent_record.kind
                    and association.identity == parent_record.identity
                    and association.invocation_trace_id == parent_record.trace_id
                    and association.invocation_span_id == parent_record.span_id
                    and association.measured
                    for association in inherited
                )
                if (parent_record.identity and not parent_identity_measured
                        and parent_record.lifecycle in {"invoked", "completed", "failed", "cancelled"}):
                    inherited.insert(0, Association(
                        kind=parent_record.kind, identity=parent_record.identity, state="invoked",
                        evidence=parent_record.identity.provenance if parent_record.identity.provenance in {"native", "caller_declared"} else "legacy",
                        invocation_span_id=parent_record.span_id,
                        invocation_trace_id=parent_record.trace_id,
                    ))
            if kind == "agent":
                # A nested agent becomes the nearest agent for its descendants.
                inherited = [association for association in inherited if association.kind != "agent"]
            inherited.append(Association(
                kind=kind, identity=identity, state="invoked", evidence=evidence,
                invocation_span_id=ids["span_id"], invocation_trace_id=ids["trace_id"],
            ))
            record = SpanRecord(
                trace_id=ids["trace_id"], span_id=ids["span_id"], kind=kind,
                name=identity.key, identity=identity, harness=self.harness,
                adapter="shared_markers", adapter_version="0.3.0", parent_span_id=parent_span_id,
                start_ns=started_ns, lifecycle="invoked", associations=tuple(inherited), links=links,
            )
            if not self.policy.allows(record):
                return ({**ids, "recorded": False, "filtered": True}, False)
            return ({**ids, "recorded": True, "filtered": False, "_record": record}, True)

        archive_keys = {f"{ids['trace_id']}/{ids['span_id']}"}
        if parent is not None:
            archive_keys.add(f"{parent['trace_id']}/{parent['span_id']}")
        saved = self._transaction(mutate, archive_keys=archive_keys)
        if saved is not None:
            if saved.pop("_identity_collision", False):
                raise ValueError("Marker identity collision")
            return saved
        return result

    def finish(
        self,
        trace_id: str,
        span_id: str,
        *,
        outcome: str = "unknown",
        timestamp: int | None = None,
    ) -> dict:
        if not self.policy.may_observe_runtime(self.harness):
            return {"recorded": False, "missing_start": False, "filtered": True}
        if outcome not in {"ok", "error", "cancelled", "unknown"}:
            raise ValueError("Invalid marker outcome")
        if (not isinstance(trace_id, str) or not re.fullmatch(r"[0-9a-f]{32}", trace_id)
                or int(trace_id, 16) == 0 or not isinstance(span_id, str)
                or not re.fullmatch(r"[0-9a-f]{16}", span_id) or int(span_id, 16) == 0):
            raise ValueError("Invalid marker handle")
        if timestamp is not None and (type(timestamp) is not int or timestamp < 0):
            raise ValueError("Invalid marker timestamp")
        ended_ns = time.time_ns() if timestamp is None else timestamp

        def mutate(records):
            key = f"{trace_id}/{span_id}"
            record = records.get(key)
            if record is None:
                return ({"recorded": False, "missing_start": True}, False)
            if not self.policy.allows(record):
                return ({"recorded": False, "missing_start": False, "filtered": True}, False)
            if record.end_ns is not None:
                return ({"recorded": True, "missing_start": False}, False)
            terminal = {"ok": "completed", "unknown": "completed", "error": "failed", "cancelled": "cancelled"}[outcome]
            changed = SpanRecord.from_dict({**record.to_dict(), "end_ns": ended_ns,
                                            "outcome": outcome, "lifecycle": terminal,
                                            "duration_quality": "inferred"})
            return ({"recorded": True, "missing_start": False, "_record": changed}, True)

        saved = self._transaction(mutate, archive_keys={f"{trace_id}/{span_id}"})
        return saved if saved is not None else {"recorded": False}


def main(argv: list[str] | None = None) -> int:
    """CLI for sharing start/finish handles across independent processes."""
    import argparse

    class SafeArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            print(json.dumps({"error": "marker_arguments_invalid"}))
            self.exit(2)

    parser = SafeArgumentParser(description="Record local agent lifecycle markers")
    parser.add_argument("--disabled", action="store_true", help="Return without reading policy or store")
    parser.add_argument("--policy", type=Path, help="Explicit JSON collection policy")
    parser.add_argument("--retention-policy", type=Path,
                        help="Optional JSON rotation and archive-cap settings")
    parser.add_argument("--store", type=Path, required=True, help="Local JSONL marker store")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=SafeArgumentParser)
    start_parser = commands.add_parser("start")
    start_ids = start_parser.add_mutually_exclusive_group(required=True)
    start_ids.add_argument("--session")
    start_ids.add_argument("--retry-handle", help="Retry using TRACE_ID:SPAN_ID from a failed start")
    start_parser.add_argument("--kind", choices=("workflow", "skill", "agent", "wait"), required=True)
    start_parser.add_argument("--identity", required=True, help="namespace:logical_id")
    start_parser.add_argument("--version")
    start_parser.add_argument("--invocation")
    start_parser.add_argument("--parent", help="Parent TRACE_ID:SPAN_ID handle")
    start_parser.add_argument("--waits-for", action="append", default=[],
                              help="Target TRACE_ID:SPAN_ID invocation (repeatable; wait spans only)")
    start_parser.add_argument("--timestamp-ns", type=int, help="Original marker timestamp for recovery")
    finish_parser = commands.add_parser("finish")
    finish_parser.add_argument("--handle", required=True, help="TRACE_ID:SPAN_ID handle")
    finish_parser.add_argument("--outcome", choices=("ok", "error", "cancelled", "unknown"), default="unknown")
    finish_parser.add_argument("--timestamp-ns", type=int, help="Original marker timestamp for recovery")
    args = parser.parse_args(argv)

    if args.disabled:
        print(json.dumps({"disabled": True}))
        return 0
    try:
        policy = CollectionPolicy.load(args.policy) if args.policy else CollectionPolicy()
        if not policy.enabled or not policy.may_observe_runtime("codex"):
            print(json.dumps({"disabled": True}))
            return 0
        retention = (RetentionPolicy.load(args.retention_policy) if args.retention_policy
                     else resolve_retention_policy())
        store = SessionStore(args.store, policy=policy, retention=retention)
        if args.command == "start":
            if ":" not in args.identity:
                raise ValueError("Invalid identity")
            namespace, logical_id = args.identity.split(":", 1)
            identity = AssetIdentity(namespace, logical_id, args.version)
            parent = None
            if args.parent:
                pieces = args.parent.split(":")
                if len(pieces) != 2:
                    raise ValueError("Invalid parent handle")
                parent = {"trace_id": pieces[0], "span_id": pieces[1]}
            timestamp = time.time_ns() if args.timestamp_ns is None else args.timestamp_ns
            retry_handle = None
            if args.retry_handle:
                pieces = args.retry_handle.split(":")
                if len(pieces) != 2:
                    raise ValueError("Invalid retry handle")
                retry_handle = {"trace_id": pieces[0], "span_id": pieces[1]}
            links = []
            for value in args.waits_for:
                pieces = value.split(":")
                if len(pieces) != 2:
                    raise ValueError("Invalid dependency target")
                links.append(SpanLink("waits_for", pieces[0], pieces[1]))
            output = store.start(args.kind, identity, session_id=args.session,
                                 invocation_id=args.invocation, retry_handle=retry_handle,
                                 parent=parent, links=tuple(links), timestamp=timestamp)
        else:
            pieces = args.handle.split(":")
            if len(pieces) != 2:
                raise ValueError("Invalid marker handle")
            timestamp = time.time_ns() if args.timestamp_ns is None else args.timestamp_ns
            output = store.finish(pieces[0], pieces[1], outcome=args.outcome, timestamp=timestamp)
            output["trace_id"], output["span_id"] = pieces
        output["timestamp_ns"] = timestamp
        if store.last_storage_error is not None:
            output["storage_error"] = store.last_storage_error
        output["diagnostics"] = dict(store.diagnostics)
        print(json.dumps(output))
        if output.get("filtered"):
            return 0
        if output.get("missing_start") or output.get("missing_parent"):
            return 1
        if not output.get("recorded"):
            return 1
        return 0
    except (OSError, UnicodeError, ValueError, TypeError):
        # Never include arguments, paths, native identifiers, or exception text.
        print(json.dumps({"error": "marker_operation_failed"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
