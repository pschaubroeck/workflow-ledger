from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from workflow_ledger.model import Association, AssetIdentity, SpanLink, SpanRecord, TokenUsage
from workflow_ledger.otel import export_records


TRACE_ID = "0123456789abcdef0123456789abcdef"
ROOT_ID = "0123456789abcdef"
CHILD_ID = "fedcba9876543210"


def record(span_id=ROOT_ID, parent=None, **kwargs):
    data = dict(trace_id=TRACE_ID, span_id=span_id, kind="workflow", name="workflow",
                harness="fixture", adapter="fixture-adapter", parent_span_id=parent,
                start_ns=1_700_000_000_000_000_000,
                end_ns=1_700_000_001_000_000_000,
                duration_quality="direct", outcome="ok", lifecycle="completed")
    data.update(kwargs)
    return SpanRecord(**data)


class Collector(BaseHTTPRequestHandler):
    requests = []
    fail_paths = set()

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.requests.append((self.path, body, dict(self.headers)))
        self.send_response(500 if self.path in self.fail_paths else 200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()

    def log_message(self, *_args):
        pass


def collector():
    Collector.requests = []
    Collector.fail_paths = set()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Collector)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_exports_actual_otlp_trace_and_metric_protobuf_with_original_ids(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=secret")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "deployment.environment=ambient")
    server, endpoint = collector()
    try:
        root = record(usage=TokenUsage(input_tokens=30, scope="run",
                                       source="explicit"))
        child = record(span_id=CHILD_ID, parent=ROOT_ID, kind="model", name="model call",
                       model="model-x", usage=TokenUsage(input_tokens=11, output_tokens=3,
                                                          scope="model_call", source="openai-agents"))
        diagnostics = export_records([root, child], endpoint, timeout=2)
    finally:
        server.shutdown()
        server.server_close()

    assert diagnostics["exported_spans"] == 2
    paths = [request[0] for request in Collector.requests]
    traces = ExportTraceServiceRequest.FromString(
        next(body for path, body, _headers in Collector.requests if path == "/v1/traces"))
    spans = [span for resource in traces.resource_spans for scope in resource.scope_spans
             for span in scope.spans]
    assert {span.span_id for span in spans} == {bytes.fromhex(ROOT_ID), bytes.fromhex(CHILD_ID)}
    assert all(span.trace_id == bytes.fromhex(TRACE_ID) for span in spans)
    child_proto = next(span for span in spans if span.span_id == bytes.fromhex(CHILD_ID))
    assert child_proto.parent_span_id == bytes.fromhex(ROOT_ID)
    assert child_proto.start_time_unix_nano == 1_700_000_000_000_000_000
    assert child_proto.end_time_unix_nano == 1_700_000_001_000_000_000
    assert b"model-x" in child_proto.SerializeToString()

    metrics = ExportMetricsServiceRequest.FromString(
        next(body for path, body, _headers in Collector.requests if path == "/v1/metrics"))
    observed = {}
    for resource in metrics.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                observed[metric.name] = metric
    assert {"workflow_ledger.tokens", "workflow_ledger.operations",
            "workflow_ledger.duration.sum", "workflow_ledger.duration.count"} <= set(observed)
    token_points = [point for metric in observed["workflow_ledger.tokens"].gauge.data_points
                    for point in [metric]]
    source_points = observed["workflow_ledger.tokens.by_source"].gauge.data_points
    sources = {kv.value.string_value for point in source_points for kv in point.attributes
               if kv.key == "source"}
    assert sources == {"explicit", "openai-agents"}
    # The workflow's run scope wins per known field; model-call input is not double counted.
    input_points = [point for point in token_points
                    if any(kv.key == "category" and kv.value.string_value == "input_tokens"
                           for kv in point.attributes)]
    assert sum(point.as_int for point in input_points) == 30
    assert not any(kv.key in {"trace_id", "span_id"} for point in token_points
                   for kv in point.attributes)
    assert paths.count("/v1/metrics") == 1
    assert paths.count("/v1/traces") == 1
    resource_attrs = {kv.key: kv.value.string_value
                      for item in metrics.resource_metrics for kv in item.resource.attributes}
    assert resource_attrs == {"service.name": "workflow-ledger"}
    assert all("Authorization" not in headers for _path, _body, headers in Collector.requests)


def test_legacy_unknown_source_is_normalized_before_otlp_export():
    legacy_data = record(span_id=CHILD_ID, parent=ROOT_ID, kind="model",
                         usage=TokenUsage(input_tokens=12, scope="model_call")).to_dict()
    legacy_data["usage"]["source"] = "private provider payload"
    legacy = SpanRecord.from_dict(legacy_data)
    server, endpoint = collector()
    try:
        diagnostics = export_records([legacy], endpoint, timeout=2)
    finally:
        server.shutdown()
        server.server_close()

    assert diagnostics["export_errors"] == 0
    trace_payload = next(body for path, body, _headers in Collector.requests if path == "/v1/traces")
    traces = ExportTraceServiceRequest.FromString(trace_payload)
    spans = [span for resource in traces.resource_spans for scope in resource.scope_spans
             for span in scope.spans]
    attributes = {item.key: item.value.string_value for item in spans[0].attributes
                  if item.value.WhichOneof("value") == "string_value"}
    assert attributes["workflow_ledger.token_source"] == "other"
    assert b"private provider payload" not in trace_payload

    metric_payload = next(body for path, body, _headers in Collector.requests if path == "/v1/metrics")
    metrics = ExportMetricsServiceRequest.FromString(metric_payload)
    source_values = {
        kv.value.string_value
        for resource in metrics.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics if metric.name == "workflow_ledger.tokens.by_source"
        for point in metric.gauge.data_points
        for kv in point.attributes if kv.key == "source"
    }
    assert source_values == {"other"}
    assert b"private provider payload" not in metric_payload


def test_otlp_coverage_includes_model_calls_without_usage():
    server, endpoint = collector()
    try:
        known = record(span_id=CHILD_ID, parent=ROOT_ID, kind="model",
                       usage=TokenUsage(input_tokens=10))
        missing = record(span_id="1" * 16, parent=ROOT_ID, kind="model")
        diagnostics = export_records([record(), known, missing], endpoint, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    assert diagnostics["export_errors"] == 0
    metrics = ExportMetricsServiceRequest.FromString(
        next(body for path, body, _ in Collector.requests if path == "/v1/metrics"))
    points = [point for resource in metrics.resource_metrics for scope in resource.scope_metrics
              for metric in scope.metrics if metric.name == "workflow_ledger.token_observations"
              for point in metric.gauge.data_points]
    coverage = {attrs["state"]: point.as_int for point in points
                if (attrs := {kv.key: kv.value.string_value for kv in point.attributes})["category"] == "input_tokens"}
    assert coverage == {"known": 1, "unknown": 1}


def test_disabled_and_invalid_endpoint_do_not_make_requests():
    disabled = export_records([record()], None)
    invalid = export_records([record()], "localhost:4318")
    assert disabled["enabled"] is False
    assert invalid["enabled"] is False
    assert invalid["errors"]


def test_incomplete_span_is_skipped_without_fabricated_timing():
    server, endpoint = collector()
    try:
        partial = record(start_ns=None, end_ns=None, duration_quality="unavailable")
        diagnostics = export_records([partial], endpoint, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    assert diagnostics["skipped_incomplete_spans"] == 1
    assert "/v1/traces" not in [request[0] for request in Collector.requests]


def test_export_failure_is_reported_and_does_not_raise():
    diagnostics = export_records([record()], "http://127.0.0.1:1", timeout=0.2)
    assert diagnostics["export_errors"] > 0
    assert diagnostics["trace_export_failures"] or diagnostics["metric_export_failures"]


def test_metric_http_failure_is_reported_once_without_retrying_snapshot():
    server, endpoint = collector()
    Collector.fail_paths = {"/v1/metrics"}
    try:
        diagnostics = export_records([record()], endpoint, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    assert diagnostics["exported_spans"] == 1
    assert diagnostics["metric_export_failures"] == 1
    assert diagnostics["export_errors"] >= 1
    assert [request[0] for request in Collector.requests].count("/v1/metrics") == 1


def test_endpoint_rejects_credentials_query_and_fragment():
    for endpoint in ("http://user:secret@localhost", "http://localhost?token=x",
                     "http://localhost/#fragment"):
        diagnostics = export_records([], endpoint)
        assert diagnostics["export_errors"] == 1
        assert diagnostics["errors"] == ["endpoint or timeout configuration is invalid"]


def test_only_explicit_headers_are_sent_and_secrets_are_not_exported_as_data():
    server, endpoint = collector()
    secret = "Bearer synthetic-test-secret"
    try:
        diagnostics = export_records([record()], endpoint, timeout=2,
                                     headers={"Authorization": secret})
    finally:
        server.shutdown()
        server.server_close()
    assert diagnostics["exported_spans"] == 1
    assert secret not in repr(diagnostics)
    for _path, body, received_headers in Collector.requests:
        assert next(value for key, value in received_headers.items()
                    if key.lower() == "authorization") == secret
        assert secret.encode() not in body
    invalid = export_records([], endpoint, headers={"Authorization": "valid\r\nX-Evil: yes"})
    assert invalid["export_errors"] == 1
    assert "X-Evil" not in repr(invalid)


def test_combined_trace_failure_and_metric_exception_are_classified_by_stage(monkeypatch):
    from opentelemetry.exporter.otlp.proto.http import metric_exporter

    server, endpoint = collector()
    Collector.fail_paths = {"/v1/traces"}

    class FailedMetricExporter:
        def __init__(self, **_kwargs):
            raise RuntimeError("synthetic secret should not be exposed")

    monkeypatch.setattr(metric_exporter, "OTLPMetricExporter", FailedMetricExporter)
    try:
        diagnostics = export_records([record()], endpoint, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    assert diagnostics["trace_export_failures"] == 1
    assert diagnostics["metric_export_failures"] == 1
    assert diagnostics["export_errors"] == 2
    assert "synthetic secret" not in repr(diagnostics)


def test_duration_snapshot_omits_unavailable_and_keeps_quality_dimension():
    server, endpoint = collector()
    try:
        direct = record(span_id=ROOT_ID, duration_quality="direct")
        unavailable = record(span_id=CHILD_ID, kind="tool", duration_quality="unavailable",
                             start_ns=None, end_ns=None)
        export_records([direct, unavailable], endpoint, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    payload = next(body for path, body, _headers in Collector.requests if path == "/v1/metrics")
    metrics = ExportMetricsServiceRequest.FromString(payload)
    found = {}
    for resource in metrics.resource_metrics:
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                found[metric.name] = metric
    duration = found["workflow_ledger.duration.sum"].gauge.data_points
    assert len(duration) == 1
    attrs = {kv.key: kv.value.string_value for kv in duration[0].attributes
             if kv.value.WhichOneof("value") == "string_value"}
    assert attrs["quality"] == "direct"


def test_otlp_uses_measured_identity_links_and_not_flat_context_labels():
    server, endpoint = collector()
    try:
        skill_id = AssetIdentity("skills", "writer", "3.2", "native")
        skill_invocation = record(trace_id="a" * 32, span_id="b" * 16, kind="skill",
                                  name="writer invocation", identity=skill_id)
        child = record(span_id=CHILD_ID, parent=ROOT_ID, kind="model", name="call",
                       model="provider-x", skill_name="flat-legacy-name",
                       usage=TokenUsage(input_tokens=12, scope="model_call"),
                       associations=(Association("skill", skill_id, "invoked", "native",
                                                  skill_invocation.span_id,
                                                  skill_invocation.trace_id),))
        wait = record(span_id="c" * 16, parent=ROOT_ID, kind="wait", name="dependency wait",
                      links=(SpanLink("waits_for", skill_invocation.trace_id,
                                      skill_invocation.span_id),))
        diagnostics = export_records([record(), skill_invocation, child, wait], endpoint, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    assert diagnostics["exported_spans"] == 4
    trace_payload = next(body for path, body, _headers in Collector.requests if path == "/v1/traces")
    traces = ExportTraceServiceRequest.FromString(trace_payload)
    spans = [span for resource in traces.resource_spans for scope in resource.scope_spans
             for span in scope.spans]
    model_span = next(span for span in spans if span.span_id == bytes.fromhex(CHILD_ID))
    attrs = {item.key: item.value for item in model_span.attributes}
    assert attrs["workflow_ledger.association_assets"].array_value.values[0].string_value == "skills:writer@3.2"
    assert attrs["workflow_ledger.association_versions"].array_value.values[0].string_value == "3.2"
    assert attrs["workflow_ledger.association_provenance"].array_value.values[0].string_value == "native"
    assert attrs["workflow_ledger.association_evidence"].array_value.values[0].string_value == "native"
    assert "workflow_ledger.skill" not in attrs
    assert "flat-legacy-name" not in model_span.SerializeToString().decode("latin1")
    assert len(model_span.links) == 1
    assert model_span.links[0].trace_id == bytes.fromhex(skill_invocation.trace_id)
    assert model_span.links[0].span_id == bytes.fromhex(skill_invocation.span_id)
    wait_span = next(span for span in spans if span.span_id == bytes.fromhex("c" * 16))
    assert len(wait_span.links) == 1
    assert wait_span.links[0].trace_id == bytes.fromhex(skill_invocation.trace_id)
    assert wait_span.links[0].span_id == bytes.fromhex(skill_invocation.span_id)
    wait_link_attrs = {item.key: item.value for item in wait_span.links[0].attributes}
    assert wait_link_attrs["workflow_ledger.relationship"].string_value == "waits_for"
    assert wait_link_attrs["workflow_ledger.link_evidence"].string_value == "caller_declared"

    metric_payload = next(body for path, body, _headers in Collector.requests if path == "/v1/metrics")
    metrics = ExportMetricsServiceRequest.FromString(metric_payload)
    metric_by_name = {metric.name: metric for resource in metrics.resource_metrics
                      for scope in resource.scope_metrics for metric in scope.metrics}
    grouped = metric_by_name["workflow_ledger.tokens.by_operation"].gauge.data_points
    assert any(any(kv.key == "skill_identity" and kv.value.string_value == "skills:writer@3.2"
                   for kv in point.attributes) for point in grouped)


def test_loaded_lifecycle_emits_metric_without_execution_span():
    server, endpoint = collector()
    try:
        loaded = record(kind="skill", span_id=CHILD_ID, start_ns=None, end_ns=None,
                        duration_quality="unavailable", lifecycle="loaded",
                        outcome="unknown",
                        identity=AssetIdentity("skills", "only-loaded", None, "native"))
        diagnostics = export_records([loaded], endpoint, timeout=2)
    finally:
        server.shutdown()
        server.server_close()
    assert diagnostics["exported_spans"] == 0
    assert diagnostics["skipped_non_execution_spans"] == 1
    assert "/v1/traces" not in [path for path, _body, _headers in Collector.requests]
    payload = next(body for path, body, _headers in Collector.requests if path == "/v1/metrics")
    metrics = ExportMetricsServiceRequest.FromString(payload)
    lifecycle = next(metric for resource in metrics.resource_metrics for scope in resource.scope_metrics
                    for metric in scope.metrics if metric.name == "workflow_ledger.asset_lifecycle")
    attrs = {kv.key: kv.value.string_value for kv in lifecycle.gauge.data_points[0].attributes
             if kv.value.WhichOneof("value") == "string_value"}
    assert attrs["state"] == "loaded"
