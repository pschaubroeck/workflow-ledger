"""Optional, explicitly configured OpenTelemetry export for completed records."""

from __future__ import annotations

from collections import defaultdict
import math
import re
from urllib.parse import urlsplit, urlunsplit

from .model import EXECUTION_STATES, TOKEN_FIELDS
from .report import identity_label, latest_records, ownership, usage_observations


def _valid_endpoint(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
        _ = parts.port  # Validate a supplied port while allowing the scheme's default port.
        return (parts.scheme in {"http", "https"} and bool(parts.hostname)
                and parts.username is None and parts.password is None
                and not parts.query and not parts.fragment)
    except ValueError:
        return False


def _dimensions(record, *, duration_quality: str | None = None, for_usage: bool = False) -> dict:
    labels = {"kind": record.kind, "outcome": record.outcome}
    labels["harness"] = record.harness
    for kind in ("agent", "skill", "tool"):
        identity = ownership(record, kind)
        if identity is not None:
            labels[f"{kind}_identity"] = identity_label(identity)
    model_identity = ownership(record, "model")
    if model_identity is not None:
        labels["model_identity"] = identity_label(model_identity)
    elif (for_usage and record.usage is not None and record.usage.scope == "model_call"
          and record.model is not None):
        labels["provider_model"] = record.model
    if duration_quality is not None:
        labels["quality"] = duration_quality
    return labels


def _snapshot(records: list) -> dict:
    tokens = defaultdict(int)
    grouped_tokens = defaultdict(int)
    by_source = defaultdict(int)
    coverage = defaultdict(int)
    for record, field, value in usage_observations(records):
        coverage[(field, "known" if value is not None else "unknown")] += 1
        if value is None or record.usage is None:
            continue
        dimensions = tuple(sorted(_dimensions(record, for_usage=True).items()))
        by_source[(field, record.usage.source, record.usage.quality, dimensions)] += value
        tokens[(field, record.usage.quality)] += value
        grouped_tokens[(field, record.usage.quality,
                        dimensions)] += value

    observed = defaultdict(int)
    invocations = defaultdict(int)
    lifecycle_counts = defaultdict(int)
    durations = defaultdict(float)
    duration_counts = defaultdict(int)
    for record in records:
        dims = tuple(sorted(_dimensions(record).items()))
        observed[dims] += 1
        lifecycle_counts[tuple(sorted({"kind": record.kind, "state": record.lifecycle,
                                       **({"asset": identity_label(record.identity)}
                                          if record.identity else {})}.items()))] += 1
        for association in record.associations:
            lifecycle_counts[tuple(sorted({"kind": association.kind, "state": association.state,
                                           "evidence": association.evidence,
                                           "asset": identity_label(association.identity)}.items()))] += 1
        if record.lifecycle in EXECUTION_STATES:
            invocations[dims] += 1
        if (record.lifecycle not in {"configured", "loaded"} and record.duration_quality != "unavailable"
                and record.duration_seconds is not None):
            duration_dims = _dimensions(record, duration_quality=record.duration_quality)
            key = tuple(sorted(duration_dims.items()))
            durations[key] += record.duration_seconds
            duration_counts[key] += 1
    span_keys = {(row.trace_id, row.span_id) for row in records}
    ancestry = {
        (("state", "filtered"),): sum(row.filtered_parent for row in records),
        (("state", "orphan"),): sum(
            row.parent_span_id is not None and (row.trace_id, row.parent_span_id) not in span_keys
            and not row.filtered_parent for row in records),
    }
    return {"tokens": dict(tokens), "grouped_tokens": dict(grouped_tokens),
            "source_tokens": dict(by_source),
            "coverage": dict(coverage), "observed": dict(observed),
            "invocations": dict(invocations), "lifecycle_counts": dict(lifecycle_counts),
            "duration_sums": dict(durations), "duration_counts": dict(duration_counts),
            "ancestry": ancestry}


def export_records(
    records,
    endpoint: str | None,
    service_name: str = "workflow-ledger",
    timeout: float = 5.0,
    headers: dict[str, str] | None = None,
) -> dict:
    """Export records via private SDK objects and OTLP/HTTP protobuf.

    A caller-supplied base or signal endpoint is required; no exporter defaults are used.
    Incomplete spans are skipped, while their independently observed usage can still
    contribute to the snapshot metrics.
    """
    diagnostics = {
        "enabled": bool(endpoint), "exported_spans": 0,
        "skipped_incomplete_spans": 0, "trace_export_failures": 0,
        "skipped_non_execution_spans": 0, "metric_export_failures": 0,
        "export_errors": 0, "errors": [],
    }
    if not endpoint:
        diagnostics["enabled"] = False
        return diagnostics
    if (not isinstance(endpoint, str) or not _valid_endpoint(endpoint)
            or isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0
            or (headers is not None and (
                not isinstance(headers, dict)
                or any(not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key)
                       or not isinstance(value, str) or "\r" in value or "\n" in value
                       for key, value in headers.items()
            )))):
        diagnostics["enabled"] = False
        diagnostics["export_errors"] = 1
        diagnostics["errors"].append("endpoint or timeout configuration is invalid")
        return diagnostics

    records = latest_records(list(records))
    completed = []
    for record in records:
        if record.lifecycle in {"configured", "loaded"}:
            diagnostics["skipped_non_execution_spans"] = diagnostics.get("skipped_non_execution_spans", 0) + 1
        elif record.start_ns is None or record.end_ns is None:
            diagnostics["skipped_incomplete_spans"] += 1
        else:
            completed.append(record)
    metric_snapshot = _snapshot(records)

    try:
        from opentelemetry.metrics import Observation
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader, MetricExportResult
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.trace import Link, SpanContext, SpanKind, TraceFlags, Status, StatusCode
    except ImportError as exc:
        diagnostics["enabled"] = False
        diagnostics["export_errors"] = 1
        diagnostics["errors"].append(f"OpenTelemetry extras unavailable ({type(exc).__name__})")
        return diagnostics

    parts = urlsplit(endpoint)
    path = parts.path.rstrip("/")
    suffix = "/v1/traces" if path.endswith("/v1/traces") else (
        "/v1/metrics" if path.endswith("/v1/metrics") else None)
    prefix = path[:-len(suffix)] if suffix else path
    trace_endpoint = urlunsplit((parts.scheme, parts.netloc, f"{prefix}/v1/traces", "", ""))
    metric_endpoint = urlunsplit((parts.scheme, parts.netloc, f"{prefix}/v1/metrics", "", ""))

    trace_exporter = None
    metric_exporter = None
    meter_provider = None
    stage = "trace"
    try:
        # Direct construction avoids ambient resource detectors and global providers.
        resource = Resource({"service.name": service_name})
        explicit_headers = dict(headers or {})
        trace_exporter = OTLPSpanExporter(endpoint=trace_endpoint, timeout=timeout,
                                           headers=explicit_headers)
        spans = []
        for record in completed:
            attrs = {
                "workflow_ledger.kind": record.kind,
                "workflow_ledger.source": record.adapter,
                "workflow_ledger.quality": record.duration_quality,
                "workflow_ledger.outcome": record.outcome,
                "workflow_ledger.harness": record.harness,
                "workflow_ledger.adapter_version": record.adapter_version,
                "workflow_ledger.lifecycle": record.lifecycle,
                "workflow_ledger.filtered_parent": record.filtered_parent,
            }
            for key, value in (("source_version", record.source_version),
                               ("error_type", record.error_type)):
                if value is not None:
                    attrs[f"workflow_ledger.{key}"] = value
            if record.identity is not None:
                attrs.update({
                    "workflow_ledger.identity.namespace": record.identity.namespace,
                    "workflow_ledger.identity.logical_id": record.identity.logical_id,
                    "workflow_ledger.identity.provenance": record.identity.provenance,
                })
                if record.identity.version is not None:
                    attrs["workflow_ledger.identity.version"] = record.identity.version
            associations = tuple(record.associations)
            if associations:
                attrs["workflow_ledger.association_kinds"] = tuple(a.kind for a in associations)
                attrs["workflow_ledger.association_assets"] = tuple(
                    identity_label(a.identity) for a in associations)
                attrs["workflow_ledger.association_namespaces"] = tuple(
                    a.identity.namespace for a in associations)
                attrs["workflow_ledger.association_logical_ids"] = tuple(
                    a.identity.logical_id for a in associations)
                attrs["workflow_ledger.association_versions"] = tuple(
                    a.identity.version or "" for a in associations)
                attrs["workflow_ledger.association_provenance"] = tuple(
                    a.identity.provenance for a in associations)
                attrs["workflow_ledger.association_states"] = tuple(a.state for a in associations)
                attrs["workflow_ledger.association_evidence"] = tuple(a.evidence for a in associations)
            if record.kind == "model" and record.usage and record.usage.scope == "model_call" and record.model:
                attrs["workflow_ledger.provider_model"] = record.model
            if record.usage:
                attrs["workflow_ledger.token_quality"] = record.usage.quality
                attrs["workflow_ledger.token_source"] = record.usage.source
                if record.kind == "model":
                    for field in TOKEN_FIELDS:
                        value = getattr(record.usage, field)
                        if value is not None:
                            attrs[f"workflow_ledger.{field}"] = value
            context = SpanContext(int(record.trace_id, 16), int(record.span_id, 16),
                                  is_remote=False, trace_flags=TraceFlags.SAMPLED)
            parent = (SpanContext(int(record.trace_id, 16), int(record.parent_span_id, 16),
                                  is_remote=True, trace_flags=TraceFlags.SAMPLED)
                      if record.parent_span_id else None)
            status = Status(StatusCode.ERROR) if record.outcome == "error" else Status(StatusCode.UNSET)
            links = []
            for association in associations:
                if (association.measured and association.invocation_trace_id
                        and association.invocation_span_id
                        and (association.invocation_trace_id != record.trace_id
                             or association.invocation_span_id != record.span_id)):
                    links.append(Link(SpanContext(
                        int(association.invocation_trace_id, 16),
                        int(association.invocation_span_id, 16), is_remote=True,
                        trace_flags=TraceFlags.SAMPLED)))
            for span_link in record.links:
                links.append(Link(
                    SpanContext(int(span_link.trace_id, 16), int(span_link.span_id, 16),
                                is_remote=True, trace_flags=TraceFlags.SAMPLED),
                    attributes={"workflow_ledger.relationship": span_link.relationship,
                                "workflow_ledger.link_evidence": span_link.evidence},
                ))
            spans.append(ReadableSpan(
                name=record.name, context=context, parent=parent, resource=resource,
                attributes=attrs, links=links, kind=SpanKind.INTERNAL, status=status,
                start_time=record.start_ns, end_time=record.end_ns,
            ))
        if spans:
            result = trace_exporter.export(spans)
            if result.name == "SUCCESS":
                diagnostics["exported_spans"] = len(spans)
            else:
                diagnostics["trace_export_failures"] += 1
                diagnostics["export_errors"] += 1
                diagnostics["errors"].append("OTLP trace exporter reported failure")

        stage = "metrics"
        reader = InMemoryMetricReader()
        meter_provider = MeterProvider(resource=resource, metric_readers=[reader])
        meter = meter_provider.get_meter("workflow_ledger.otel", "0.2.0")

        def observations(dataset, labels):
            return lambda _options: [Observation(value, attributes=labels(key))
                                     for key, value in dataset.items()]

        meter.create_observable_gauge(
            "workflow_ledger.tokens",
            callbacks=[observations(metric_snapshot["tokens"],
                                    lambda key: {"category": key[0], "quality": key[1]})],
            description="Current export snapshot of known token totals; repeated exports replace the snapshot, they are not additive.",
            unit="{token}",
        )
        meter.create_observable_gauge(
            "workflow_ledger.tokens.by_source",
            callbacks=[observations(metric_snapshot["source_tokens"],
                                    lambda key: {"category": key[0], "source": key[1],
                                                 "quality": key[2], **dict(key[3])})],
            description="Current export snapshot of known tokens attributed to their observed source and explicitly available operation metadata; not additive across exports.",
            unit="{token}",
        )
        meter.create_observable_gauge(
            "workflow_ledger.tokens.by_operation",
            callbacks=[observations(metric_snapshot["grouped_tokens"],
                                    lambda key: {"category": key[0], "quality": key[1],
                                                 **dict(key[2])})],
            description="Current export snapshot of token totals by category, quality, and explicitly available operation metadata; missing run-total metadata remains unattributed.",
            unit="{token}",
        )
        meter.create_observable_gauge(
            "workflow_ledger.token_observations",
            callbacks=[observations(metric_snapshot["coverage"],
                                    lambda key: {"category": key[0], "state": key[1]})],
            description="Current export snapshot of known and unknown token observations by category; repeated exports replace the snapshot.",
            unit="{observation}",
        )
        for name, key, description, unit in (
            ("workflow_ledger.observed_records", "observed",
             "Current export snapshot of observed record rows by kind, outcome, and measured identity dimensions; repeated exports replace the snapshot.", "{record}"),
            ("workflow_ledger.operations", "invocations",
             "Current export snapshot of execution invocations by kind, outcome, and measured identity dimensions; repeated exports replace the snapshot.", "{operation}"),
            ("workflow_ledger.asset_lifecycle", "lifecycle_counts",
             "Current export snapshot of asset lifecycle states; configured and loaded states do not imply execution.", "{observation}"),
            ("workflow_ledger.ancestry_gaps", "ancestry",
             "Current export snapshot of filtered ancestry gaps and unresolved parent links.", "{record}"),
            ("workflow_ledger.duration.sum", "duration_sums",
             "Current export snapshot of known duration sum by kind, outcome, quality, and explicitly observed grouping dimensions; repeated exports replace the snapshot.", "s"),
            ("workflow_ledger.duration.count", "duration_counts",
             "Current export snapshot count of operations with known duration by kind, outcome, quality, and explicitly observed grouping dimensions; repeated exports replace the snapshot.", "{operation}"),
        ):
            meter.create_observable_gauge(
                name,
                callbacks=[observations(metric_snapshot[key], lambda pairs: dict(pairs))],
                description=description,
                unit=unit,
            )
        metric_exporter = OTLPMetricExporter(endpoint=metric_endpoint, timeout=timeout,
                                             headers=explicit_headers)
        metric_data = reader.get_metrics_data()
        if metric_data is not None:
            result = metric_exporter.export(metric_data, timeout_millis=max(1, int(timeout * 1000)))
            if result != MetricExportResult.SUCCESS:
                diagnostics["metric_export_failures"] += 1
                diagnostics["export_errors"] += 1
                diagnostics["errors"].append("OTLP metric exporter reported failure")
    except Exception as exc:  # Export errors must not disrupt local recording.
        diagnostics["export_errors"] += 1
        diagnostics["errors"].append(f"OTel export failed ({type(exc).__name__})")
        if stage == "trace":
            diagnostics["trace_export_failures"] += 1
        else:
            diagnostics["metric_export_failures"] += 1
    finally:
        for exporter in (metric_exporter, trace_exporter):
            if exporter is not None:
                try:
                    exporter.shutdown()
                except Exception as exc:
                    diagnostics["export_errors"] += 1
                    diagnostics["errors"].append(f"OTel exporter shutdown failed ({type(exc).__name__})")
        if meter_provider is not None:
            try:
                meter_provider.shutdown()
            except Exception as exc:
                diagnostics["export_errors"] += 1
                diagnostics["errors"].append(f"OTel metric reader shutdown failed ({type(exc).__name__})")
    return diagnostics
