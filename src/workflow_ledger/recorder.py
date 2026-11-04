"""Explicit instrumentation with selective attachment and source-neutral context."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import secrets
import time
from typing import Protocol
import uuid

from .model import (AssetIdentity, Association, Capabilities, EventRecord, Kind, Severity,
                    SpanLink, SpanRecord, TokenUsage)
from .policy import CollectionPolicy


class Sink(Protocol):
    def emit(self, record: SpanRecord) -> bool: ...


class EventSink(Protocol):
    def emit(self, record: EventRecord) -> bool: ...


@dataclass
class Activity:
    usage: TokenUsage | None = None
    model: str | None = None
    trace_id: str | None = None
    span_id: str | None = None


@dataclass(frozen=True)
class InvocationContext:
    trace_id: str
    span_id: str | None
    associations: tuple[Association, ...] = ()
    selected: bool = True


class Recorder:
    capabilities = Capabilities(
        "explicit", "0.2.0", "direct", "direct", "direct", "direct", "direct", "direct",
        "direct", ("Only explicitly instrumented operations and supplied usage are observed.",
                   "Configured/loaded assets do not establish invocation or usage ownership."),
    )

    def __init__(self, sink: Sink, harness: str = "explicit", enabled: bool = True,
                 policy: CollectionPolicy | None = None, event_sink: EventSink | None = None):
        self.sink = sink
        self.event_sink = event_sink
        self.harness = harness
        self.policy = policy if policy is not None else CollectionPolicy()
        self.enabled = enabled and self.policy.may_observe_runtime(harness)
        self.diagnostics = {"rejected_records": 0, "delivery_errors": 0, "filtered_records": 0}
        self._context: ContextVar[InvocationContext | None] = ContextVar(
            f"observer_{id(self)}", default=None)

    def emit(self, record: SpanRecord) -> bool:
        if not self.enabled:
            return True
        try:
            if not self.policy.allows(record):
                self.diagnostics["filtered_records"] += 1
                return True
            if self.sink.emit(record):
                return True
        except Exception:
            pass
        self.diagnostics["delivery_errors"] += 1
        return False

    def emit_event(self, event_name: str, severity: Severity, *, body: str | None = None,
                   attributes: dict | None = None) -> bool:
        """Record one structured event, correlated to the current explicit span when present."""
        if not self.enabled:
            return True
        if self.event_sink is None:
            self.diagnostics["delivery_errors"] += 1
            return False
        context = self._context.get()
        now_ns = time.time_ns()
        try:
            event = EventRecord(
                event_id=uuid.uuid4().hex,
                event_name=event_name,
                severity_text=severity,
                timestamp_ns=now_ns,
                observed_timestamp_ns=now_ns,
                harness=self.harness,
                body=body,
                attributes=attributes if attributes is not None else {},
                trace_id=context.trace_id if context else None,
                span_id=context.span_id if context else None,
                associations=context.associations if context else (),
            )
            if not self.policy.allows_event(event):
                self.diagnostics["filtered_records"] += 1
                return True
            if self.event_sink.emit(event):
                return True
        except (ValueError, TypeError, AttributeError):
            self.diagnostics["rejected_records"] += 1
            return False
        except Exception:
            pass
        self.diagnostics["delivery_errors"] += 1
        return False

    def context_associations(self) -> tuple[Association, ...]:
        context = self._context.get()
        return context.associations if context else ()

    @contextmanager
    def bind(self, trace_id: str, parent_span_id: str | None = None, *,
             associations: tuple[Association, ...] = (), selected: bool = True):
        """An adapter may attach explicit helpers to supported native trace context."""
        if not self.enabled:
            yield
            return
        SpanRecord(trace_id=trace_id, span_id=secrets.token_hex(8), kind="workflow",
                   name="context", harness=self.harness, adapter="explicit",
                   parent_span_id=parent_span_id, associations=associations)
        token = self._context.set(InvocationContext(trace_id, parent_span_id, associations, selected))
        try:
            yield
        finally:
            self._context.reset(token)

    @contextmanager
    def associate(self, *associations: Association):
        """Carry contextual evidence; loading/configuration is not execution."""
        if not self.enabled:
            yield
            return
        parent = self._context.get()
        combined = (parent.associations if parent else ()) + tuple(associations)
        with self.bind(parent.trace_id if parent else secrets.token_hex(16),
                       parent.span_id if parent else None, associations=combined,
                       selected=parent.selected if parent else True):
            yield

    def observe(self, kind: Kind, identity: AssetIdentity, state: str, *, name: str | None = None):
        """Record configuration/loading evidence without duration, outcome, or usage."""
        if not self.enabled:
            return
        parent = self._context.get()
        try:
            if state not in {"configured", "loaded"}:
                raise ValueError("observe accepts configuration/loading only")
            self.emit(SpanRecord(
                trace_id=parent.trace_id if parent else secrets.token_hex(16),
                span_id=secrets.token_hex(8), parent_span_id=parent.span_id if parent else None,
                kind=kind, name=name or identity.logical_id, identity=identity,
                associations=parent.associations if parent else (), lifecycle=state,
                filtered_parent=bool(parent and not parent.selected), start_ns=time.time_ns(),
                harness=self.harness, adapter="explicit",
            ))
        except (ValueError, TypeError, AttributeError):
            self.diagnostics["rejected_records"] += 1

    @contextmanager
    def span(self, kind: Kind, name: str, *, identity: AssetIdentity | None = None,
             agent_name: str | None = None, skill_name: str | None = None,
             skill_version: str | None = None, links: tuple[SpanLink, ...] = ()):
        activity = Activity()
        if not self.enabled:
            yield activity
            return
        parent = self._context.get()
        trace_id = parent.trace_id if parent else secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        activity.trace_id, activity.span_id = trace_id, span_id
        try:
            asset = identity or AssetIdentity("caller", name,
                                             skill_version if kind == "skill" else None)
            associations = parent.associations if parent else ()
            if kind == "agent":
                associations = tuple(a for a in associations if not (a.kind == "agent" and a.measured))
            own = Association(kind, asset, "invoked", "caller_declared", span_id, trace_id)
            associations += (own,)
            for contextual_kind, label in (("agent", agent_name), ("skill", skill_name)):
                if label and not any(a.kind == contextual_kind and a.identity.logical_id == label
                                     for a in associations):
                    associations += (Association(contextual_kind, AssetIdentity("caller", label),
                                                 "loaded", "contextual"),)
            candidate = SpanRecord(
                trace_id=trace_id, span_id=span_id, parent_span_id=parent.span_id if parent else None,
                kind=kind, name=name, identity=asset, associations=associations, lifecycle="invoked",
                links=links, harness=self.harness, adapter="explicit",
                filtered_parent=bool(parent and not parent.selected),
            )
            selected = self.policy.allows(candidate)
        except (ValueError, TypeError):
            self.diagnostics["rejected_records"] += 1
            yield activity
            return
        token = self._context.set(InvocationContext(trace_id, span_id, associations, selected))
        start, monotonic_start = time.time_ns(), time.monotonic_ns()
        self.emit(replace(candidate, start_ns=start))
        outcome, lifecycle, error_type = "ok", "completed", None
        try:
            yield activity
        except BaseException as exc:
            outcome = "cancelled" if isinstance(exc, (KeyboardInterrupt, SystemExit)) or (
                type(exc).__name__ == "CancelledError"
            ) else "error"
            lifecycle = "cancelled" if outcome == "cancelled" else "failed"
            error_type = type(exc).__name__
            raise
        finally:
            end = start + (time.monotonic_ns() - monotonic_start)
            self._context.reset(token)
            agents = [a for a in associations if a.kind == "agent" and a.measured]
            skills = [a for a in associations if a.kind == "skill" and a.measured]
            try:
                self.emit(replace(candidate, start_ns=start, end_ns=end, duration_quality="direct",
                    outcome=outcome, lifecycle=lifecycle, error_type=error_type,
                    usage=activity.usage, model=activity.model,
                    agent_name=agents[-1].identity.logical_id if agents else agent_name,
                    skill_name=skills[-1].identity.logical_id if len(skills) == 1 else skill_name,
                    skill_version=skills[-1].identity.version if len(skills) == 1 else None))
            except (ValueError, TypeError):
                self.diagnostics["rejected_records"] += 1
