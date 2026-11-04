import json
from pathlib import Path
import stat
import sys
import types

import pytest

from workflow_ledger import AssetIdentity, JsonlStore, RetentionPolicy, SpanRecord
from workflow_ledger.cli import main as cli_main
import workflow_ledger.retention as retention_module
from workflow_ledger.retention import archive_paths
from workflow_ledger.sessions import SessionStore


def record(number: int) -> SpanRecord:
    return SpanRecord(trace_id=f"{number:032x}", span_id=f"{number:016x}", kind="workflow",
                      name=f"synthetic-{number}", harness="test", adapter="test")


def test_retention_defaults_and_validation(tmp_path, monkeypatch):
    assert RetentionPolicy().rotation_bytes == 5 * 1024 * 1024
    assert RetentionPolicy().archive_cap_bytes == 250 * 1024 * 1024
    with pytest.raises(ValueError):
        RetentionPolicy(rotation_bytes=0)
    with pytest.raises(ValueError):
        RetentionPolicy(rotation_bytes=10, archive_cap_bytes=9)

    settings = tmp_path / "retention.json"
    settings.write_text(json.dumps({"rotation_bytes": 1234, "archive_cap_bytes": 5678,
                                    "enabled": True}), encoding="utf-8")
    assert RetentionPolicy.load(settings) == RetentionPolicy(1234, 5678, True)
    monkeypatch.setenv("WORKFLOW_LEDGER_RETENTION_POLICY", str(settings))
    assert JsonlStore(tmp_path / "env.jsonl").retention == RetentionPolicy(1234, 5678, True)
    assert SessionStore(tmp_path / "markers.jsonl").retention == RetentionPolicy(1234, 5678, True)


def test_jsonl_store_rotates_reads_archives_and_preserves_private_modes(tmp_path):
    path = tmp_path / "records.jsonl"
    store = JsonlStore(path, retention=RetentionPolicy(rotation_bytes=500, archive_cap_bytes=10_000))
    for number in range(1, 6):
        assert store.emit(record(number))

    archives = archive_paths(path)
    assert archives
    assert all(stat.S_IMODE(archive.stat().st_mode) == 0o600 for archive in archives)
    assert {item.key for item in store.read()} == {record(number).key for number in range(1, 6)}
    assert {item.key for item in store.read(include_archives=False)} == {
        record(5).key
    }


def test_rotation_purges_oldest_archives_to_configured_cap(tmp_path):
    path = tmp_path / "records.jsonl"
    policy = RetentionPolicy(rotation_bytes=500, archive_cap_bytes=1_000)
    store = JsonlStore(path, retention=policy)
    for number in range(1, 7):
        assert store.emit(record(number))

    archives = archive_paths(path)
    assert archives
    assert sum(item.stat().st_size for item in archives) <= policy.archive_cap_bytes
    retained = {item.key for item in store.read()}
    assert record(1).key not in retained
    assert record(6).key in retained
    assert path.exists()
    assert path.with_name(path.name + ".lock").exists()


def test_disabled_retention_keeps_one_active_store(tmp_path):
    path = tmp_path / "records.jsonl"
    store = JsonlStore(path, retention=RetentionPolicy(1, 1_000, enabled=False))
    for number in range(1, 5):
        assert store.emit(record(number))
    assert archive_paths(path) == []
    assert len(store.read()) == 4


def test_failed_rotation_recovers_cleanly_on_retry(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    store = JsonlStore(path, retention=RetentionPolicy(rotation_bytes=1,
                                                        archive_cap_bytes=10_000))
    assert store.emit(record(1))
    original_replace = retention_module.os.replace

    def fail_archive_move(source, destination):
        if Path(source) == path:
            raise OSError("synthetic rotation failure")
        return original_replace(source, destination)

    monkeypatch.setattr(retention_module.os, "replace", fail_archive_move)
    assert not store.emit(record(2))
    assert store.diagnostics["storage_errors"] == 1
    assert path.exists()

    monkeypatch.setattr(retention_module.os, "replace", original_replace)
    assert store.emit(record(2))
    assert {item.key for item in store.read()} == {record(1).key, record(2).key}
    assert not list(tmp_path.glob(".records.jsonl.rotation-*.tmp"))


def test_session_markers_keep_open_spans_available_across_rotation(tmp_path):
    path = tmp_path / "markers.jsonl"
    store = SessionStore(path, retention=RetentionPolicy(rotation_bytes=1,
                                                          archive_cap_bytes=10 * 1024 * 1024))
    workflow = store.start("workflow", AssetIdentity("caller", "context"),
                           session_id="session", invocation_id="context")
    skill = store.start("skill", AssetIdentity("personal", "sample"),
                        session_id="session", invocation_id="skill",
                        parent=workflow)
    assert store.finish(skill["trace_id"], skill["span_id"], outcome="ok")["recorded"]
    assert store.finish(workflow["trace_id"], workflow["span_id"], outcome="ok")["recorded"]

    latest = {item.key: item for item in JsonlStore(path).read()}
    assert len(latest) == 2
    assert latest[f"{workflow['trace_id']}/{workflow['span_id']}"].lifecycle == "completed"
    assert latest[f"{skill['trace_id']}/{skill['span_id']}"].parent_span_id == workflow["span_id"]
    assert latest[f"{skill['trace_id']}/{skill['span_id']}"].lifecycle == "completed"


def test_reports_include_archives_but_exports_need_explicit_archive_flag(
        tmp_path, monkeypatch, capsys):
    path = tmp_path / "records.jsonl"
    store = JsonlStore(path, retention=RetentionPolicy(rotation_bytes=500, archive_cap_bytes=10_000))
    for number in range(1, 5):
        assert store.emit(record(number))
    assert archive_paths(path)

    assert cli_main(["report", "--store", str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["records"] == 4

    module = types.ModuleType("workflow_ledger.otel")
    exports = []

    def export_records(records, **kwargs):
        exports.append([item.key for item in records])
        return {"exported_spans": len(exports[-1]), "export_errors": 0}

    module.export_records = export_records
    monkeypatch.setitem(sys.modules, "workflow_ledger.otel", module)
    assert cli_main(["export-otel", "--store", str(path), "--endpoint", "http://localhost"]) == 0
    capsys.readouterr()
    assert len(exports[-1]) == 1
    assert cli_main(["export-otel", "--store", str(path), "--endpoint", "http://localhost",
                     "--include-archives"]) == 0
    capsys.readouterr()
    assert len(exports[-1]) == 4
