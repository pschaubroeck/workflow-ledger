import json
import tracemalloc

from workflow_ledger.adapters.codex import import_codex
from workflow_ledger.model import stable_id
from workflow_ledger.policy import CollectionPolicy, Selector
from workflow_ledger.report import summarize
from workflow_ledger.storage import JsonlStore


def fixture(path, snapshots, extra=None):
    rows = [{"type": "session_meta", "timestamp": "2026-10-06T12:00:00Z",
             "payload": {"id": "native-session", "cwd": "PRIVATE_PATH", "cli_version": "test"}},
            {"type": "response_item", "payload": {"type": "message", "content": "PRIVATE_PROMPT"}}]
    for index, tokens in enumerate(snapshots):
        rows.append({"type": "event_msg", "timestamp": f"2026-10-06T12:00:{index + 1:02d}Z",
                     "payload": {"type": "token_count", "info": {
                         "total_token_usage": tokens, "last_token_usage": tokens}}})
    if extra:
        rows += extra
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_cumulative_snapshots_not_added_and_no_false_attribution(tmp_path):
    source = tmp_path / "source.jsonl"
    fixture(source, [{"input_tokens": 10, "output_tokens": 5},
                     {"input_tokens": 10, "output_tokens": 5},
                     {"input_tokens": 30, "output_tokens": 10}])
    result = import_codex(source)
    assert result.diagnostics["duplicate_snapshots"] == 1
    row = result.records[0]
    assert row.usage.input_tokens == 30
    assert row.usage.reasoning_tokens is None
    assert row.agent_name is None and row.skill_name is None and row.model is None
    assert row.outcome == "unknown" and row.duration_quality == "inferred"
    assert row.lifecycle == "observed" and row.identity.version is None
    assert "PRIVATE" not in json.dumps(row.to_dict())
    store = JsonlStore(tmp_path / "store.jsonl")
    for _ in range(2):
        for record in import_codex(source).records:
            assert store.emit(record)
    assert len(store.read()) == 1
    assert summarize(store.read())["groups"][0]["tokens"]["input_tokens"]["observed"] == 30


def test_reset_and_invalid_subsets_do_not_become_zero(tmp_path):
    source = tmp_path / "source.jsonl"
    fixture(source, [{"input_tokens": 20, "output_tokens": 5},
                     {"input_tokens": 10, "output_tokens": 8, "reasoning_output_tokens": 9}])
    result = import_codex(source)
    assert result.records[0].usage.input_tokens is None
    assert result.records[0].usage.output_tokens == 8
    assert result.records[0].usage.reasoning_tokens is None
    assert result.diagnostics["counter_resets"] == 1


def test_truncated_unknown_and_unsupported_shapes_are_diagnosed(tmp_path):
    source = tmp_path / "source.jsonl"
    fixture(source, [], [{"type": "future_event", "payload": {}},
                         {"type": "event_msg", "payload": {
                             "type": "token_count", "info": {"usage_v2": 100}}}])
    with source.open("a") as handle:
        handle.write('{"truncated":')
    result = import_codex(source)
    assert result.records[0].usage is None
    assert result.diagnostics["malformed_records"] == 1
    assert result.diagnostics["unsupported_records"] == 2


def test_missing_source_and_missing_session_are_explicit(tmp_path):
    assert import_codex(tmp_path / "absent").diagnostics["source_errors"] == 1
    source = tmp_path / "empty"
    source.write_text("")
    result = import_codex(source)
    assert not result.records and result.diagnostics["missing_session_id"] == 1


def test_counter_reset_across_missing_fields_is_still_unknown(tmp_path):
    source = tmp_path / "source.jsonl"
    fixture(source, [{"input_tokens": 20}, {"output_tokens": 5}, {"input_tokens": 10}])
    result = import_codex(source)
    assert result.records[0].usage.input_tokens is None
    assert result.diagnostics["counter_resets"] == 1


def test_timestamp_extrema_and_invalid_token_fields_are_preserved(tmp_path):
    source = tmp_path / "source.jsonl"
    rows = [
        {"type": "session_meta", "timestamp": "2026-10-06T12:00:02Z",
         "payload": {"id": "native-session"}},
        {"type": "event_msg", "timestamp": "not-a-timestamp",
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"input_tokens": -1, "cached_input_tokens": 12,
                                   "output_tokens": 20, "reasoning_output_tokens": 3}}}},
        {"type": "response_item", "timestamp": "2026-10-06T12:00:01Z",
         "payload": {"type": "message"}},
    ]
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    result = import_codex(source)

    record = result.records[0]
    assert record.start_ns == 1791288001000000000
    assert record.end_ns == 1791288002000000000
    assert record.usage.input_tokens is None
    assert record.usage.cached_input_tokens == 12
    assert record.usage.output_tokens == 20
    assert result.diagnostics["invalid_token_fields"] == 1


def test_multiple_sessions_remain_unsupported(tmp_path):
    source = tmp_path / "source.jsonl"
    rows = [
        {"type": "session_meta", "payload": {"id": "first"}},
        {"type": "session_meta", "payload": {"id": "second"}},
    ]
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    result = import_codex(source)

    assert not result.records
    assert result.diagnostics["unsupported_records"] == 1


def test_large_rollout_aggregation_memory_is_constant_size(tmp_path):
    def make_rollout(path, snapshot_count):
        with path.open("w") as handle:
            handle.write(json.dumps({"type": "session_meta", "timestamp": "2026-10-06T12:00:00Z",
                                    "payload": {"id": "native-session"}}) + "\n")
            for index in range(snapshot_count):
                handle.write(json.dumps({
                    "type": "event_msg", "timestamp": "2026-10-06T12:00:01Z",
                    "payload": {"type": "token_count", "info": {
                        "total_token_usage": {"input_tokens": index, "output_tokens": 1}}},
                }) + "\n")

    def peak_import_memory(path):
        tracemalloc.start()
        try:
            result = import_codex(path)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert len(result.records) == 1
        return peak

    small = tmp_path / "small.jsonl"
    large = tmp_path / "large.jsonl"
    make_rollout(small, 100)
    make_rollout(large, 20_000)

    small_peak = peak_import_memory(small)
    large_peak = peak_import_memory(large)

    # The larger input has 200x more rows. An importer retaining timestamp or
    # snapshot lists grows by several MiB; incremental aggregation stays bounded.
    assert large_peak < small_peak + 1_000_000


def test_codex_policy_short_circuits_before_open_and_filters_root(tmp_path, monkeypatch):
    class NeverOpen:
        def __fspath__(self):
            raise AssertionError("disabled source was accessed")

    disabled = CollectionPolicy(enabled=False)
    result = import_codex(NeverOpen(), policy=disabled)
    assert not result.records and result.diagnostics["disabled"] == 1

    source = tmp_path / "selected-out.jsonl"
    fixture(source, [{"input_tokens": 12}])
    excluded = CollectionPolicy(exclude=(Selector("runtime", "codex"),))
    result = import_codex(NeverOpen(), policy=excluded)
    assert not result.records and result.diagnostics["disabled"] == 1

    fixture(source, [{"input_tokens": 12}])
    identity = f"codex:{stable_id('codex/session-asset', 'native-session')}"
    filtered = import_codex(source, policy=CollectionPolicy(include=(Selector("workflow", identity),)))
    assert len(filtered.records) == 1
    excluded_identity = f"codex:{stable_id('codex/session-asset', 'other-session')}"
    filtered = import_codex(source, policy=CollectionPolicy(include=(Selector("workflow", excluded_identity),)))
    assert not filtered.records and filtered.diagnostics["filtered_records"] == 1
    assert filtered.diagnostics["source_errors"] == 0
