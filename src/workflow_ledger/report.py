"""Coverage-aware reporting and identity ownership shared with exporters."""

from collections import Counter, defaultdict
from collections.abc import Iterator
from statistics import median

from .model import EXECUTION_STATES, AssetIdentity, SpanRecord, TOKEN_FIELDS

GROUP_FIELDS = {"agent": "agent", "skill": "skill", "model": "model",
                "tool": "tool", "harness": "harness", "kind": "kind"}
_IDENTITY_GROUPS = {"agent": "agent", "skill": "skill", "model": "model", "tool": "tool"}


def latest_records(records: list[SpanRecord]) -> list[SpanRecord]:
    return list({record.key: record for record in records}.values())


def identity_label(identity: AssetIdentity) -> str:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("@", "\\@")

    key = f"{identity.namespace}:{escape(identity.logical_id)}"
    return key if identity.version is None else f"{key}@{escape(identity.version)}"


def ownership(record: SpanRecord, kind: str) -> AssetIdentity | None:
    """Return a unique measured association; ambiguity is deliberately unattributed."""
    identities = {
        (association.identity.namespace, association.identity.logical_id,
         association.identity.version): association.identity
        for association in record.associations
        if association.kind == kind and association.measured
    }
    return next(iter(identities.values())) if len(identities) == 1 else None


def group_label(record: SpanRecord, group_by: str | None, for_usage: bool = False) -> str:
    """Resolve a group from measured associations, with lifecycle identities kept separate."""
    if group_by is None:
        return "all"
    if group_by == "kind":
        return record.kind
    if group_by == "harness":
        return record.harness
    identity_kind = _IDENTITY_GROUPS.get(group_by)
    if identity_kind is None:
        raise ValueError("Unsupported report grouping")
    if record.kind == "workflow":
        if for_usage and group_by in _IDENTITY_GROUPS:
            return "unattributed"
        return "workflow-context"
    identity = ownership(record, identity_kind)
    if (identity is None and for_usage and group_by == "model" and record.usage is not None
            and record.usage.scope == "model_call" and record.model is not None):
        return f"provider:{record.model}"
    if identity is None and not for_usage and record.kind == identity_kind and (
        record.lifecycle in {"configured", "loaded"}
    ):
        identity = record.identity
    return identity_label(identity) if identity else "unattributed"


def usage_observations(records: list[SpanRecord]) -> list[tuple[SpanRecord, str, int | None]]:
    """Select token basis per trace/field; never infer ownership from legacy labels."""
    traces: dict[str, list[SpanRecord]] = defaultdict(list)
    for record in latest_records(records):
        traces[record.trace_id].append(record)
    result = []
    for rows in traces.values():
        roots = [r for r in rows if r.kind == "workflow" and r.parent_span_id is None
                 and r.lifecycle not in {"configured", "loaded"}]
        run_usage = [r for r in roots if r.usage is not None and r.usage.scope == "run"]
        models = [r for r in rows if r.kind == "model" and r.lifecycle not in {"configured", "loaded"}]
        if not roots and not models:
            continue
        for field in TOKEN_FIELDS:
            if len(run_usage) > 1:
                result.append((run_usage[0], field, None))
            elif run_usage and getattr(run_usage[0].usage, field) is not None:
                result.append((run_usage[0], field, getattr(run_usage[0].usage, field)))
            elif models:
                for model in models:
                    result.append((model, field, getattr(model.usage, field, None)))
            else:
                result.append((roots[0] if roots else rows[0], field, None))
    return result


def _bucket() -> dict:
    return {"observed_records": 0, "invocations": 0, "lifecycle_counts": Counter(),
            "outcomes": Counter(), "kinds": Counter(), "durations": [],
            "duration_quality": Counter(), "tokens": {
                field: {"observed": None, "known_observations": 0, "unknown_observations": 0,
                        "quality": Counter(), "scope": Counter(), "source": Counter()}
                for field in TOKEN_FIELDS
            }}


def _asset_info(identity: AssetIdentity) -> dict:
    return {"namespace": identity.namespace, "logical_id": identity.logical_id,
            "version": identity.version, "provenance": identity.provenance}


def _agent_overlap_pairs(records: list[SpanRecord]) -> Iterator[dict]:
    """Find overlapping timestamp windows for sibling agent invocations."""
    siblings: dict[tuple[str, str | None], list[SpanRecord]] = defaultdict(list)
    for record in latest_records(records):
        if (record.kind == "agent" and record.lifecycle not in {"configured", "loaded"}
                and record.start_ns is not None and record.end_ns is not None
                and record.duration_quality != "unavailable"):
            siblings[(record.trace_id, record.parent_span_id)].append(record)

    for (trace_id, _), agents in sorted(siblings.items(), key=lambda item: (item[0][0], item[0][1] or "")):
        agents.sort(key=lambda record: (record.start_ns, record.end_ns, record.span_id))
        for index, left in enumerate(agents):
            for right_index in range(index + 1, len(agents)):
                right = agents[right_index]
                if right.start_ns >= left.end_ns:
                    break
                overlap_ns = min(left.end_ns, right.end_ns) - right.start_ns
                if overlap_ns <= 0:
                    continue
                yield {
                    "trace_id": trace_id,
                    "left_span_id": left.span_id,
                    "left_agent": identity_label(left.identity) if left.identity else left.name,
                    "right_span_id": right.span_id,
                    "right_agent": identity_label(right.identity) if right.identity else right.name,
                    "overlap_seconds": overlap_ns / 1_000_000_000,
                    "duration_quality": [left.duration_quality, right.duration_quality],
                }


def _agent_window_counts(records: list[SpanRecord]) -> tuple[int, int]:
    agents = [record for record in latest_records(records)
              if record.kind == "agent" and record.lifecycle not in {"configured", "loaded"}]
    bounded = sum(record.start_ns is not None and record.end_ns is not None
                  and record.duration_quality != "unavailable" for record in agents)
    return bounded, len(agents) - bounded


def summarize(records: list[SpanRecord], group_by: str | None = None) -> dict:
    if group_by is not None and group_by not in GROUP_FIELDS:
        raise ValueError("Unsupported report grouping")
    records = latest_records(records)
    buckets: dict[str, dict] = defaultdict(_bucket)
    keys = {(r.trace_id, r.span_id) for r in records}
    filtered_gaps = sum(r.filtered_parent for r in records)
    orphan_links = sum(r.parent_span_id is not None and r.parent_span_id and
                       (r.trace_id, r.parent_span_id) not in keys and not r.filtered_parent
                       for r in records)
    contextual_assets = Counter()
    contextual_labels = Counter()
    for record in records:
        label = group_label(record, group_by)
        bucket = buckets[label]
        bucket["observed_records"] += 1
        bucket["lifecycle_counts"][record.lifecycle] += 1
        if record.lifecycle in EXECUTION_STATES:
            bucket["invocations"] += 1
        bucket["outcomes"][record.outcome] += 1
        bucket["kinds"][record.kind] += 1
        bucket["duration_quality"][record.duration_quality] += 1
        if (record.lifecycle not in {"configured", "loaded"} and record.duration_quality != "unavailable"
                and record.duration_seconds is not None):
            bucket["durations"].append(record.duration_seconds)

        for association in record.associations:
            if not association.measured:
                identity = association.identity
                contextual_assets[(association.kind, identity.namespace, identity.logical_id,
                                   identity.version, association.state, association.evidence)] += 1
        if record.identity is not None and record.lifecycle in {"configured", "loaded"}:
            identity = record.identity
            contextual_assets[(record.kind, identity.namespace, identity.logical_id,
                               identity.version, record.lifecycle, identity.provenance)] += 1
        for kind, flat_label in (("agent", record.agent_name), ("skill", record.skill_name),
                                 ("model", record.model)):
            if flat_label and ownership(record, kind) is None:
                evidence = "legacy" if record.lifecycle == "observed" and not record.associations else "contextual"
                contextual_labels[(kind, flat_label, evidence)] += 1

    for record, field, value in usage_observations(records):
        label = group_label(record, group_by, for_usage=True)
        token = buckets[label]["tokens"][field]
        if value is None:
            token["unknown_observations"] += 1
        else:
            token["observed"] = (token["observed"] or 0) + value
            token["known_observations"] += 1
        token["quality"][record.usage.quality if record.usage and value is not None else "unavailable"] += 1
        token["scope"][record.usage.scope if record.usage else "unavailable"] += 1
        token["source"][record.usage.source if record.usage else "unavailable"] += 1

    result = []
    for label, bucket in sorted(buckets.items()):
        durations = bucket.pop("durations")
        bucket["duration_seconds"] = {
            "known_duration_records": len(durations), "sum_operation_time": sum(durations) if durations else None,
            "median": median(durations) if durations else None,
            "maximum": max(durations) if durations else None,
        }
        result.append({"group": label, **bucket})
    workflows = [r for r in records if r.kind == "workflow" and r.parent_span_id is None]
    overlap_count = 0
    pairwise_overlap_seconds = 0.0
    for pair in _agent_overlap_pairs(records):
        overlap_count += 1
        pairwise_overlap_seconds += pair["overlap_seconds"]
    bounded_agent_windows, incomplete_agent_windows = _agent_window_counts(records)
    wait_spans = [record for record in latest_records(records) if record.kind == "wait"]
    wait_links = [(record, link) for record in wait_spans for link in record.links]
    records_by_key = {(record.trace_id, record.span_id): record for record in latest_records(records)}
    resolved_wait_links = [(wait, link, records_by_key.get((link.trace_id, link.span_id)))
                           for wait, link in wait_links]
    overlapping_wait_targets = sum(
        target is not None and wait.start_ns is not None and wait.end_ns is not None
        and wait.duration_quality != "unavailable" and target.start_ns is not None
        and target.end_ns is not None and target.duration_quality != "unavailable"
        and min(wait.end_ns, target.end_ns) > max(wait.start_ns, target.start_ns)
        for wait, _link, target in resolved_wait_links
    )
    return {
        "schema_version": 3, "group_by": group_by, "records": len(records),
        "observed_records": len(records),
        "observed_traces": len({r.trace_id for r in records}),
        "root_workflows": len(workflows), "orphan_parent_links": orphan_links,
        "filtered_ancestry_gaps": filtered_gaps, "groups": result,
        "agent_overlap": {
            "overlapping_sibling_pairs": overlap_count,
            "pairwise_overlap_seconds": pairwise_overlap_seconds,
            "bounded_agent_windows": bounded_agent_windows,
            "incomplete_agent_windows": incomplete_agent_windows,
        },
        "dependency_waits": {
            "spans": len(wait_spans),
            "bounded_windows": sum(record.start_ns is not None and record.end_ns is not None
                                    and record.duration_quality != "unavailable" for record in wait_spans),
            "links": len(wait_links),
            "resolved_links": sum(target is not None for _wait, _link, target in resolved_wait_links),
            "overlapping_target_windows": overlapping_wait_targets,
        },
        "contextual_assets": [
            {"kind": k, "namespace": ns, "logical_id": logical, "version": version,
             "state": state, "evidence": evidence, "observations": count}
            for (k, ns, logical, version, state, evidence), count in sorted(
                contextual_assets.items(),
                key=lambda item: (item[0][0], item[0][1], item[0][2],
                                  item[0][3] is not None, item[0][3] or "",
                                  item[0][4], item[0][5]))
        ],
        "contextual_labels": [
            {"kind": kind, "label": label, "evidence": evidence, "observations": count}
            for (kind, label, evidence), count in sorted(contextual_labels.items())
        ],
        "measurement_notes": [
            "Observed records, execution invocations, and asset lifecycle states are reported separately.",
            "Configured and loaded assets do not establish invocation, duration, or token ownership.",
            "Only unique measured associations determine agent, skill, model, and tool ownership.",
            "Legacy flat labels are contextual and never establish ownership.",
            "Operation durations can overlap; their sum is not elapsed workflow time.",
            "Agent overlap compares selected sibling agent windows with recorded start and end timestamps; overlap does not establish a dependency.",
            "Wait spans and waits-for links are caller-declared or native evidence; missing targets do not imply no dependency.",
            "Token figures are observed counts; coverage does not prove complete capture.",
            "Cached input and reasoning are subsets, not additional token totals.",
            "Run totals override child usage per field; ambiguous or unsupported attribution stays unattributed.",
        ],
    }


def _duration_quality_text(quality: str) -> str:
    return {
        "direct": "directly measured",
        "inferred": "estimated from timestamps",
        "unavailable": "unavailable",
    }.get(quality, quality)


def _duration_quality_count_text(quality: str, count: int) -> str:
    return {
        "direct": f"{count} direct measurement(s)",
        "inferred": f"{count} timestamp-based estimate(s)",
        "unavailable": f"{count} unavailable duration(s)",
    }.get(quality, f"{count} {quality} duration(s)")


def explain_report(summary: dict) -> str:
    """Render a bounded, human-readable interpretation of a report summary."""
    lines = ["Workflow observation summary", ""]
    records = summary.get("records", 0)
    traces = summary.get("observed_traces", 0)
    lines.append(f"Read {records} selected record(s) across {traces} trace(s).")

    groups = summary.get("groups", [])
    if not groups:
        lines.extend(["", "No selected observations were found. This does not establish that no work ran."])
    else:
        lines.extend(["", "By observed asset or workflow context:"])
        for group in groups:
            label = group["group"]
            rows = group.get("observed_records", 0)
            invocations = group.get("invocations", 0)
            if label == "workflow-context":
                lines.append(f"- Workflow context: {invocations} context window(s), {rows} record(s).")
            else:
                lines.append(f"- {label}: {invocations} execution invocation(s), {rows} record(s).")

            outcomes = group.get("outcomes", {})
            if outcomes:
                outcome_text = ", ".join(f"{name}: {count}" for name, count in sorted(outcomes.items()))
                lines.append(f"  Outcomes: {outcome_text}.")

            duration = group.get("duration_seconds", {})
            known_durations = duration.get("known_duration_records", 0)
            if known_durations:
                quality = ", ".join(
                    _duration_quality_count_text(name, count)
                    for name, count in sorted(group.get("duration_quality", {}).items())
                ) or "unspecified"
                median_value = duration.get("median")
                maximum = duration.get("maximum")
                if median_value is not None:
                    lines.append(
                        f"  Median recorded operation window: {median_value:.1f}s "
                        f"({known_durations} timed record(s); {quality})."
                    )
                elif maximum is not None:
                    lines.append(f"  Recorded operation window available; quality: {quality}.")
            else:
                lines.append("  Duration: unavailable in these records.")

            token_fields = group.get("tokens", {})
            known_tokens = {
                field: value for field, value in token_fields.items()
                if value.get("known_observations", 0) > 0
            }
            if known_tokens:
                token_text = ", ".join(
                    f"{field.removesuffix('_tokens').replace('_', ' ')}: {value['observed']}"
                    for field, value in sorted(known_tokens.items())
                )
                lines.append(f"  Observed token counts: {token_text}.")
                unavailable = [
                    field.removesuffix("_tokens").replace("_", " ")
                    for field, value in token_fields.items()
                    if value.get("known_observations", 0) == 0
                ]
                if unavailable:
                    lines.append(f"  Other token fields are unavailable: {', '.join(sorted(unavailable))}.")
            else:
                lines.append("  Token usage: unavailable from these observations; this does not mean zero.")

    store = summary.get("store", {})
    lines.extend([
        "",
        "Capture and data limits:",
        f"- Store read: {store.get('malformed_records', 0)} malformed record(s); "
        f"{store.get('storage_errors', 0)} storage error(s) reported by this query.",
    ])
    filtered = summary.get("query_filtered_records", 0)
    if filtered:
        lines.append(f"- The active policy filtered {filtered} record(s) from this query.")
    lines.append(f"- Task/session roots recorded: {summary.get('root_workflows', 0)}.")
    if summary.get("root_workflows", 0) == 0:
        lines.append("  No whole-task boundary is available from these observations.")
    agent_overlap = summary.get("agent_overlap", {})
    overlap_count = agent_overlap.get("overlapping_sibling_pairs", 0)
    overlap_seconds = agent_overlap.get("pairwise_overlap_seconds", 0.0)
    lines.append(
        f"- Agent windows: {overlap_count} overlapping sibling pair(s) among "
        f"{agent_overlap.get('bounded_agent_windows', 0)} fully bounded window(s); "
        f"{overlap_seconds:.1f}s summed pairwise overlap."
    )
    incomplete_agent_windows = agent_overlap.get("incomplete_agent_windows", 0)
    if incomplete_agent_windows:
        lines.append(
            f"  {incomplete_agent_windows} agent invocation(s) lack complete duration evidence "
            "and are excluded from overlap comparisons."
        )
    dependency_waits = summary.get("dependency_waits", {})
    if dependency_waits.get("spans", 0):
        lines.append(
            f"- Dependency waits: {dependency_waits['spans']} span(s), "
            f"{dependency_waits.get('links', 0)} explicit target link(s); "
            f"{dependency_waits.get('resolved_links', 0)} target(s) are in the selected records; "
            f"{dependency_waits.get('overlapping_target_windows', 0)} target interval(s) overlap the wait."
        )
    orphan_links = summary.get("orphan_parent_links", 0)
    ancestry_gaps = summary.get("filtered_ancestry_gaps", 0)
    if orphan_links or ancestry_gaps:
        lines.append(f"- Parent links: {orphan_links} orphan(s), {ancestry_gaps} filtered ancestry gap(s).")
    else:
        lines.append("- Parent links: no orphan links or filtered ancestry gaps reported.")
    lines.append("- This describes only the selected store and policy; it is not proof of complete capture or workflow quality.")
    lines.append("- Operation windows can include waiting and marker overhead, and can overlap; they are not active model time.")
    lines.append("- Recorded outcomes describe event status; they are not an independent review of the work product.")
    return "\n".join(lines)


def explain_workflow(records: list[SpanRecord], store: dict | None = None) -> str:
    """Render a bounded explanation of the records for one trace."""
    records = latest_records(records)
    if not records:
        return "No selected observations were found for this trace. This does not establish that no work ran."

    lines = [f"Workflow observation: {len(records)} selected record(s)"]
    records_by_span = {(record.trace_id, record.span_id): record for record in records}
    overlap_pairs = list(_agent_overlap_pairs(records))
    bounded_agent_windows, incomplete_agent_windows = _agent_window_counts(records)
    for record in records:
        label = identity_label(record.identity) if record.identity else record.name
        lines.extend(["", f"- {record.kind}: {label}",
                      f"  Lifecycle: {record.lifecycle}; recorded outcome: {record.outcome or 'unknown'}."])
        if record.identity:
            evidence = "caller-declared marker" if record.identity.provenance == "caller_declared" else record.identity.provenance
            lines.append(f"  Identity source: {evidence}.")
        duration = record.duration_seconds
        if duration is not None and record.duration_quality != "unavailable":
            lines.append(
                f"  Recorded operation window: {duration:.1f}s "
                f"({_duration_quality_text(record.duration_quality)})."
            )
        else:
            lines.append("  Duration: unavailable.")
        if record.usage is None:
            lines.append("  Token usage: not attached to this record; this does not mean zero.")
        else:
            counts = [
                f"{field.removesuffix('_tokens').replace('_', ' ')}: {getattr(record.usage, field)}"
                for field in TOKEN_FIELDS if getattr(record.usage, field) is not None
            ]
            lines.append("  Token usage: " + (", ".join(counts) if counts else "counts unavailable") + ".")
        parent_record = records_by_span.get((record.trace_id, record.parent_span_id)) if record.parent_span_id else None
        if parent_record is not None:
            parent_label = identity_label(parent_record.identity) if parent_record.identity else parent_record.name
            lines.append(f"  Parent: {parent_record.kind} {parent_label}.")
        elif record.parent_span_id is None and record.kind != "workflow":
            lines.append("  Parent: none recorded; this is not a complete whole-task trace.")
        elif record.parent_span_id is None:
            lines.append("  Parent: this is a root workflow span.")
        else:
            lines.append("  Parent: a parent span is recorded.")
        if record.kind == "wait":
            if not record.links:
                lines.append("  Wait target: unknown; no target invocation link was recorded.")
            for link in record.links:
                target = records_by_span.get((link.trace_id, link.span_id))
                if target is None:
                    target_label = "target invocation is not present in the selected records"
                else:
                    target_name = identity_label(target.identity) if target.identity else target.name
                    target_label = f"{target.kind} {target_name}"
                overlap = None
                if (target is not None and record.start_ns is not None and record.end_ns is not None
                        and record.duration_quality != "unavailable" and target.start_ns is not None
                        and target.end_ns is not None and target.duration_quality != "unavailable"):
                    overlap = max(0, min(record.end_ns, target.end_ns)
                                  - max(record.start_ns, target.start_ns)) / 1_000_000_000
                if overlap is None:
                    detail = "target interval is unavailable"
                elif overlap == 0:
                    detail = "no recorded interval overlap"
                else:
                    detail = f"{overlap:.1f}s recorded interval overlap"
                lines.append(f"  Waits for: {target_label} ({link.evidence} link; {detail}).")

    diagnostics = store or {}
    lines.extend([
        "",
    ])
    lines.append(
        f"Sibling agent-window overlaps: {len(overlap_pairs)} pair(s) among "
        f"{bounded_agent_windows} fully bounded invocation window(s)."
    )
    if overlap_pairs:
        for pair in overlap_pairs:
            qualities = "/".join(pair["duration_quality"])
            lines.append(
                f"- {pair['left_agent']} and {pair['right_agent']}: "
                f"{pair['overlap_seconds']:.1f}s ({qualities} timestamps)."
            )
    if incomplete_agent_windows:
        lines.append(
            f"{incomplete_agent_windows} agent invocation(s) lack complete duration evidence "
            "and are excluded from overlap comparisons."
        )
    lines.extend([
        "Agent-window overlap does not establish that one agent waited for another.",
        f"Store read: {diagnostics.get('malformed_records', 0)} malformed record(s); "
        f"{diagnostics.get('storage_errors', 0)} storage error(s) reported by this query.",
        "This trace view reflects selected records only; missing spans do not prove that work did not occur.",
        "Recorded operation windows can include waiting and marker overhead, and are not active model time.",
        "Recorded outcomes describe event status; they are not an independent review of the work product.",
    ])
    return "\n".join(lines)
