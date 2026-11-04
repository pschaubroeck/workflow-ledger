import errno
import json
import fcntl
import os
from pathlib import Path
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import workflow_ledger.sessions as sessions_module
from workflow_ledger.model import Association, AssetIdentity, SpanLink, SpanRecord
from workflow_ledger.policy import CollectionPolicy, Selector
from workflow_ledger.report import ownership, summarize
from workflow_ledger.retention import RetentionPolicy, archive_paths
from workflow_ledger.sessions import SessionStore, main as marker_main


def rows(path):
    latest = {}
    if not path.exists():
        return []
    for line in path.read_text().splitlines():
        record = SpanRecord.from_dict(json.loads(line))
        latest[record.key] = record
    return list(latest.values())


def test_markers_establish_own_invocation_identity_for_reports(tmp_path):
    for kind in ("workflow", "agent", "skill", "model", "tool", "wait"):
        path = tmp_path / f"{kind}.jsonl"
        identity = AssetIdentity("personal", "existing")
        store = SessionStore(path)
        started = store.start(kind, identity, session_id="s", invocation_id="i", timestamp=100)
        store.finish(started["trace_id"], started["span_id"], timestamp=200)
        record, = rows(path)
        assert ownership(record, kind) == identity
        assert record.associations[0].invocation_span_id == record.span_id
        assert record.associations[0].invocation_trace_id == record.trace_id
        group_by = kind if kind in {"agent", "skill", "model", "tool"} else "kind"
        report = summarize([record], group_by)
        assert report["groups"][0]["group"] == (identity.key if group_by != "kind" else kind)
        assert report["groups"][0]["outcomes"] == {"unknown": 1}


def test_start_finish_are_idempotent_across_store_instances(tmp_path):
    path = tmp_path / "markers.jsonl"
    identity = AssetIdentity("skills", "daily-summary")
    first = SessionStore(path).start("skill", identity, session_id="native-session-secret",
                                     invocation_id="native-invocation-secret", timestamp=100)
    repeated = SessionStore(path).start("skill", identity, session_id="native-session-secret",
                                        invocation_id="native-invocation-secret", timestamp=200)
    assert first == repeated
    assert first["recorded"] and not first["filtered"]
    assert SessionStore(path).finish(first["trace_id"], first["span_id"], outcome="ok", timestamp=150)["recorded"]
    assert SessionStore(path).finish(first["trace_id"], first["span_id"], outcome="error",
                                     timestamp=175)["recorded"]
    record, = rows(path)
    assert record.start_ns == 100
    assert record.end_ns == 150
    assert record.outcome == "ok"
    assert record.lifecycle == "completed"
    assert record.duration_quality == "inferred"
    assert record.usage is None
    raw = path.read_text()
    assert "native-session-secret" not in raw
    assert "native-invocation-secret" not in raw


def test_start_and_finish_retries_find_completed_marker_in_retained_archive(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path, retention=RetentionPolicy(rotation_bytes=1,
                                                          archive_cap_bytes=1024 * 1024))
    identity = AssetIdentity("skills", "retained")
    started = store.start("skill", identity, session_id="s", invocation_id="i", timestamp=100)
    assert store.finish(started["trace_id"], started["span_id"], outcome="ok", timestamp=200)["recorded"]
    # Move the completed revision out of the active file before retrying it.
    store.start("skill", AssetIdentity("skills", "other"), session_id="s",
                invocation_id="other", timestamp=300)
    before = path.read_text(encoding="utf-8") + "".join(
        archive.read_text(encoding="utf-8") for archive in archive_paths(path)
    )

    repeated_start = store.start("skill", identity, session_id="s", invocation_id="i", timestamp=400)
    repeated_finish = store.finish(started["trace_id"], started["span_id"],
                                   outcome="error", timestamp=500)
    index_path = path.with_name(path.name + ".archive-index.sqlite3")
    after = path.read_text(encoding="utf-8") + "".join(
        archive.read_text(encoding="utf-8") for archive in archive_paths(path)
    )

    assert repeated_start == started
    assert repeated_finish["recorded"] and not repeated_finish["missing_start"]
    assert after == before
    assert index_path.stat().st_mode & 0o777 == 0o600


def test_parent_and_workflow_lookup_use_latest_retained_archive_revision(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path, retention=RetentionPolicy(rotation_bytes=1,
                                                          archive_cap_bytes=1024 * 1024))
    parent_identity = AssetIdentity("agents", "retained-parent")
    parent = store.start("agent", parent_identity, session_id="s", invocation_id="parent",
                         timestamp=100)
    assert store.finish(parent["trace_id"], parent["span_id"], outcome="error", timestamp=200)["recorded"]
    store.start("skill", AssetIdentity("skills", "rotate"), session_id="s",
                invocation_id="rotate", timestamp=300)

    child = store.start("skill", AssetIdentity("skills", "child"), session_id="s",
                        invocation_id="child", parent=parent, timestamp=400)
    child_record = next(record for record in rows(path) if record.span_id == child["span_id"])
    assert child["recorded"]
    assert child_record.parent_span_id == parent["span_id"]
    assert child_record.associations[0].identity == parent_identity

    workflow_identity = AssetIdentity("caller", "context")
    workflow_store = SessionStore(tmp_path / "workflows.jsonl", retention=RetentionPolicy(
        rotation_bytes=1, archive_cap_bytes=1024 * 1024
    ))
    first = workflow_store.start_workflow("native-session", workflow_identity)
    workflow_store.finish_workflow("native-session", workflow_identity, outcome="ok")
    second = workflow_store.start_workflow("native-session", workflow_identity)
    assert second["recorded"] and second["span_id"] != first["span_id"]
    assert workflow_store.workflow_handle("native-session", workflow_identity)["span_id"] == second["span_id"]


def test_concurrent_duplicate_workflow_resume_starts_converge(tmp_path, monkeypatch):
    path = tmp_path / "workflows.jsonl"
    identity = AssetIdentity("caller", "context")
    store = SessionStore(path)
    first = store.start_workflow("native-session", identity)
    store.finish_workflow("native-session", identity, outcome="ok")

    barrier = Barrier(2)
    original = SessionStore.latest_workflow

    def synchronized_latest(instance, session_id, workflow_identity):
        latest = original(instance, session_id, workflow_identity)
        barrier.wait(timeout=5)
        return latest

    monkeypatch.setattr(SessionStore, "latest_workflow", synchronized_latest)
    with ThreadPoolExecutor(max_workers=2) as pool:
        resumed = list(pool.map(
            lambda _: SessionStore(path).start_workflow("native-session", identity), range(2)
        ))

    assert resumed[0] == resumed[1]
    assert resumed[0]["recorded"]
    assert resumed[0]["span_id"] != first["span_id"]
    workflows = [record for record in rows(path) if record.kind == "workflow"]
    assert len(workflows) == 2


def test_reused_marker_identity_rejects_changed_parent_or_wait_target(tmp_path):
    store = SessionStore(tmp_path / "markers.jsonl")
    identity = AssetIdentity("agents", "waiter")
    parent_identity = AssetIdentity("agents", "parent")
    target_identity = AssetIdentity("agents", "target")
    parent_a = store.start("agent", parent_identity, session_id="s", invocation_id="parent-a")
    parent_b = store.start("agent", parent_identity, session_id="s", invocation_id="parent-b")
    target_a = store.start("agent", target_identity, session_id="s", invocation_id="target-a")
    target_b = store.start("agent", target_identity, session_id="s", invocation_id="target-b")
    link_a = (SpanLink("waits_for", target_a["trace_id"], target_a["span_id"]),)
    link_b = (SpanLink("waits_for", target_b["trace_id"], target_b["span_id"]),)

    first = store.start("wait", identity, session_id="s", invocation_id="same-wait",
                        parent=parent_a, links=link_a)
    assert store.start("wait", identity, session_id="s", invocation_id="same-wait",
                       parent=parent_a, links=link_a) == first
    with pytest.raises(ValueError, match="Marker identity collision"):
        store.start("wait", identity, session_id="s", invocation_id="same-wait",
                    parent=parent_b, links=link_a)
    with pytest.raises(ValueError, match="Marker identity collision"):
        store.start("wait", identity, session_id="s", invocation_id="same-wait",
                    parent=parent_a, links=link_b)


def test_finish_retry_guarantee_ends_after_retention_purges_marker(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path, retention=RetentionPolicy(rotation_bytes=1, archive_cap_bytes=1))
    old = store.start("skill", AssetIdentity("skills", "purged"),
                      session_id="s", invocation_id="old", timestamp=100)
    assert store.finish(old["trace_id"], old["span_id"], timestamp=200)["recorded"]
    store.start("skill", AssetIdentity("skills", "new"), session_id="s",
                invocation_id="new", timestamp=300)

    retried = store.finish(old["trace_id"], old["span_id"], timestamp=400)
    assert not retried["recorded"] and retried["missing_start"]


def test_archive_retry_falls_back_when_sidecar_index_is_corrupt(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path, retention=RetentionPolicy(rotation_bytes=1,
                                                          archive_cap_bytes=1024 * 1024))
    identity = AssetIdentity("skills", "fallback")
    started = store.start("skill", identity, session_id="s", invocation_id="i", timestamp=100)
    store.finish(started["trace_id"], started["span_id"], outcome="ok", timestamp=200)
    store.start("skill", AssetIdentity("skills", "rotate"), session_id="s",
                invocation_id="rotate", timestamp=300)

    index_path = path.with_name(path.name + ".archive-index.sqlite3")
    index_path.write_bytes(b"corrupt index")

    retried = store.start("skill", identity, session_id="s", invocation_id="i", timestamp=400)

    assert retried == started


def test_parent_associations_and_filtered_parent_flag(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path)
    agent = store.start("agent", AssetIdentity("agents", "planner"), session_id="s", invocation_id="a")
    child = store.start("skill", AssetIdentity("skills", "lookup"), session_id="s", invocation_id="b",
                        parent=agent)
    child_record = next(record for record in rows(path) if record.span_id == child["span_id"])
    assert child_record.parent_span_id == agent["span_id"]
    assert child_record.associations[0].kind == "agent"
    assert child_record.associations[0].identity.key == "agents:planner"
    assert child_record.associations[0].measured

    missing = store.start("skill", AssetIdentity("skills", "orphan"), session_id="s", invocation_id="c",
                          parent=SessionStore.handle("s", "missing"))
    assert missing["missing_parent"] and not missing["recorded"]
    assert all(record.span_id != missing["span_id"] for record in rows(path))


def test_opted_in_skill_context_parent_is_exact_and_linked(tmp_path):
    path = tmp_path / "markers.jsonl"
    workflow_identity = AssetIdentity("caller", "observability-context")
    skill_identity = AssetIdentity("personal", "ps-ascii-diagrams")
    policy = CollectionPolicy(include=(
        Selector("workflow", workflow_identity.key),
        Selector("skill", skill_identity.key),
    ))
    store = SessionStore(path, policy)
    context = store.start("workflow", workflow_identity, session_id="task", invocation_id="context")
    skill = store.start("skill", skill_identity, session_id="task", invocation_id="skill", parent=context)
    store.finish(skill["trace_id"], skill["span_id"], outcome="ok")
    store.finish(context["trace_id"], context["span_id"], outcome="ok")

    records = rows(path)
    assert len(records) == 2
    context_record = next(record for record in records if record.kind == "workflow")
    skill_record = next(record for record in records if record.kind == "skill")
    assert skill_record.trace_id == context_record.trace_id
    assert skill_record.parent_span_id == context_record.span_id
    selected = policy.select_records(records)
    report = summarize(selected)
    assert report["root_workflows"] == 1
    assert report["orphan_parent_links"] == 0


def test_excluded_context_parent_blocks_selected_skill_child(tmp_path):
    path = tmp_path / "markers.jsonl"
    workflow_identity = AssetIdentity("caller", "observability-context")
    skill_identity = AssetIdentity("personal", "ps-ascii-diagrams")
    policy = CollectionPolicy(
        include=(Selector("workflow", workflow_identity.key), Selector("skill", skill_identity.key)),
        exclude=(Selector("workflow", workflow_identity.key),),
    )
    store = SessionStore(path, policy)
    context = store.start("workflow", workflow_identity, session_id="task", invocation_id="context")
    skill = store.start("skill", skill_identity, session_id="task", invocation_id="skill", parent=context)

    assert context["filtered"] and not context["recorded"]
    assert skill["missing_parent"] and not skill["recorded"]
    assert rows(path) == []


def test_policy_exclusions_and_disabled_mode(tmp_path):
    path = tmp_path / "never-created.jsonl"
    disabled = SessionStore(path, CollectionPolicy(enabled=False))
    result = disabled.start("skill", AssetIdentity("skills", "excluded"), session_id="s", invocation_id="i")
    assert result["filtered"] and not result["recorded"]
    assert not path.exists()

    policy = CollectionPolicy(exclude=(Selector("skill", "skills:private"),))
    store = SessionStore(path, policy)
    excluded = store.start("skill", AssetIdentity("skills", "private"), session_id="s", invocation_id="x")
    included = store.start("skill", AssetIdentity("skills", "public"), session_id="s", invocation_id="y")
    assert excluded["filtered"] and not excluded["recorded"]
    assert included["recorded"]
    assert len(rows(path)) == 1


def test_missing_finish_does_not_fabricate_record_and_open_start_stays_open(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path)
    absent = SessionStore.handle("s", "missing")
    assert store.finish(absent["trace_id"], absent["span_id"]) == {
        "recorded": False, "missing_start": True
    }
    opened = store.start("skill", AssetIdentity("skills", "open"), session_id="s", invocation_id="open")
    record, = rows(path)
    assert record.span_id == opened["span_id"]
    assert record.end_ns is None and record.lifecycle == "invoked"
    assert record.outcome == "unknown" and record.duration_quality == "unavailable"


def test_finish_without_outcome_does_not_assume_success(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path)
    started = store.start("skill", AssetIdentity("skills", "unknown"), session_id="s", invocation_id="i",
                          timestamp=100)
    store.finish(started["trace_id"], started["span_id"], timestamp=200)
    record, = rows(path)
    assert record.outcome == "unknown"
    assert record.lifecycle == "completed"


def test_separate_process_start_and_finish(tmp_path):
    path = tmp_path / "markers.jsonl"
    root = str(Path(__file__).parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": root}
    start_code = (
        "from workflow_ledger.sessions import SessionStore; "
        "from workflow_ledger.model import AssetIdentity; "
        "import json; print(json.dumps(SessionStore(%r).start('agent', AssetIdentity('agents','worker'), "
        "session_id='separate-session', invocation_id='separate-call', timestamp=100)))" % str(path)
    )
    result = subprocess.run([sys.executable, "-c", start_code], env=env, check=True,
                            capture_output=True, text=True)
    handle = json.loads(result.stdout)
    finish_code = (
        "from workflow_ledger.sessions import SessionStore; "
        "import json; print(json.dumps(SessionStore(%r).finish(%r,%r,timestamp=200)))"
        % (str(path), handle["trace_id"], handle["span_id"])
    )
    completed = subprocess.run([sys.executable, "-c", finish_code], env=env, check=True,
                               capture_output=True, text=True)
    assert json.loads(completed.stdout)["recorded"]
    record, = rows(path)
    assert record.lifecycle == "completed" and record.end_ns == 200


def test_concurrent_processes_append_without_lost_records(tmp_path):
    path = tmp_path / "markers.jsonl"
    root = str(Path(__file__).parents[1] / "src")
    env = {**os.environ, "PYTHONPATH": root}
    code = (
        "from workflow_ledger.sessions import SessionStore; "
        "from workflow_ledger.model import AssetIdentity; "
        "SessionStore(%r).start('skill', AssetIdentity('skills','worker'), "
        "session_id='concurrent', invocation_id=%r)"
    )
    processes = [subprocess.Popen([sys.executable, "-c", code % (str(path), f"call-{n}")], env=env)
                 for n in range(12)]
    assert all(process.wait() == 0 for process in processes)
    records = rows(path)
    assert len(records) == 12
    assert len({record.span_id for record in records}) == 12


def test_marker_kinds_inherit_measured_parent_identity_and_secure_store_mode(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path)
    parent = store.start("skill", AssetIdentity("skills", "parent", provenance="native"),
                         session_id="s", invocation_id="parent", evidence="native")
    child = store.start("tool", AssetIdentity("tools", "lookup"), session_id="s", invocation_id="child",
                        parent=parent)
    child_record = next(record for record in rows(path) if record.span_id == child["span_id"])
    association = child_record.associations[0]
    assert association.kind == "skill"
    assert association.identity.key == "skills:parent"
    assert association.evidence == "native" and association.measured
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_filtered_existing_start_does_not_finish(tmp_path):
    path = tmp_path / "markers.jsonl"
    identity = AssetIdentity("skills", "private")
    started = SessionStore(path).start("skill", identity, session_id="s", invocation_id="i")
    excluding = SessionStore(path, CollectionPolicy(exclude=(Selector("skill", identity.key),)))
    result = excluding.finish(started["trace_id"], started["span_id"])
    assert result["filtered"] and not result["recorded"]
    assert rows(path)[0].end_ns is None


def test_cli_disabled_skips_missing_policy_and_cli_start_finish(tmp_path):
    repo = Path(__file__).parents[1]
    root = str(repo / "src")
    env = {**os.environ, "PYTHONPATH": root}
    absent_policy = tmp_path / "missing-policy.json"
    path = tmp_path / "cli.jsonl"
    disabled = subprocess.run(
        [sys.executable, "-m", "workflow_ledger.sessions", "--disabled", "--policy", str(absent_policy),
         "--store", str(path), "start", "--session", "raw-session", "--kind", "skill",
         "--identity", "skills:demo"], env=env, check=True, capture_output=True, text=True,
    )
    assert json.loads(disabled.stdout) == {"disabled": True}
    assert not absent_policy.exists() and not path.exists()

    started = subprocess.run(
        [sys.executable, "-m", "workflow_ledger.sessions", "--store", str(path), "start",
         "--session", "raw-session-secret", "--invocation", "raw-invocation-secret", "--kind", "skill",
         "--identity", "skills:demo"], env=env, check=True, capture_output=True, text=True,
    )
    handle = json.loads(started.stdout)
    assert "raw-session-secret" not in started.stdout and "raw-invocation-secret" not in started.stdout
    finished = subprocess.run(
        [sys.executable, "-m", "workflow_ledger.sessions", "--store", str(path), "finish", "--handle",
         f"{handle['trace_id']}:{handle['span_id']}", "--outcome", "ok"], env=env, check=True,
        capture_output=True, text=True,
    )
    assert json.loads(finished.stdout)["recorded"]
    workflow = subprocess.run(
        [sys.executable, "-m", "workflow_ledger.sessions", "--store", str(path), "start",
         "--session", "raw-workflow-session", "--invocation", "workflow-invocation", "--kind", "workflow",
         "--identity", "caller:observability-context"], env=env, check=True, capture_output=True, text=True,
    )
    assert json.loads(workflow.stdout)["recorded"]
    assert "raw-workflow-session" not in workflow.stdout
    target = SessionStore(path).start("agent", AssetIdentity("agents", "target"),
                                     session_id="dependency-session", invocation_id="target")
    waiter = SessionStore(path).start("agent", AssetIdentity("agents", "waiter"),
                                      session_id="dependency-session", invocation_id="waiter")
    wait = subprocess.run(
        [sys.executable, "-m", "workflow_ledger.sessions", "--store", str(path), "start",
         "--session", "dependency-session", "--invocation", "wait-1", "--kind", "wait",
         "--identity", "caller:dependency-wait", "--parent",
         f"{waiter['trace_id']}:{waiter['span_id']}", "--waits-for",
         f"{target['trace_id']}:{target['span_id']}"], env=env, check=True, capture_output=True, text=True,
    )
    assert json.loads(wait.stdout)["recorded"]
    wait_record = next(record for record in rows(path) if record.kind == "wait")
    assert wait_record.parent_span_id == waiter["span_id"]
    assert wait_record.links == (SpanLink("waits_for", target["trace_id"], target["span_id"]),)
    invalid = subprocess.run(
        [sys.executable, "-m", "workflow_ledger.sessions", "--store", str(path), "start",
         "--session", "secret-session", "--kind", "secret-kind", "--identity", "skills:demo"],
        env=env, capture_output=True, text=True,
    )
    assert invalid.returncode == 2
    assert "secret-session" not in invalid.stdout + invalid.stderr
    assert "secret-kind" not in invalid.stdout + invalid.stderr
    assert json.loads(invalid.stdout) == {"error": "marker_arguments_invalid"}


def test_caller_declared_child_of_filtered_parent_is_not_written(tmp_path):
    path = tmp_path / "markers.jsonl"
    policy = CollectionPolicy(exclude=(Selector("agent", "agents:private"),))
    store = SessionStore(path, policy)
    parent = store.start("agent", AssetIdentity("agents", "private"), session_id="s", invocation_id="parent")
    assert parent["filtered"]
    child = store.start("skill", AssetIdentity("skills", "leaked"), session_id="s", invocation_id="child",
                        parent=parent)
    assert child["missing_parent"] and not child["recorded"]
    assert rows(path) == []


def test_native_orphan_can_record_with_explicit_workflow_association(tmp_path):
    path = tmp_path / "markers.jsonl"
    workflow = AssetIdentity("codex-workflow", "session", provenance="native")
    association = Association("workflow", workflow, state="invoked", evidence="native",
                              invocation_span_id=SessionStore.handle("s", "session")["span_id"],
                              invocation_trace_id=SessionStore.handle("s", "session")["trace_id"])
    tool = SessionStore(path).start("tool", AssetIdentity("codex-tool", "search", provenance="native"),
                                   session_id="s", invocation_id="tool-1",
                                   parent=SessionStore.handle("s", "session"),
                                   associations=(association,), evidence="native")
    assert tool["recorded"]
    record, = rows(path)
    assert record.filtered_parent is False
    assert record.associations[0].measured and record.associations[0].identity == workflow


def test_agent_start_replaces_all_inherited_agent_associations(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path)
    outer = store.start("agent", AssetIdentity("agents", "outer"), session_id="s", invocation_id="outer")
    inner = store.start("agent", AssetIdentity("agents", "inner"), session_id="s", invocation_id="inner",
                        parent=outer)
    descendant = store.start("skill", AssetIdentity("skills", "child"), session_id="s", invocation_id="child",
                             parent=inner)
    record = next(item for item in rows(path) if item.span_id == descendant["span_id"])
    agents = [association for association in record.associations if association.kind == "agent"]
    assert [association.identity.key for association in agents] == ["agents:inner"]


def test_identity_collision_and_bounded_lock_timeout(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path)
    store.start("skill", AssetIdentity("skills", "first"), session_id="s", invocation_id="same")
    try:
        store.start("skill", AssetIdentity("skills", "second"), session_id="s", invocation_id="same")
    except ValueError as error:
        assert str(error) == "Marker identity collision"
    else:
        raise AssertionError("Expected identity collision")

    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        store.LOCK_TIMEOUT_SECONDS = 0.03
        result = store.start("skill", AssetIdentity("skills", "timeout"), session_id="s", invocation_id="timeout")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    assert not result["recorded"]
    assert store.diagnostics["lock_timeouts"] == 1
    assert store.last_storage_error == "lock_timeout"


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
def test_cli_failed_start_can_be_retried_with_same_handle_and_timestamp(
        tmp_path, monkeypatch, capsys, error_number):
    path = tmp_path / "markers.jsonl"
    original_open = sessions_module.os.open
    timestamp = 123456789
    session = "private-session"
    invocation = "private-invocation"

    def denied_append(file_path, flags, *args, **kwargs):
        if Path(file_path) == path and flags & os.O_APPEND:
            raise OSError(error_number, "private exception detail")
        return original_open(file_path, flags, *args, **kwargs)

    monkeypatch.setattr(sessions_module.os, "open", denied_append)
    result = marker_main([
        "--store", str(path), "start", "--session", session,
        "--invocation", invocation, "--kind", "skill", "--identity", "skills:demo",
        "--timestamp-ns", str(timestamp),
    ])
    failed = json.loads(capsys.readouterr().out)
    assert result == 1
    assert failed["storage_error"] == "permission_denied"
    assert failed["timestamp_ns"] == timestamp
    assert failed["trace_id"] and failed["span_id"]
    assert session not in json.dumps(failed) and invocation not in json.dumps(failed)
    assert str(path) not in json.dumps(failed)
    assert "private exception detail" not in json.dumps(failed)
    assert not path.exists()

    monkeypatch.setattr(sessions_module.os, "open", original_open)
    args = [
        "--store", str(path), "start", "--retry-handle",
        f"{failed['trace_id']}:{failed['span_id']}", "--kind", "skill",
        "--identity", "skills:demo", "--timestamp-ns", str(timestamp),
    ]
    assert marker_main(args) == 0
    assert json.loads(capsys.readouterr().out)["recorded"]
    assert marker_main(args) == 0
    assert json.loads(capsys.readouterr().out)["recorded"]
    record, = rows(path)
    assert record.trace_id == failed["trace_id"]
    assert record.span_id == failed["span_id"]
    assert record.start_ns == timestamp
    assert len(path.read_text().splitlines()) == 1


def test_cli_failed_finish_is_not_mislabeled_and_can_be_retried(tmp_path, monkeypatch, capsys):
    path = tmp_path / "markers.jsonl"
    start_timestamp = 100
    finish_timestamp = 200
    started = SessionStore(path).start(
        "skill", AssetIdentity("skills", "demo"), session_id="s", invocation_id="i",
        timestamp=start_timestamp,
    )
    original_open = sessions_module.os.open

    def denied_append(file_path, flags, *args, **kwargs):
        if Path(file_path) == path and flags & os.O_APPEND:
            raise PermissionError(errno.EACCES, "private exception detail")
        return original_open(file_path, flags, *args, **kwargs)

    monkeypatch.setattr(sessions_module.os, "open", denied_append)
    handle = f"{started['trace_id']}:{started['span_id']}"
    finish_args = [
        "--store", str(path), "finish", "--handle", handle,
        "--outcome", "ok", "--timestamp-ns", str(finish_timestamp),
    ]
    assert marker_main(finish_args) == 1
    failed = json.loads(capsys.readouterr().out)
    assert failed["storage_error"] == "permission_denied"
    assert failed["timestamp_ns"] == finish_timestamp
    assert failed["trace_id"] == started["trace_id"]
    assert failed["span_id"] == started["span_id"]
    assert "missing_start" not in failed
    assert "private exception detail" not in json.dumps(failed)
    assert str(path) not in json.dumps(failed)

    monkeypatch.setattr(sessions_module.os, "open", original_open)
    assert marker_main(finish_args) == 0
    recovered = json.loads(capsys.readouterr().out)
    assert recovered["recorded"] and not recovered["missing_start"]
    assert marker_main(finish_args) == 0
    assert json.loads(capsys.readouterr().out)["recorded"]
    record, = rows(path)
    assert record.start_ns == start_timestamp
    assert record.end_ns == finish_timestamp
    assert record.outcome == "ok"
    assert len(path.read_text().splitlines()) == 2


_STORAGE_FULL_ERRNOS = [errno.ENOSPC]
if hasattr(errno, "EDQUOT"):
    _STORAGE_FULL_ERRNOS.append(errno.EDQUOT)


@pytest.mark.parametrize(("error_number", "expected"), [
    *((error_number, "storage_full") for error_number in _STORAGE_FULL_ERRNOS),
    (errno.EIO, "storage_unavailable"),
])
def test_marker_cli_classifies_nonpermission_storage_errors(
        tmp_path, monkeypatch, capsys, error_number, expected):
    path = tmp_path / "markers.jsonl"
    original_open = sessions_module.os.open

    def failed_append(file_path, flags, *args, **kwargs):
        if Path(file_path) == path and flags & os.O_APPEND:
            raise OSError(error_number, "private exception detail")
        return original_open(file_path, flags, *args, **kwargs)

    monkeypatch.setattr(sessions_module.os, "open", failed_append)
    result = marker_main([
        "--store", str(path), "start", "--session", "s", "--invocation", "i",
        "--kind", "skill", "--identity", "skills:demo", "--timestamp-ns", "100",
    ])
    output = json.loads(capsys.readouterr().out)
    assert result == 1
    assert output["storage_error"] == expected
    assert "private exception detail" not in json.dumps(output)
