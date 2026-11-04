import asyncio
from dataclasses import replace
import json
from typing import get_args, get_type_hints

import pytest

from workflow_ledger import (
    AssetIdentity, Association, JsonlStore, Kind, Recorder, SpanLink, SpanRecord, TokenUsage,
)
from workflow_ledger.report import summarize


def record(**overrides):
    values = dict(trace_id="1" * 32, span_id="2" * 16, kind="workflow", name="run",
                  harness="test", adapter="test", start_ns=1, end_ns=1_000_000_001,
                  duration_quality="direct")
    values.update(overrides)
    return SpanRecord(**values)


def test_supported_span_kinds_are_public_and_typed():
    assert set(get_args(Kind)) == {"workflow", "agent", "skill", "model", "tool", "wait"}
    assert get_type_hints(Recorder.span)["kind"] == Kind
    assert get_type_hints(Recorder.observe)["kind"] == Kind


def test_scope_and_subset_contract():
    with pytest.raises(ValueError):
        TokenUsage(input_tokens=10, cached_input_tokens=11)
    with pytest.raises(ValueError):
        TokenUsage(input_tokens=True)
    with pytest.raises(ValueError, match="token source"):
        TokenUsage(input_tokens=1, source="caller-provided free text")
    with pytest.raises(ValueError, match="token source"):
        TokenUsage(input_tokens=1, source=[])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        record(kind="skill", usage=TokenUsage(input_tokens=10))
    with pytest.raises(ValueError):
        record(parent_span_id="3" * 16, usage=TokenUsage(scope="run"))
    with pytest.raises(TypeError):
        SpanRecord.from_dict({**record().to_dict(), "prompt": "private"})


def test_recorder_emits_harness_neutral_wait_span_with_explicit_target_link(tmp_path):
    store = JsonlStore(tmp_path / "wait.jsonl")
    recorder = Recorder(store, harness="arbitrary-harness")
    with recorder.span("workflow", "run"):
        with recorder.span("agent", "dispatcher"):
            with recorder.span("agent", "target") as target:
                assert target.trace_id and target.span_id
            target_link = SpanLink("waits_for", target.trace_id, target.span_id)
            with recorder.span("wait", "dependency", links=(target_link,)):
                pass

    rows = store.read()
    wait = next(item for item in rows if item.kind == "wait")
    waiter = next(item for item in rows if item.kind == "agent")
    assert wait.harness == "arbitrary-harness"
    assert wait.parent_span_id == waiter.span_id
    assert wait.links == (target_link,)
    assert SpanRecord.from_dict(wait.to_dict()) == wait


def test_nested_parallel_context_and_error_content(tmp_path):
    store = JsonlStore(tmp_path / "records.jsonl")
    recorder = Recorder(store)

    async def child(name):
        with recorder.span("tool", name):
            await asyncio.sleep(0)

    async def run_children():
        await asyncio.gather(child("first"), child("second"))

    with recorder.span("workflow", "run"):
        with recorder.span("agent", "agent"):
            with recorder.span("skill", "skill", skill_version="1"):
                asyncio.run(run_children())
                with pytest.raises(ValueError, match="PRIVATE_SENTINEL"):
                    with recorder.span("tool", "failed"):
                        raise ValueError("PRIVATE_SENTINEL")
                with recorder.span("model", "call") as activity:
                    activity.usage = TokenUsage(input_tokens=10, output_tokens=5)
    rows = store.read()
    skill = next(r for r in rows if r.kind == "skill")
    children = [r for r in rows if r.kind in {"tool", "model"}]
    assert len({r.trace_id for r in rows}) == 1
    assert all(r.parent_span_id == skill.span_id for r in children)
    assert all(r.agent_name == "agent" and r.skill_name == "skill" for r in children)
    assert next(r for r in rows if r.name == "failed").error_type == "ValueError"
    assert "PRIVATE_SENTINEL" not in store.path.read_text()
    assert summarize(rows)["groups"][0]["tokens"]["input_tokens"]["observed"] == 10


def test_cancellation_is_distinct_and_propagates(tmp_path):
    store = JsonlStore(tmp_path / "cancel.jsonl")
    recorder = Recorder(store)
    with pytest.raises(asyncio.CancelledError):
        with recorder.span("workflow", "run"):
            raise asyncio.CancelledError()
    assert store.read()[0].outcome == "cancelled"


def test_disabled_and_failed_observer_do_not_change_workflow():
    class BrokenSink:
        def emit(self, record):
            raise OSError("private storage path")

    recorder = Recorder(BrokenSink())
    with recorder.span("workflow", "run"):
        pass
    assert recorder.diagnostics["delivery_errors"] == 2
    disabled = Recorder(BrokenSink(), enabled=False)
    with disabled.span("workflow", "run"):
        pass
    assert disabled.diagnostics["delivery_errors"] == 0


def test_store_idempotence_revisions_and_truncated_tail(tmp_path):
    store = JsonlStore(tmp_path / "records.jsonl")
    original = record(usage=TokenUsage(scope="run", input_tokens=10))
    assert store.emit(original)
    assert store.emit(original)
    assert len(store.path.read_text().splitlines()) == 1
    revised = replace(original, usage=TokenUsage(scope="run", input_tokens=20))
    assert store.emit(revised)
    assert store.read() == [revised]
    with store.path.open("a") as handle:
        handle.write('{"truncated":')
    reopened = JsonlStore(store.path)
    assert reopened.emit(record(span_id="3" * 16))
    assert len(reopened.read()) == 2
    assert reopened.diagnostics["malformed_records"] == 1


def test_run_totals_override_calls_per_field_and_unknowns_stay_unknown():
    root = record(usage=TokenUsage(scope="run", input_tokens=50))
    model = record(kind="model", span_id="3" * 16, parent_span_id=root.span_id,
                   agent_name="worker", skill_name="skill", associations=(Association(
                       "skill", AssetIdentity("caller", "skill"), "invoked", "caller_declared",
                       "4" * 16, root.trace_id),), usage=TokenUsage(
                       input_tokens=40, output_tokens=10, cached_input_tokens=5))
    group = summarize([root, model])["groups"][0]
    assert group["tokens"]["input_tokens"]["observed"] == 50
    assert group["tokens"]["output_tokens"]["observed"] == 10
    assert group["tokens"]["reasoning_tokens"]["observed"] is None
    assert group["tokens"]["reasoning_tokens"]["unknown_observations"] == 1
    by_skill = {g["group"]: g for g in summarize([root, model], "skill")["groups"]}
    assert by_skill["unattributed"]["tokens"]["input_tokens"]["observed"] == 50
    skill_group = next(v for k, v in by_skill.items() if k.startswith("caller:skill"))
    assert skill_group["tokens"]["input_tokens"]["observed"] is None
    assert skill_group["tokens"]["output_tokens"]["observed"] == 10
    assert group["duration_seconds"]["sum_operation_time"] == 2


def test_partial_capture_exposes_orphan_and_duplicate_records():
    model = record(kind="model", parent_span_id="4" * 16)
    result = summarize([model, model])
    assert result["records"] == 1
    assert result["orphan_parent_links"] == 1
    assert result["groups"][0]["tokens"]["input_tokens"]["observed"] is None


def test_store_failure_is_counted(tmp_path):
    store = JsonlStore(tmp_path)
    assert not store.emit(record())
    assert store.diagnostics["storage_errors"] == 1


def test_metadata_contains_no_arbitrary_payload_fields():
    serialized = json.dumps(record().to_dict())
    assert "prompt" not in serialized and "arguments" not in serialized
