"""OpenAI Agents SDK adapter.

Importing this module does not change SDK tracing configuration. Call
``install_local_tracing`` only when this process owns the SDK's tracing setup.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
import re
import threading
import time
from typing import Any

from ..model import AssetIdentity, Association, Capabilities, SpanRecord, TokenUsage, stable_id, timestamp_ns

try:  # The SDK remains an optional dependency.
    from agents.tracing import set_trace_processors
    from agents.tracing.processor_interface import TracingProcessor as TraceProcessor
except ImportError:  # pragma: no cover - covered with SDK installed
    set_trace_processors = None

    class TraceProcessor:  # type: ignore[no-redef]
        pass


try:
    SOURCE_VERSION = version("openai-agents")
except PackageNotFoundError:
    SOURCE_VERSION = "unavailable"

CAPABILITIES = Capabilities(
    adapter="openai-agents", version="0.2.0", workflows="direct", agents="direct",
    skills="unavailable", models="direct", tools="direct", durations="direct",
    tokens="direct", limitations=(
        "Skills are not represented by a documented dedicated span type.",
        "Unsupported span kinds are counted in diagnostics and omitted.",
        "Agent, tool, and workflow names are hashed to avoid retaining content.",
    ),
)


def _label(prefix: str, value: Any) -> str:
    """Return a content-free, stable label for a native name."""
    return f"{prefix}:{stable_id('openai-agents-name', str(value), 12)}"


def _safe_model(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", value):
        return None
    return value


def _native_identity(kind: str, value: Any) -> AssetIdentity:
    return AssetIdentity("openai-agents", stable_id(f"openai-agents-{kind}", str(value)), provenance="native")


def _merge_associations(values: tuple[Association, ...]) -> tuple[Association, ...]:
    result = []
    seen = set()
    for association in values:
        key = (association.kind, association.identity.key, association.identity.version,
               association.identity.provenance, association.state, association.evidence,
               association.invocation_trace_id, association.invocation_span_id)
        if key not in seen:
            seen.add(key)
            result.append(association)
    return tuple(result)


def _context_associations(recorder) -> tuple[Association, ...]:
    method = getattr(recorder, "context_associations", None)
    return tuple(method()) if callable(method) else ()


def _policy_allows(recorder, record: SpanRecord) -> bool:
    policy = getattr(recorder, "policy", None)
    return policy.allows(record) if policy is not None else True


class OpenAIAgentsTraceProcessor(TraceProcessor):
    """Convert completed SDK spans into bounded local ``SpanRecord`` values."""

    capabilities = CAPABILITIES
    MAX_CALLBACK_CONVERSION_ERRORS = 1000

    def __init__(self, recorder):
        self.recorder = recorder
        self._lock = threading.Lock()
        self._diagnostics = {"unsupported_span_kinds": 0, "rejected_records": 0,
                             "callback_conversion_errors": 0, "missing_lifecycle": 0,
                             "filtered_records": 0, "disabled": 0}
        self._traces: dict[str, tuple[int, int, str]] = {}
        self._trace_associations: dict[str, Association] = {}
        self._spans: dict[str, tuple[int, int]] = {}
        self._span_trace: dict[str, str] = {}
        self._span_agents: dict[str, Association] = {}
        self._span_associations: dict[str, tuple[Association, ...]] = {}
        self._span_parent: dict[str, str | None] = {}
        self._filtered_workflows: set[str] = set()
        self._filtered_spans: set[str] = set()
        self._enabled = bool(getattr(recorder, "enabled", True))
        self._diagnostics["disabled"] = int(not self._enabled)

    @property
    def diagnostics(self) -> dict[str, int]:
        """Return content-free adapter and recorder diagnostics."""
        with self._lock:
            result = dict(self._diagnostics)
        recorder_diagnostics = getattr(self.recorder, "diagnostics", None)
        delivery_errors = (recorder_diagnostics.get("delivery_errors", 0)
                           if isinstance(recorder_diagnostics, dict) else 0)
        result["delivery_errors"] = (delivery_errors if type(delivery_errors) is int
                                     and delivery_errors >= 0 else 0)
        return result

    def _record_callback_conversion_error(self) -> None:
        with self._lock:
            current = self._diagnostics["callback_conversion_errors"]
            self._diagnostics["callback_conversion_errors"] = min(
                current + 1, self.MAX_CALLBACK_CONVERSION_ERRORS)

    def _increment_diagnostic(self, key: str) -> None:
        with self._lock:
            self._diagnostics[key] += 1

    def on_trace_start(self, trace) -> None:
        if not self._enabled:
            return
        try:
            native_id = getattr(trace, "trace_id", None)
            if not isinstance(native_id, str) or not native_id:
                return
            with self._lock:
                self._traces[native_id] = (time.time_ns(), time.monotonic_ns(),
                                           str(getattr(trace, "name", "workflow")))
            identity = AssetIdentity("openai-agents", stable_id("openai-agents-workflow-asset", str(getattr(trace, "name", "workflow"))),
                                     provenance="native")
            trace_hash = stable_id("openai-agents-trace", native_id)
            workflow_span_id = stable_id("openai-agents-workflow", native_id, 16)
            workflow_association = Association("workflow", identity, "invoked", "native",
                                               workflow_span_id, trace_hash)
            with self._lock:
                self._trace_associations[native_id] = workflow_association
            candidate = SpanRecord(trace_id=stable_id("openai-agents-trace", native_id),
                span_id=workflow_span_id, kind="workflow", name="workflow", identity=identity,
                associations=(workflow_association,), lifecycle="invoked", start_ns=time.time_ns(),
                harness="openai-agents", adapter="openai-agents")
            if not _policy_allows(self.recorder, candidate):
                self._filtered_workflows.add(native_id)
                self._increment_diagnostic("filtered_records")
            self._emit(candidate)
        except Exception:
            self._record_callback_conversion_error()
            return

    def on_trace_end(self, trace) -> None:
        if not self._enabled:
            return
        try:
            native_id = getattr(trace, "trace_id", None)
            if not isinstance(native_id, str) or not native_id:
                return
            with self._lock:
                timing = self._traces.pop(native_id, None)
                workflow_association = self._trace_associations.pop(native_id, None)
                stale_spans = [key for key, trace_id in self._span_trace.items() if trace_id == native_id]
                for key in stale_spans:
                    self._span_trace.pop(key, None)
                    self._spans.pop(key, None)
                    self._span_agents.pop(key, None)
                    self._span_associations.pop(key, None)
                    self._span_parent.pop(key, None)
                    self._filtered_spans.discard(key)
            now_mono, now_wall = time.monotonic_ns(), time.time_ns()
            start_ns = timing[0] if timing else None
            end_ns = start_ns + (now_mono - timing[1]) if timing else now_wall
            trace_id = stable_id("openai-agents-trace", native_id)
            name = _label("workflow", timing[2] if timing else "workflow")
            workflow_identity = AssetIdentity("openai-agents", stable_id("openai-agents-workflow-asset", timing[2] if timing else "workflow"),
                                              provenance="native")
            if workflow_association is None:
                workflow_association = Association("workflow", workflow_identity, "invoked", "native",
                                                    stable_id("openai-agents-workflow", native_id, 16), trace_id)
            workflow_record = SpanRecord(trace_id=trace_id, span_id=stable_id("openai-agents-workflow", native_id, 16),
                                  kind="workflow", name=name, harness="openai-agents", adapter="openai-agents",
                                  identity=workflow_identity,
                                  associations=(workflow_association,), lifecycle="completed",
                                  start_ns=start_ns, end_ns=end_ns,
                                  duration_quality="direct" if start_ns is not None else "unavailable",
                                  outcome="unknown", source_version=SOURCE_VERSION)
            self._emit(workflow_record)
            self._filtered_workflows.discard(native_id)
        except Exception:
            self._record_callback_conversion_error()
            return

    def on_span_start(self, span) -> None:
        if not self._enabled:
            return
        try:
            native_id = getattr(span, "span_id", None)
            if isinstance(native_id, str) and native_id:
                trace_id = getattr(span, "trace_id", None)
                parent_native = getattr(span, "parent_id", None)
                data = getattr(span, "span_data", None)
                data_type = getattr(data, "type", None)
                with self._lock:
                    self._spans[native_id] = (time.time_ns(), time.monotonic_ns())
                    if isinstance(trace_id, str):
                        self._span_trace[native_id] = trace_id
                    self._span_parent[native_id] = parent_native if isinstance(parent_native, str) else None
                    inherited = self._span_agents.get(parent_native) if isinstance(parent_native, str) else None
                    if data_type == "agent":
                        asset = _native_identity("agent", getattr(data, "name", None) or "agent")
                        inherited = Association("agent", asset, "invoked", "native",
                                                stable_id("openai-agents-span", native_id, 16),
                                                stable_id("openai-agents-trace", trace_id)
                                                if isinstance(trace_id, str) else None)
                    if inherited:
                        self._span_agents[native_id] = inherited
                    associations = (self._span_associations.get(parent_native, ())
                                    if isinstance(parent_native, str) else ())
                    if parent_native is None and isinstance(trace_id, str):
                        associations += ((self._trace_associations.get(trace_id),)
                                         if self._trace_associations.get(trace_id) is not None else ())
                    if data_type == "agent" and inherited is not None:
                        associations = tuple(a for a in associations if not (a.kind == "agent" and a.measured))
                        associations += (inherited,)
                    elif data_type == "function":
                        identity = _native_identity("tool", getattr(data, "name", None) or "tool")
                        associations += (Association("tool", identity, "invoked", "native",
                            stable_id("openai-agents-span", native_id, 16),
                            stable_id("openai-agents-trace", trace_id) if isinstance(trace_id, str) else None),)
                    associations = _merge_associations(associations + _context_associations(self.recorder))
                    if associations:
                        self._span_associations[native_id] = associations
                    kind = ("agent" if data_type == "agent" else "tool" if data_type == "function"
                            else "model" if data_type in {"generation", "response"} else None)
                    identity = None
                    if kind == "agent":
                        identity = _native_identity("agent", getattr(data, "name", None) or "agent")
                        name = f"agent:{identity.logical_id[:12]}"
                    elif kind == "tool":
                        identity = _native_identity("tool", getattr(data, "name", None) or "tool")
                        name = _label("tool", getattr(data, "name", None) or "tool")
                    elif kind == "model":
                        response = getattr(data, "response", None) if data_type == "response" else None
                        model = _safe_model(getattr(data, "model", None) or getattr(response, "model", None))
                        identity = _native_identity("model", model) if model else None
                        name = "model_call"
                    if kind and isinstance(trace_id, str):
                        candidate = SpanRecord(trace_id=stable_id("openai-agents-trace", trace_id),
                            span_id=stable_id("openai-agents-span", native_id, 16), kind=kind, name=name,
                            identity=identity, harness="openai-agents", adapter="openai-agents",
                            lifecycle="invoked", start_ns=timestamp_ns(getattr(span, "started_at", None)),
                            parent_span_id=(stable_id("openai-agents-span", parent_native, 16)
                                            if isinstance(parent_native, str) else stable_id("openai-agents-workflow", trace_id, 16)),
                            associations=associations, filtered_parent=(parent_native in self._filtered_spans
                                if isinstance(parent_native, str) else trace_id in self._filtered_workflows))
                        if identity is not None:
                            associations = _merge_associations(associations + (Association(
                                kind, identity, "invoked", "native", candidate.span_id, candidate.trace_id),))
                            candidate = replace(candidate, associations=associations)
                            self._span_associations[native_id] = associations
                        if not _policy_allows(self.recorder, candidate):
                            self._filtered_spans.add(native_id)
                            self._diagnostics["filtered_records"] += 1
                        self._emit(candidate)
        except Exception:
            self._record_callback_conversion_error()
            return

    def on_span_end(self, span) -> None:
        if not self._enabled:
            return
        try:
            record = self._record(span)
            if record is not None:
                if not _policy_allows(self.recorder, record):
                    self._increment_diagnostic("filtered_records")
                self._emit(record)
        except Exception:
            self._increment_diagnostic("rejected_records")
            self._record_callback_conversion_error()

    def _record(self, span) -> SpanRecord | None:
        native_span_id = getattr(span, "span_id", None)
        native_trace_id = getattr(span, "trace_id", None)
        data = getattr(span, "span_data", None)
        if not all(isinstance(v, str) and v for v in (native_span_id, native_trace_id)) or data is None:
            return None
        data_type = getattr(data, "type", None)
        if data_type == "agent":
            kind = "agent"
            identity = _native_identity("agent", getattr(data, "name", None) or "agent")
            agent_name = f"agent:{identity.logical_id[:12]}"
            name = agent_name
        elif data_type in {"generation", "response"}:
            kind, name, agent_name = "model", "model_call", None
        elif data_type == "function":
            kind = "tool"
            source_name = getattr(data, "name", None) or "tool"
            identity = _native_identity("tool", source_name)
            name = _label("tool", source_name)
            agent_name = None
        else:
            with self._lock:
                self._spans.pop(native_span_id, None)
            self._increment_diagnostic("unsupported_span_kinds")
            return None

        trace_id = stable_id("openai-agents-trace", native_trace_id)
        span_id = stable_id("openai-agents-span", native_span_id, 16)
        parent_native = getattr(span, "parent_id", None)
        if isinstance(parent_native, str) and parent_native:
            parent = stable_id("openai-agents-span", parent_native, 16)
        else:
            parent = stable_id("openai-agents-workflow", native_trace_id, 16)
        with self._lock:
            timing = self._spans.pop(native_span_id, None)
            inherited_agent = self._span_agents.get(native_span_id)
            inherited_associations = self._span_associations.get(native_span_id, ())
            filtered_parent = (parent_native in self._filtered_spans if isinstance(parent_native, str)
                               else native_trace_id in self._filtered_workflows)
        if timing is None:
            self._increment_diagnostic("missing_lifecycle")
        start = timestamp_ns(getattr(span, "started_at", None))
        end = timestamp_ns(getattr(span, "ended_at", None))
        if start is None and timing:
            start = timing[0]
        if end is None and timing:
            end = start + (time.monotonic_ns() - timing[1]) if start is not None else None
        duration_quality = "direct" if start is not None and end is not None else "unavailable"

        error = getattr(span, "error", None)
        outcome = "error" if error is not None else "unknown"
        error_type = None
        if error is not None:
            # Never read error messages or serialized error data.
            raw_type = error.get("type") if isinstance(error, dict) else getattr(error, "type", None)
            if isinstance(raw_type, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,100}", raw_type):
                error_type = raw_type
            elif not isinstance(error, dict):
                error_type = type(error).__name__

        model, usage = None, None
        if kind == "model":
            response = getattr(data, "response", None) if data_type == "response" else None
            model = _safe_model(getattr(data, "model", None) or getattr(response, "model", None))
            identity = _native_identity("model", model) if model else None
            raw_usage = getattr(data, "usage", None)
            if response is not None and raw_usage is None:
                raw_usage = getattr(response, "usage", None)
            if raw_usage is not None:
                input_tokens = _usage_value(raw_usage, "input_tokens")
                output_tokens = _usage_value(raw_usage, "output_tokens")
                details = _usage_get(raw_usage, "input_tokens_details")
                cached = _usage_value(details, "cached_tokens")
                output_details = _usage_get(raw_usage, "output_tokens_details")
                reasoning = _usage_value(output_details, "reasoning_tokens")
                # Keep subset invariants; never invent values for missing counters.
                if cached is not None and (input_tokens is None or cached > input_tokens):
                    cached = None
                if reasoning is not None and (output_tokens is None or reasoning > output_tokens):
                    reasoning = None
                if any(v is not None for v in (input_tokens, cached, output_tokens, reasoning)):
                    usage = TokenUsage(input_tokens=input_tokens, cached_input_tokens=cached,
                                       output_tokens=output_tokens, reasoning_tokens=reasoning,
                                       scope="model_call", source="openai-agents")

        associations = _merge_associations(inherited_associations + _context_associations(self.recorder))
        if identity is not None:
            associations = _merge_associations(associations + (
                Association(kind, identity, "invoked", "native", span_id, trace_id),))
        if kind == "agent" and inherited_agent is not None:
            associations = tuple(a for a in associations if a.kind != "agent") + (
                Association("agent", identity, "invoked", "native", span_id, trace_id),)
        return SpanRecord(trace_id=trace_id, span_id=span_id, parent_span_id=parent, kind=kind,
                          name=name, harness="openai-agents", adapter="openai-agents",
                          start_ns=start, end_ns=end, duration_quality=duration_quality,
                          outcome=outcome, usage=usage, agent_name=agent_name or (
                              f"agent:{inherited_agent.identity.logical_id[:12]}" if inherited_agent else None), model=model,
                          error_type=error_type, source_version=SOURCE_VERSION, identity=identity,
                          associations=associations, lifecycle="failed" if outcome == "error" else "completed",
                          filtered_parent=filtered_parent)

    def _emit(self, record: SpanRecord) -> None:
        try:
            self.recorder.emit(record)
        except Exception:
            # Adapter callbacks must not alter workflow execution.
            pass

    def force_flush(self) -> None:
        return None

    def shutdown(self) -> None:
        with self._lock:
            self._traces.clear()
            self._trace_associations.clear()
            self._spans.clear()
            self._span_trace.clear()
            self._span_agents.clear()
            self._span_associations.clear()
            self._span_parent.clear()
            self._filtered_workflows.clear()


def _int_or_none(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _usage_get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _usage_value(value: Any, key: str) -> int | None:
    return _int_or_none(_usage_get(value, key))


def install_local_tracing(recorder):
    """Replace SDK processors with this local processor.

    Call before creating traces. This replaces the SDK's global processor list,
    including any default remote exporter; caller must own tracing configuration.
    """
    processor = OpenAIAgentsTraceProcessor(recorder)
    if not processor._enabled:
        return processor
    if set_trace_processors is None:
        raise ImportError("Install the optional dependency with: pip install workflow-ledger[agents]")
    set_trace_processors([processor])
    return processor


@contextmanager
def bind_current_sdk_context(recorder, processor: OpenAIAgentsTraceProcessor):
    """Correlate explicit Recorder helpers with the current SDK trace/span."""
    if not getattr(recorder, "enabled", True):
        yield
        return
    try:
        from agents.tracing import get_current_span, get_current_trace
    except ImportError:
        yield
        return
    trace = get_current_trace()
    span = get_current_span()
    native_trace = getattr(trace, "trace_id", None)
    if not isinstance(native_trace, str):
        yield
        return
    native_parent = getattr(span, "span_id", None)
    parent_id = (stable_id("openai-agents-span", native_parent, 16)
                 if isinstance(native_parent, str)
                 else stable_id("openai-agents-workflow", native_trace, 16))
    associations = _context_associations(recorder)
    selected = True
    if isinstance(native_parent, str):
        with processor._lock:
            associations += processor._span_associations.get(native_parent, ())
            selected = native_parent not in processor._filtered_spans
    else:
        with processor._lock:
            workflow_association = processor._trace_associations.get(native_trace)
            if workflow_association is not None:
                associations += (workflow_association,)
            selected = native_trace not in processor._filtered_workflows
    with recorder.bind(stable_id("openai-agents-trace", native_trace), parent_id,
                       associations=_merge_associations(associations), selected=selected):
        yield
