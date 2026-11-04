import io
import json
import os
import subprocess
import sys

from workflow_ledger.adapters.codex_hooks import main, process_event
from workflow_ledger.model import stable_id
from workflow_ledger.policy import CollectionPolicy, Selector
from workflow_ledger.report import summarize
from workflow_ledger.sessions import SessionStore
from workflow_ledger.storage import JsonlStore


class MemoryStore:
    def __init__(self):
        self.calls = []
        self.started = set()

    def handle(self, session_id, invocation_id):
        self.calls.append(("handle", session_id, invocation_id))
        return {"trace_id": stable_id("t", session_id), "span_id": stable_id("s", invocation_id, 16)}

    def start(self, kind, identity, **kwargs):
        self.calls.append(("start", kind, identity, kwargs))
        handle = self.handle(kwargs["session_id"], kwargs["invocation_id"])
        self.started.add((handle["trace_id"], handle["span_id"]))
        return {**handle, "recorded": True, "filtered": False}

    def finish(self, trace_id, span_id, **kwargs):
        self.calls.append(("finish", trace_id, span_id, kwargs))
        return {"recorded": (trace_id, span_id) in self.started,
                "missing_start": (trace_id, span_id) not in self.started}


def event(name, **values):
    return {"hook_event_name": name, "session_id": "session-raw", **values}


def test_lifecycle_events_use_hashes_explicit_parents_and_unknown_outcomes():
    store = MemoryStore()
    assert process_event(event("SessionStart", source="startup"), store)["observed"] == 1
    assert process_event(event("PreToolUse", tool_name="Bash", tool_use_id="tool-raw",
                               tool_input={"command": "secret command"}), store)["observed"] == 1
    assert process_event(event("PostToolUse", tool_name="Bash", tool_use_id="tool-raw",
                               tool_response={"is_error": True}), store)["observed"] == 1
    assert process_event(event("SubagentStart", agent_id="agent-raw", agent_type="reviewer"), store)["observed"] == 1
    assert process_event(event("SubagentStop", agent_id="agent-raw", agent_type="reviewer"), store)["observed"] == 1
    assert process_event(event("SessionEnd", reason="other"), store)["observed"] == 1

    tool_start = next(call for call in store.calls if call[0] == "start" and call[1] == "tool")
    assert tool_start[2].namespace == "codex-tool"
    assert tool_start[2].logical_id != "Bash"
    assert tool_start[3]["invocation_id"] == "tool:tool-raw"
    assert tool_start[3]["parent"]["span_id"] == stable_id("s", "session", 16)
    finish_calls = [call for call in store.calls if call[0] == "finish"]
    assert all(call[3]["outcome"] == "unknown" for call in finish_calls)
    identities = [call[2] for call in store.calls if call[0] == "start"]
    assert all(identity.provenance == "native" for identity in identities)
    assert "secret command" not in repr(store.calls)


def test_duplicate_and_missing_start_delivery_is_delegated_idempotently():
    store = MemoryStore()
    process_event(event("SessionStart"), store)
    process_event(event("SessionStart"), store)
    process_event(event("PostToolUse", tool_name="Bash", tool_use_id="orphan"), store)
    assert [call[0] for call in store.calls].count("start") == 2  # session hook retries share marker key
    assert [call[0] for call in store.calls].count("finish") == 1  # missing starts are not fabricated


def test_real_session_store_records_sanitized_codex_lifecycle(tmp_path):
    path = tmp_path / "hooks.jsonl"
    store = SessionStore(path, harness="codex")
    payloads = [
        event("SessionStart", source="startup"),
        event("PreToolUse", tool_name="Bash", tool_use_id="tool-secret", tool_input={"command": "private"}),
        event("PostToolUse", tool_name="Bash", tool_use_id="tool-secret", tool_response={"output": "private"}),
        event("SubagentStart", agent_id="agent-secret", agent_type="reviewer"),
        event("SubagentStop", agent_id="agent-secret", agent_type="reviewer"),
        event("SessionEnd", reason="other"),
    ]
    for payload in payloads:
        result = process_event(payload, store)
        assert result["observed"] == 1
        assert result["storage_errors"] == 0
    raw = path.read_text(encoding="utf-8")
    for secret in ("session-raw", "tool-secret", "agent-secret", "private", "Bash", "reviewer"):
        assert secret not in raw
    records = JsonlStore(path).read()
    assert {record.kind for record in records} == {"workflow", "tool", "agent"}
    assert all(record.end_ns is not None and record.outcome == "unknown" for record in records)


def test_resumed_session_creates_a_new_workflow_segment_and_parents_new_events(tmp_path):
    path = tmp_path / "resumed.jsonl"
    store = SessionStore(path, harness="codex")

    assert process_event(event("SessionStart", source="startup"), store)["observed"] == 1
    assert process_event(event("PreToolUse", tool_name="Bash", tool_use_id="tool-before"),
                         store)["observed"] == 1
    assert process_event(event("PostToolUse", tool_name="Bash", tool_use_id="tool-before"),
                         store)["observed"] == 1
    assert process_event(event("SessionEnd", reason="other"), store)["observed"] == 1

    assert process_event(event("SessionStart", source="resume"), store)["observed"] == 1
    assert process_event(event("SessionStart", source="resume"), store)["observed"] == 1
    assert process_event(event("PreToolUse", tool_name="Bash", tool_use_id="tool-after"),
                         store)["observed"] == 1
    assert process_event(event("PostToolUse", tool_name="Bash", tool_use_id="tool-after"),
                         store)["observed"] == 1
    assert process_event(event("SessionEnd", reason="other"), store)["observed"] == 1

    records = JsonlStore(path).read()
    workflows = [record for record in records if record.kind == "workflow"]
    tools = [record for record in records if record.kind == "tool"]
    assert len(workflows) == 2
    assert len({record.span_id for record in workflows}) == 2
    assert len({record.trace_id for record in workflows}) == 1
    assert all(record.end_ns is not None for record in workflows)
    assert len(tools) == 2
    assert {record.parent_span_id for record in tools} == {record.span_id for record in workflows}


def test_session_end_resolves_and_finishes_workflow_in_one_transaction(tmp_path, monkeypatch):
    path = tmp_path / "session-end.jsonl"
    store = SessionStore(path, harness="codex")
    process_event(event("SessionStart"), store)
    process_event(event("SessionEnd"), store)
    process_event(event("SessionStart", source="resume"), store)

    original_transaction = store._transaction
    transactions = 0

    def count_transactions(mutate, **kwargs):
        nonlocal transactions
        transactions += 1
        return original_transaction(mutate, **kwargs)

    monkeypatch.setattr(store, "_transaction", count_transactions)
    assert process_event(event("SessionEnd"), store)["observed"] == 1
    assert transactions == 1

    workflows = [record for record in JsonlStore(path).read() if record.kind == "workflow"]
    assert len(workflows) == 2
    assert all(record.end_ns is not None for record in workflows)


def test_workflow_exclusion_filters_native_children_even_without_parent_record(tmp_path):
    policy = CollectionPolicy(exclude=(Selector("workflow", "codex-workflow:session"),))
    store = SessionStore(tmp_path / "excluded.jsonl", policy=policy, harness="codex")
    start = process_event(event("SessionStart"), store)
    child = process_event(event("PreToolUse", tool_name="Bash", tool_use_id="tool-1"), store)
    assert start["filtered_records"] == 1
    assert child["filtered_records"] == 1
    assert child["observed"] == 0
    assert not (tmp_path / "excluded.jsonl").exists()


def test_selected_native_agent_retains_only_its_workflow_ancestry(tmp_path):
    selected_agent = "codex-agent:" + stable_id("agent", "quality-gate")
    policy = CollectionPolicy(include=(Selector("agent", selected_agent),))
    path = tmp_path / "selected-agent.jsonl"
    store = SessionStore(path, policy=policy, harness="codex")

    assert process_event(event("SessionStart"), store)["observed"] == 1
    assert process_event(event("SubagentStart", agent_id="agent-selected", agent_type="quality-gate"),
                         store)["observed"] == 1
    assert process_event(event("SubagentStart", agent_id="agent-other", agent_type="worker"),
                         store)["filtered_records"] == 1
    assert process_event(event("PreToolUse", tool_name="Bash", tool_use_id="tool-1"),
                         store)["filtered_records"] == 1

    records = JsonlStore(path).read()
    workflow = next(record for record in records if record.kind == "workflow")
    agent = next(record for record in records if record.kind == "agent")
    assert len(records) == 2
    assert agent.parent_span_id == workflow.span_id
    assert agent.trace_id == workflow.trace_id
    assert agent.associations[0].identity == workflow.identity
    summary = summarize(records)
    assert summary["root_workflows"] == 1
    assert summary["orphan_parent_links"] == 0


def test_explicit_workflow_exclusion_still_filters_selected_native_agent(tmp_path):
    selected_agent = "codex-agent:" + stable_id("agent", "quality-gate")
    policy = CollectionPolicy(
        include=(Selector("agent", selected_agent),),
        exclude=(Selector("workflow", "codex-workflow:session"),),
    )
    store = SessionStore(tmp_path / "excluded-parent.jsonl", policy=policy, harness="codex")

    assert process_event(event("SessionStart"), store)["filtered_records"] == 1
    child = process_event(event("SubagentStart", agent_id="agent-selected", agent_type="quality-gate"), store)
    assert child["filtered_records"] == 1
    assert child["observed"] == 0
    assert not (tmp_path / "excluded-parent.jsonl").exists()


def test_malformed_native_agent_selector_does_not_retain_workflow_root(tmp_path):
    policy = CollectionPolicy(include=(Selector("agent", "codex-agent:anything"),))
    store = SessionStore(tmp_path / "malformed-selector.jsonl", policy=policy, harness="codex")

    assert process_event(event("SessionStart"), store)["filtered_records"] == 1
    child = process_event(event("SubagentStart", agent_id="agent-selected", agent_type="quality-gate"), store)
    assert child["filtered_records"] == 1
    assert child["observed"] == 0
    assert not (tmp_path / "malformed-selector.jsonl").exists()


def test_reused_agent_id_is_scoped_to_validated_turn_id(tmp_path):
    path = tmp_path / "turns.jsonl"
    store = SessionStore(path, harness="codex")
    process_event(event("SessionStart"), store)
    first_start = event("SubagentStart", agent_id="agent-reused", agent_type="worker", turn_id="turn-one")
    for _ in range(2):
        assert process_event(first_start, store)["observed"] == 1
    assert process_event(event("SubagentStop", agent_id="agent-reused", agent_type="worker",
                               turn_id="turn-one"), store)["observed"] == 1
    assert process_event(event("SubagentStart", agent_id="agent-reused", agent_type="worker",
                               turn_id="turn-two"), store)["observed"] == 1
    assert process_event(event("SubagentStop", agent_id="agent-reused", agent_type="worker",
                               turn_id="turn-two"), store)["observed"] == 1

    records = JsonlStore(path).read()
    agents = [record for record in records if record.kind == "agent"]
    assert len(agents) == 2
    assert len({record.span_id for record in agents}) == 2
    persisted = path.read_text(encoding="utf-8")
    assert "turn-one" not in persisted
    assert "turn-two" not in persisted

    rejected = process_event(event("SubagentStart", agent_id="agent-reused", agent_type="worker",
                                   turn_id="invalid\nturn"), store)
    assert rejected["rejected_events"] == 1
    assert rejected["missing_ids"] == 1
    assert len([record for record in JsonlStore(path).read() if record.kind == "agent"]) == 2


def test_unsupported_and_malformed_payloads_are_counted_without_echo():
    store = MemoryStore()
    assert process_event({"hook_event_name": "Stop", "session_id": "raw"}, store)["unsupported_events"] == 1
    assert process_event(event("PreToolUse", tool_name="Bash"), store)["missing_ids"] == 1
    assert process_event([], store)["rejected_events"] == 1
    assert store.calls == []


def test_storage_failure_categories_and_lock_timeouts_are_counted_without_details():
    for category in ("permission_denied", "storage_full", "storage_unavailable"):
        store = MemoryStore()
        store.diagnostics = {"storage_errors": 1, "lock_timeouts": 0}
        store.last_storage_error = category
        store.start = lambda *args, **kwargs: {"recorded": False, "filtered": False}
        result = process_event(event("SessionStart"), store)
        assert result["storage_errors"] == 1
        assert result[category] == 1
        assert sum(result[key] for key in ("permission_denied", "storage_full", "storage_unavailable")) == 1

    store = MemoryStore()
    store.diagnostics = {"storage_errors": 0, "lock_timeouts": 1}
    store.last_storage_error = "lock_timeout"
    store.start = lambda *args, **kwargs: {"recorded": False, "filtered": False}
    result = process_event(event("SessionStart"), store)
    assert result["lock_timeouts"] == 1
    assert result["storage_unavailable"] == 0
    assert result["permission_denied"] == result["storage_full"] == 0


def test_hook_storage_diagnostics_expose_only_static_categories(monkeypatch, tmp_path, capsys):
    import workflow_ledger.sessions as sessions

    class FailedStore:
        diagnostics = {"storage_errors": 1, "lock_timeouts": 0}
        last_storage_error = "permission_denied"

        def __init__(self, *args, **kwargs):
            pass

        def start(self, *args, **kwargs):
            return {"recorded": False, "filtered": False}

    monkeypatch.setattr(sessions, "SessionStore", FailedStore)
    payload = json.dumps(event("SessionStart", secret="payload-secret")).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
    monkeypatch.setattr(os, "environ", {"WORKFLOW_LEDGER_ENABLED": "1", "WORKFLOW_LEDGER_STORE": str(tmp_path / "private-store.jsonl")})

    assert main([]) == 0
    output = capsys.readouterr().err
    assert json.loads(output) == {"diagnostics": {
        "observed": 0, "filtered_records": 0, "missing_starts": 0, "storage_errors": 1,
        "lock_timeouts": 0, "permission_denied": 1, "storage_full": 0,
        "storage_unavailable": 0, "rejected_events": 0, "missing_ids": 0,
        "unsupported_events": 0,
    }}
    assert str(tmp_path) not in output
    assert "payload-secret" not in output


def test_disabled_flag_precedes_environment_and_stdin(monkeypatch):
    class ExplodingStdin:
        @property
        def buffer(self):
            raise AssertionError("stdin must not be inspected")

    monkeypatch.setattr(sys, "stdin", ExplodingStdin())
    monkeypatch.setattr(os, "environ", {"WORKFLOW_LEDGER_ENABLED": "1", "WORKFLOW_LEDGER_STORE": "/tmp/never-open"})
    assert main(["--disabled"]) == 0


def test_policy_disable_precedes_payload_read(monkeypatch, tmp_path):
    class ExplodingStdin:
        @property
        def buffer(self):
            raise AssertionError("disabled policy must short-circuit before stdin")

    policy_file = tmp_path / "disabled.json"
    policy_file.write_text('{"enabled": false}', encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", ExplodingStdin())
    monkeypatch.setattr(os, "environ", {"WORKFLOW_LEDGER_ENABLED": "1", "WORKFLOW_LEDGER_STORE": str(tmp_path / "store.jsonl"),
                                         "WORKFLOW_LEDGER_POLICY": str(policy_file)})
    assert main([]) == 0
    assert not (tmp_path / "store.jsonl").exists()


def test_invalid_retention_settings_report_static_fail_open_error(monkeypatch, tmp_path, capsys):
    settings = tmp_path / "retention.json"
    settings.write_text('{"rotation_bytes": 10, "archive_cap_bytes": 1}', encoding="utf-8")
    payload = json.dumps(event("SessionStart")).encode()
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(payload)))
    monkeypatch.setattr(os, "environ", {
        "WORKFLOW_LEDGER_ENABLED": "1", "WORKFLOW_LEDGER_STORE": str(tmp_path / "store.jsonl"),
        "WORKFLOW_LEDGER_RETENTION_POLICY": str(settings),
    })

    assert main([]) == 0
    assert json.loads(capsys.readouterr().err) == {"error": "retention_policy_error"}
    assert not (tmp_path / "store.jsonl").exists()


def test_subprocess_disabled_hook_exits_zero_with_empty_stdout(tmp_path):
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), WORKFLOW_LEDGER_ENABLED="1",
               WORKFLOW_LEDGER_STORE=str(tmp_path / "never-created.jsonl"))
    completed = subprocess.run([sys.executable, "-m", "workflow_ledger.adapters.codex_hooks", "--disabled"],
                               input=b"not json", capture_output=True, env=env, check=False)
    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""
    assert not (tmp_path / "never-created.jsonl").exists()


def test_subprocess_enabled_hook_records_without_stdout_or_source_content(tmp_path):
    store_path = tmp_path / "hooks.jsonl"
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path), WORKFLOW_LEDGER_ENABLED="1", WORKFLOW_LEDGER_STORE=str(store_path))
    payload = {"hook_event_name": "SessionStart", "session_id": "private-session",
               "transcript_path": "/private/secret.jsonl", "source": "startup"}
    completed = subprocess.run([sys.executable, "-m", "workflow_ledger.adapters.codex_hooks"],
                               input=json.dumps(payload).encode(), capture_output=True, env=env, check=False)
    assert completed.returncode == 0
    assert completed.stdout == b""
    assert "private-session" not in completed.stderr.decode()
    persisted = store_path.read_text(encoding="utf-8")
    assert "private-session" not in persisted
    assert "/private/secret.jsonl" not in persisted


def test_cli_malformed_input_is_fail_open_and_diagnostic_only(monkeypatch, capsys):
    monkeypatch.setattr(os, "environ", {"WORKFLOW_LEDGER_ENABLED": "1", "WORKFLOW_LEDGER_STORE": "/tmp/awo-test-store.jsonl"})
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b"{bad")))
    assert main([]) == 0
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"error": "invalid_payload"}
