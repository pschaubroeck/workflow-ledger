import asyncio
from dataclasses import replace
import json

import pytest

from workflow_ledger import (AssetIdentity, Association, CollectionPolicy, JsonlStore,
                                 Recorder, Selector, SpanRecord, TokenUsage)
from workflow_ledger.report import summarize


def identity(name, version=None):
    return AssetIdentity("custom", name, version)


def test_exact_selection_exclusions_and_filtered_ancestry(tmp_path):
    policy = CollectionPolicy(
        include=(Selector("skill", "custom:research"), Selector("agent", "custom:second")),
        exclude=(Selector("tool", "custom:private"), Selector("agent", "custom:excluded")),
    )
    store = JsonlStore(tmp_path / "records.jsonl")
    observer = Recorder(store, policy=policy)
    with observer.span("workflow", "root", identity=identity("root")):
        for agent in ("first", "second", "excluded", "unselected"):
            with observer.span("agent", agent, identity=identity(agent)):
                skill = "research" if agent in {"first", "excluded"} else "other"
                with observer.span("skill", skill, identity=identity(skill, "1")):
                    with observer.span("model", "generation") as call:
                        call.usage = TokenUsage(input_tokens=10)
                    with observer.span("tool", "private", identity=identity("private")):
                        with observer.span("model", "private-model") as call:
                            call.usage = TokenUsage(input_tokens=100)
    rows = store.read()
    assert not any(r.identity.logical_id in {"root", "first", "excluded", "unselected", "private", "private-model"}
                   for r in rows)
    assert sum(r.kind == "model" for r in rows) == 2
    assert any(r.filtered_parent for r in rows)
    assert summarize(rows)["groups"][0]["tokens"]["input_tokens"]["observed"] == 20
    assert observer.diagnostics["filtered_records"] > 0
    assert "private" not in store.path.read_text()


def test_loaded_skill_is_context_not_execution_or_usage(tmp_path):
    store = JsonlStore(tmp_path / "records.jsonl")
    observer = Recorder(store)
    loaded = identity("loaded-only")
    with observer.span("workflow", "root"):
        observer.observe("skill", loaded, "configured")
        observer.observe("skill", loaded, "loaded")
        with observer.associate(Association("skill", loaded)):
            with observer.span("model", "generation") as call:
                call.usage = TokenUsage(input_tokens=7)
    rows = store.read()
    loading = [r for r in rows if r.lifecycle in {"configured", "loaded"}]
    assert len(loading) == 2
    assert all(r.usage is None and r.duration_quality == "unavailable" for r in loading)
    groups = summarize(rows, "skill")["groups"]
    assert sum((g["tokens"]["input_tokens"]["observed"] or 0) for g in groups
               if g["group"] != "unattributed") == 0


def test_loading_a_selected_skill_does_not_select_unrelated_children(tmp_path):
    policy = CollectionPolicy(include=(Selector("skill", "custom:loaded"),))
    store = JsonlStore(tmp_path / "records.jsonl")
    observer = Recorder(store, policy=policy)
    with observer.span("workflow", "root"):
        observer.observe("skill", identity("loaded"), "loaded")
        with observer.associate(Association("skill", identity("loaded"))):
            with observer.span("model", "unrelated") as call:
                call.usage = TokenUsage(input_tokens=10)
    assert len(store.read()) == 1
    assert store.read()[0].lifecycle == "loaded"


def test_multiple_skills_remain_ambiguous_and_parallel_agents_separate(tmp_path):
    store = JsonlStore(tmp_path / "records.jsonl")
    observer = Recorder(store)

    async def run_agent(name):
        with observer.span("agent", name, identity=identity(name)):
            with observer.span("skill", "outer", identity=identity("outer", "1")):
                with observer.span("skill", "inner", identity=identity("inner")):
                    await asyncio.sleep(0)
                    with observer.span("model", "generation") as call:
                        call.usage = TokenUsage(input_tokens=10)

    async def run_all():
        await asyncio.gather(run_agent("one"), run_agent("two"))

    with observer.span("workflow", "root"):
        asyncio.run(run_all())
    models = [r for r in store.read() if r.kind == "model"]
    assert len({r.trace_id for r in models}) == 1
    assert {a.identity.key for r in models for a in r.associations if a.kind == "agent"} == {
        "custom:one", "custom:two"}
    assert all(len([a for a in r.associations if a.kind == "skill" and a.measured]) == 2 for r in models)
    by_skill = summarize(store.read(), "skill")
    group = next(g for g in by_skill["groups"] if g["group"] == "unattributed")
    assert group["tokens"]["input_tokens"]["observed"] == 20


def test_disabled_returns_before_metadata_or_sink_access():
    class Sentinel:
        def emit(self, record):
            raise AssertionError("must not access sink")

    recorder = Recorder(Sentinel(), policy=CollectionPolicy(enabled=False))
    with recorder.span("invalid-kind", None):
        pass
    recorder.observe("invalid-kind", None, "invalid")
    with recorder.bind("invalid-trace"):
        pass
    assert recorder.diagnostics == {"rejected_records": 0, "delivery_errors": 0, "filtered_records": 0}


def test_invalid_instrumentation_does_not_change_original_exception(tmp_path):
    recorder = Recorder(JsonlStore(tmp_path / "records.jsonl"))
    with pytest.raises(RuntimeError, match="application failure"):
        with recorder.span("invalid-kind", "unsafe\nlabel"):
            raise RuntimeError("application failure")
    assert recorder.diagnostics["rejected_records"] == 1


def test_schema1_migration_does_not_claim_invocation_evidence():
    record = SpanRecord(trace_id="1" * 32, span_id="2" * 16, kind="model", name="legacy",
                        harness="test", adapter="test", agent_name="named", skill_name="mentioned")
    value = record.to_dict()
    for key in ("identity", "associations", "links", "lifecycle", "filtered_parent"):
        value.pop(key)
    value["schema_version"] = 1
    migrated = SpanRecord.from_dict(value)
    assert migrated.schema_version == 3 and migrated.lifecycle == "observed"
    assert not migrated.associations and migrated.identity is None
    assert not migrated.links
    assert migrated.skill_name == "mentioned"
    with pytest.raises(ValueError):
        SpanRecord.from_dict({**value, "lifecycle": "completed"})


def test_schema2_migration_adds_empty_links_without_changing_record_evidence():
    record = SpanRecord(trace_id="1" * 32, span_id="2" * 16, kind="agent", name="worker",
                        harness="test", adapter="test", identity=identity("worker"))
    value = record.to_dict()
    value.pop("links")
    value["schema_version"] = 2
    migrated = SpanRecord.from_dict(value)
    assert migrated.schema_version == 3
    assert migrated.identity == record.identity
    assert not migrated.links


def test_legacy_unknown_token_source_maps_to_other_and_preserves_counts():
    record = SpanRecord(trace_id="1" * 32, span_id="2" * 16, kind="model", name="legacy",
                        harness="test", adapter="test", usage=TokenUsage(
                            input_tokens=23, output_tokens=7))
    value = record.to_dict()
    value["usage"]["source"] = "private provider payload"

    migrated = SpanRecord.from_dict(value)

    assert migrated.usage is not None
    assert migrated.usage.source == "other"
    assert migrated.usage.input_tokens == 23
    assert migrated.usage.output_tokens == 7
    assert "private provider payload" not in str(migrated.to_dict())


def test_query_filter_marks_removed_parent_without_fabricating_records():
    root = SpanRecord(trace_id="1" * 32, span_id="2" * 16, kind="workflow", name="root",
                      harness="test", adapter="test", identity=identity("root"))
    child = replace(root, span_id="3" * 16, kind="skill", name="child",
                    identity=identity("child"), parent_span_id=root.span_id)
    policy = CollectionPolicy(include=(Selector("skill", "custom:child"),))
    selected = policy.select_records([root, child])
    assert len(selected) == 1 and selected[0].filtered_parent
    assert selected[0].parent_span_id == root.span_id


def test_bad_policy_shape_rejected_without_echoing_content(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"include": [{"PRIVATE_SENTINEL": "value"}]}))
    with pytest.raises(ValueError, match="Invalid selector object"):
        CollectionPolicy.load(path)


def test_lifecycle_revisions_preserve_incomplete_failed_and_cancelled(tmp_path):
    store = JsonlStore(tmp_path / "records.jsonl")
    observer = Recorder(store)
    with observer.span("skill", "complete"):
        assert store.read()[0].lifecycle == "invoked"
        assert store.read()[0].end_ns is None
    with pytest.raises(RuntimeError):
        with observer.span("skill", "failure"):
            raise RuntimeError("private body")
    with pytest.raises(asyncio.CancelledError):
        with observer.span("skill", "cancel"):
            raise asyncio.CancelledError()
    assert [r.lifecycle for r in store.read()] == ["completed", "failed", "cancelled"]
    assert len(store.path.read_text().splitlines()) == 6
    assert "private body" not in store.path.read_text()


def test_malformed_nested_association_is_diagnosed_by_store(tmp_path):
    record = SpanRecord(trace_id="1" * 32, span_id="2" * 16, kind="workflow", name="root",
                        harness="test", adapter="test").to_dict()
    record["associations"] = [{"kind": "skill"}]
    path = tmp_path / "records.jsonl"
    path.write_text(json.dumps(record) + "\n")
    store = JsonlStore(path)
    assert store.read() == []
    assert store.diagnostics["malformed_records"] == 1


@pytest.mark.parametrize("changes", [
    {"lifecycle": "failed", "outcome": "ok"},
    {"lifecycle": "cancelled", "outcome": "unknown"},
    {"lifecycle": "completed", "outcome": "error"},
    {"lifecycle": "invoked", "end_ns": 10},
    {"lifecycle": "invoked", "outcome": "ok"},
])
def test_schema_rejects_contradictory_lifecycle(changes):
    with pytest.raises(ValueError):
        SpanRecord(trace_id="1" * 32, span_id="2" * 16, kind="skill", name="test",
                   harness="test", adapter="test", **changes)
