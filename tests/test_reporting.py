from dataclasses import replace

from workflow_ledger.model import Association, AssetIdentity, SpanLink, SpanRecord, TokenUsage
from workflow_ledger.report import (
    explain_workflow, group_label, identity_label, ownership, summarize,
)


TRACE = "1" * 32
ROOT = "2" * 16
MODEL = "3" * 16


def row(**overrides):
    values = dict(trace_id=TRACE, span_id=ROOT, kind="workflow", name="run",
                  harness="harness", adapter="adapter", start_ns=1, end_ns=1_000_000_001,
                  duration_quality="direct", lifecycle="completed")
    values.update(overrides)
    return SpanRecord(**values)


def test_missing_model_usage_is_unknown_with_and_without_root():
    known = row(span_id="3" * 16, kind="model", usage=TokenUsage(input_tokens=10))
    missing = row(span_id="4" * 16, kind="model")
    summary = summarize([row(), known, missing])
    tokens = summary["groups"][0]["tokens"]["input_tokens"]
    assert tokens["observed"] == 10 and tokens["known_observations"] == 1
    assert tokens["unknown_observations"] == 1
    partial = summarize([missing])["groups"][0]["tokens"]["input_tokens"]
    assert partial["observed"] is None and partial["unknown_observations"] == 1
    root = row(usage=TokenUsage(input_tokens=20, scope="run"))
    authoritative = summarize([root, known, missing])["groups"][0]["tokens"]["input_tokens"]
    assert authoritative["observed"] == 20 and authoritative["unknown_observations"] == 0


def test_agent_overlap_reports_only_overlapping_sibling_windows():
    root = row()
    first = row(span_id="4" * 16, kind="agent", parent_span_id=ROOT,
                identity=AssetIdentity("caller", "agent-a"), start_ns=0,
                end_ns=10_000_000_000)
    second = row(span_id="5" * 16, kind="agent", parent_span_id=ROOT,
                 identity=AssetIdentity("caller", "agent-b"), start_ns=5_000_000_000,
                 end_ns=15_000_000_000)
    sequential = row(span_id="6" * 16, kind="agent", parent_span_id=ROOT,
                     identity=AssetIdentity("caller", "agent-c"), start_ns=20_000_000_000,
                     end_ns=30_000_000_000)
    nested = row(span_id="7" * 16, kind="agent", parent_span_id=first.span_id,
                 identity=AssetIdentity("caller", "nested-agent"), start_ns=6_000_000_000,
                 end_ns=9_000_000_000)
    incomplete = row(span_id="8" * 16, kind="agent", parent_span_id=ROOT,
                     lifecycle="invoked", end_ns=None, duration_quality="unavailable")

    records = [root, first, second, sequential, nested, incomplete]
    overlap = summarize(records)["agent_overlap"]
    assert overlap["overlapping_sibling_pairs"] == 1
    assert overlap["pairwise_overlap_seconds"] == 5
    assert overlap["bounded_agent_windows"] == 4
    assert overlap["incomplete_agent_windows"] == 1
    rendered = explain_workflow(records)
    assert "caller:agent-a and caller:agent-b: 5.0s" in rendered
    assert "does not establish that one agent waited for another" in rendered


def test_agent_overlap_handles_large_disjoint_sibling_set():
    agents = [row(span_id=f"{index + 1:016x}", kind="agent", parent_span_id=ROOT,
                  start_ns=index * 2, end_ns=index * 2 + 1)
              for index in range(5_000)]

    overlap = summarize(agents)["agent_overlap"]

    assert overlap["overlapping_sibling_pairs"] == 0
    assert overlap["pairwise_overlap_seconds"] == 0
    assert overlap["bounded_agent_windows"] == 5_000


def test_dependency_wait_report_resolves_only_explicit_links():
    root = row()
    waiter = row(span_id="4" * 16, kind="agent", parent_span_id=ROOT,
                 identity=AssetIdentity("caller", "agent-a"), start_ns=0,
                 end_ns=10_000_000_000)
    target = row(span_id="5" * 16, kind="agent", parent_span_id=ROOT,
                 identity=AssetIdentity("caller", "agent-b"), start_ns=2_000_000_000,
                 end_ns=9_000_000_000)
    wait = row(span_id="6" * 16, kind="wait", name="dependency wait",
               parent_span_id=waiter.span_id, start_ns=3_000_000_000,
               end_ns=8_000_000_000,
               links=(SpanLink("waits_for", TRACE, target.span_id),))

    records = [root, waiter, target, wait]
    summary = summarize(records)
    assert summary["schema_version"] == 3
    assert summary["dependency_waits"] == {
        "spans": 1, "bounded_windows": 1, "links": 1, "resolved_links": 1,
        "overlapping_target_windows": 1,
    }
    rendered = explain_workflow(records)
    assert "Parent: agent caller:agent-a." in rendered
    assert "Waits for: agent caller:agent-b (caller_declared link; 5.0s recorded interval overlap)." in rendered


def test_dependency_wait_report_marks_unavailable_target_without_inference():
    wait = row(span_id="6" * 16, kind="wait", name="dependency wait",
               links=(SpanLink("waits_for", "9" * 32, "8" * 16),))
    rendered = explain_workflow([wait])
    assert "Waits for: target invocation is not present in the selected records" in rendered


def test_dependency_wait_report_does_not_call_disjoint_windows_overlap():
    target = row(span_id="5" * 16, kind="agent", parent_span_id=ROOT,
                 start_ns=0, end_ns=2_000_000_000)
    wait = row(span_id="6" * 16, kind="wait", name="dependency wait",
               start_ns=3_000_000_000, end_ns=4_000_000_000,
               links=(SpanLink("waits_for", TRACE, target.span_id),))

    rendered = explain_workflow([target, wait])
    assert "no recorded interval overlap" in rendered


def test_configured_workflow_does_not_establish_usage_coverage():
    configured = row(lifecycle="configured", end_ns=None, duration_quality="unavailable")
    tokens = summarize([configured])["groups"][0]["tokens"]
    assert all(t["observed"] is None and t["unknown_observations"] == 0 for t in tokens.values())


def measured(kind, namespace, logical_id, version, record):
    return Association(kind, AssetIdentity(namespace, logical_id, version, "native"),
                       "invoked", "native", record.span_id, record.trace_id)


def test_loaded_asset_has_lifecycle_but_no_invocation_or_measurement():
    identity = AssetIdentity("agent-pack", "reviewer", "2.1", "native")
    loaded = row(kind="agent", span_id=ROOT, start_ns=None, end_ns=None,
                 duration_quality="unavailable", lifecycle="loaded", identity=identity,
                 agent_name="legacy-ish")
    summary = summarize([loaded], "agent")
    group = summary["groups"][0]
    assert group["group"] == "agent-pack:reviewer@2.1"
    assert group["observed_records"] == 1
    assert group["invocations"] == 0
    assert group["lifecycle_counts"] == {"loaded": 1}
    assert group["tokens"]["input_tokens"]["observed"] is None
    assert group["duration_seconds"]["known_duration_records"] == 0


def test_measured_versions_distinguish_and_ambiguous_ownership_is_unattributed():
    first = row(kind="model", span_id=MODEL, parent_span_id=ROOT, model="provider-model",
                usage=TokenUsage(input_tokens=5, scope="model_call"), skill_name="flat-skill")
    first = replace(first, associations=(measured("skill", "catalog", "writer", "1", first),))
    second_id = AssetIdentity("catalog", "writer", "2", "native")
    second = Association("skill", second_id, "invoked", "native", first.span_id, first.trace_id)
    ambiguous = row(kind="model", span_id="4" * 16, parent_span_id=ROOT,
                    usage=TokenUsage(input_tokens=9, scope="model_call"),
                    associations=(first.associations[0], second), skill_name="flat-skill")

    assert group_label(first, "skill") == "catalog:writer@1"
    assert group_label(first, "model", for_usage=True) == "provider:provider-model"
    assert ownership(ambiguous, "skill") is None
    by_skill = {g["group"]: g for g in summarize([first, ambiguous], "skill")["groups"]}
    assert by_skill["catalog:writer@1"]["tokens"]["input_tokens"]["observed"] == 5
    assert by_skill["unattributed"]["tokens"]["input_tokens"]["observed"] == 9


def test_run_totals_and_contextual_flat_labels_never_assign_child_usage():
    root = row(usage=TokenUsage(input_tokens=20, scope="run"), agent_name="flat-agent",
               skill_name="flat-skill")
    model = row(kind="model", span_id=MODEL, parent_span_id=ROOT,
                usage=TokenUsage(input_tokens=18, output_tokens=7, scope="model_call"),
                agent_name="flat-agent", skill_name="flat-skill")
    grouped = summarize([root, model], "skill")
    assert all(g["group"] != "flat-skill" for g in grouped["groups"])
    groups = {g["group"]: g for g in grouped["groups"]}
    assert groups["unattributed"]["tokens"]["input_tokens"]["observed"] == 20
    assert groups["unattributed"]["tokens"]["output_tokens"]["observed"] == 7
    assert grouped["contextual_labels"]


def test_legacy_flat_label_remains_context_only_and_filtered_gap_is_not_orphan():
    legacy_data = row(agent_name="old-agent", lifecycle="observed").to_dict()
    for key in ("identity", "associations", "links", "lifecycle", "filtered_parent"):
        legacy_data.pop(key, None)
    legacy_data["schema_version"] = 1
    legacy = SpanRecord.from_dict(legacy_data)
    filtered = row(span_id="4" * 16, parent_span_id="5" * 16, filtered_parent=True,
                   lifecycle="invoked", duration_quality="unavailable", end_ns=None)
    summary = summarize([legacy, filtered], "agent")
    assert summary["orphan_parent_links"] == 0
    assert summary["filtered_ancestry_gaps"] == 1
    assert summary["contextual_labels"] == [
        {"kind": "agent", "label": "old-agent", "evidence": "legacy", "observations": 1}
    ]


def test_identity_group_encoding_is_unambiguous_and_mixed_versions_sort():
    no_version = AssetIdentity("skills", "writer@2", None, "native")
    versioned = AssetIdentity("skills", "writer", "2", "native")
    assert identity_label(no_version) == r"skills:writer\@2"
    assert identity_label(versioned) == "skills:writer@2"
    unloaded = row(kind="skill", lifecycle="loaded", start_ns=None, end_ns=None,
                   duration_quality="unavailable", identity=no_version)
    version_loaded = row(kind="skill", span_id="4" * 16, lifecycle="loaded", start_ns=None,
                         end_ns=None, duration_quality="unavailable", identity=versioned)
    assert len(summarize([unloaded, version_loaded])["contextual_assets"]) == 2
