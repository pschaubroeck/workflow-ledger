from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
from pathlib import Path
import stat

from workflow_ledger import JsonlStore, SpanRecord
import workflow_ledger.storage as storage_module


def record(number=1):
    return SpanRecord(trace_id="1" * 32, span_id=f"{number:016x}", kind="workflow",
                      name="synthetic", harness="test", adapter="test")


def test_append_preparation_runs_once_until_explicit_refresh(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "records.jsonl"
    original_mkdir = Path.mkdir
    calls = []

    def counted_mkdir(self, *args, **kwargs):
        if self == path.parent:
            calls.append(self)
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", counted_mkdir)
    store = JsonlStore(path)
    assert all(store.emit(record(i)) for i in range(1, 5))
    assert len(calls) == 1
    assert len(path.read_text().splitlines()) == 4
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.read() == [record(i) for i in range(1, 5)]
    assert store.emit(record(5))
    assert len(calls) == 2


def test_existing_tail_is_checked_once_and_recovered(tmp_path, monkeypatch):
    path = tmp_path / "records.jsonl"
    path.write_text(json.dumps(record().to_dict()) + '\n{"truncated":', encoding="utf-8")
    original_open = Path.open
    tail_reads = []

    def counted_open(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == path and mode == "rb":
            tail_reads.append(self)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    store = JsonlStore(path)
    assert store.emit(record(2)) and store.emit(record(3))
    assert len(tail_reads) == 1
    assert store.read() == [record(1), record(2), record(3)]
    assert store.diagnostics["malformed_records"] == 1


def test_partial_write_failure_rechecks_tail_before_retry(tmp_path, monkeypatch):
    store = JsonlStore(tmp_path / "records.jsonl")
    original_fdopen = storage_module.os.fdopen
    failing = True

    @contextmanager
    def fail_once(descriptor, *args, **kwargs):
        nonlocal failing
        with original_fdopen(descriptor, *args, **kwargs) as handle:
            if failing:
                failing = False

                class PartialWriter:
                    def write(self, value):
                        handle.write(value[:13])
                        handle.flush()
                        raise OSError("synthetic partial write")

                yield PartialWriter()
            else:
                yield handle

    monkeypatch.setattr(storage_module.os, "fdopen", fail_once)
    assert not store.emit(record())
    assert store.diagnostics["storage_errors"] == 1
    assert store.emit(record())
    assert store.read() == [record()]
    assert store.diagnostics["malformed_records"] == 1


def test_refresh_detects_external_tail_change_before_resuming(tmp_path):
    store = JsonlStore(tmp_path / "records.jsonl")
    assert store.emit(record())
    with store.path.open("a", encoding="utf-8") as handle:
        handle.write('{"external_partial":')
    store.read()
    assert store.emit(record(2))
    assert store.read() == [record(), record(2)]
    assert store.diagnostics["malformed_records"] == 1


def test_threads_share_one_prepared_writer_and_duplicate_cache(tmp_path):
    store = JsonlStore(tmp_path / "records.jsonl")
    rows = [record(i) for i in range(1, 21)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(store.emit, rows + rows))
    assert set(r.key for r in store.read()) == set(r.key for r in rows)
    assert len(store.path.read_text().splitlines()) == len(rows)
    assert store.diagnostics == {"malformed_records": 0, "storage_errors": 0}
