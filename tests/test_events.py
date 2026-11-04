import json

import pytest

from workflow_ledger import (
    AssetIdentity, Association, CollectionPolicy, EventRecord, EventStore, JsonlStore, Recorder,
    RetentionPolicy, Selector,
)


def event(**changes):
    values = {
        "event_id": "1" * 32,
        "event_name": "agent.operation.retrying",
        "severity_text": "WARN",
        "timestamp_ns": 1,
        "harness": "example",
        "attributes": {"retry.count": 2, "retry.transient": True},
    }
    values.update(changes)
    return EventRecord(**values)


def test_event_record_maps_severity_and_round_trips():
    record = event()
    serialized = record.to_dict()

    assert record.severity_number == 13
    assert serialized["SeverityText"] == "WARN"
    assert serialized["SeverityNumber"] == 13
    assert serialized["EventName"] == record.event_name
    assert serialized["Timestamp"] == record.timestamp_ns
    assert serialized["Resource"]["Attributes"]["workflow_ledger.harness"] == "example"
    assert serialized["Attributes"]["workflow_ledger.event_id"] == record.event_id
    assert "event_id" not in serialized
    assert EventRecord.from_dict(serialized) == record


def test_shared_event_vocabulary_is_consistent_with_event_record_contract():
    from pathlib import Path

    catalog_path = Path(__file__).parents[1] / "docs" / "reference" / "event-vocabulary.json"
    catalog = json.loads(catalog_path.read_text())
    event_names = [entry["name"] for entry in catalog["events"]]

    assert catalog["vocabulary_version"] == 1
    assert len(event_names) == len(set(event_names))
    assert all(name.startswith("agent.") for name in event_names)
    assert catalog["ownership"]["workflow_ledger_prefix"].startswith("Reserved")
    for name in event_names:
        event(event_name=name)
    assert {entry["name"]: entry["severity"] for entry in catalog["events"]} == {
        "agent.operation.retrying": "WARN",
        "agent.operation.failed": "ERROR",
    }
    for entry in catalog["events"]:
        assert entry["severity"] in {"TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"}
        assert set(entry["required_attributes"] + entry["optional_attributes"]).issubset(
            catalog["attributes"]
        )


@pytest.mark.parametrize("changes", [
    {"event_name": "retry"},
    {"severity_text": "WARNING"},
    {"attributes": {"message": {"prompt": "private"}}},
    {"attributes": {"workflow_ledger.event_id": "override"}},
    {"attributes": {"app.count": 1 << 63}},
    {"timestamp_ns": 1 << 64},
    {"observed_timestamp_ns": 1 << 64},
    {"trace_id": "1" * 32},
])
def test_event_record_rejects_non_contract_values(changes):
    with pytest.raises(ValueError):
        event(**changes)


def test_event_store_is_separate_append_only_and_uses_retention(tmp_path):
    event_path = tmp_path / "events.jsonl"
    span_path = tmp_path / "spans.jsonl"
    events = EventStore(event_path, RetentionPolicy(rotation_bytes=1, archive_cap_bytes=10_000))
    spans = JsonlStore(span_path)
    first, second = event(), event(event_id="2" * 32, severity_text="ERROR", timestamp_ns=2)

    assert events.emit(first)
    assert events.emit(second)
    assert events.read() == [first, second]
    assert list(tmp_path.glob("events.jsonl.archive-*.jsonl"))
    assert spans.read() == []
    stored = json.loads(event_path.read_text().splitlines()[0])
    assert stored["Attributes"]["workflow_ledger.event_id"] == second.event_id


def test_recorder_emits_correlated_structured_events_and_respects_selection(tmp_path):
    events = EventStore(tmp_path / "events.jsonl")
    recorder = Recorder(
        JsonlStore(tmp_path / "spans.jsonl"), event_sink=events,
        policy=CollectionPolicy(include=(Selector("skill", "team:review"),)),
    )

    assert recorder.emit_event("app.test.started", "INFO")
    assert events.read() == []
    with recorder.span("skill", "review", identity=AssetIdentity("team", "review")):
        assert recorder.emit_event(
            "agent.operation.retrying", "WARN", body="Retrying operation",
            attributes={"retry.count": 1},
        )

    records = events.read()
    assert len(records) == 1
    assert records[0].trace_id is not None
    assert records[0].span_id is not None
    assert records[0].associations[0].identity.key == "team:review"
    assert records[0].severity_number == 13
    assert records[0].observed_timestamp_ns is not None


def test_policy_selects_events_by_runtime_or_measured_association():
    policy = CollectionPolicy(include=(Selector("skill", "team:review"),))
    selected = event(associations=(
        Association(
            "skill", AssetIdentity("team", "review"), "invoked", "caller_declared",
            "2" * 16, "3" * 32,
        ),
    ))
    contextual = event(associations=(
        Association(
            "skill", AssetIdentity("team", "review"), "loaded", "contextual"
        ),
    ))

    assert policy.select_events([selected, contextual]) == [selected]
