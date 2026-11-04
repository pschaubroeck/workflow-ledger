"""Small measurement contract independent of any exporter or harness.

Records are completed or partially observed operations, not transcripts. Input
and output tokens are primary categories; cached input and reasoning are subsets
and must never be added to those primary counts.
"""

from dataclasses import asdict, dataclass, fields
from datetime import datetime
import hashlib
import re
from typing import Literal

Quality = Literal["direct", "inferred", "estimated", "unavailable"]
Kind = Literal["workflow", "agent", "skill", "model", "tool", "wait"]
Outcome = Literal["ok", "error", "cancelled", "unknown"]
Lifecycle = Literal["configured", "loaded", "invoked", "completed", "failed", "cancelled", "observed"]
Severity = Literal["TRACE", "DEBUG", "INFO", "WARN", "ERROR", "FATAL"]
SEVERITY_NUMBERS = {"TRACE": 1, "DEBUG": 5, "INFO": 9, "WARN": 13, "ERROR": 17, "FATAL": 21}
MAX_OTEL_TIMESTAMP_NS = (1 << 64) - 1
MIN_OTEL_INTEGER = -(1 << 63)
MAX_OTEL_INTEGER = (1 << 63) - 1
TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")
TOKEN_SOURCES = frozenset({
    "explicit", "provider_response", "native_cumulative_snapshot", "openai-agents",
    "synthetic", "other",
})
QUALITIES = {"direct", "inferred", "estimated", "unavailable"}
KINDS = {"workflow", "agent", "skill", "model", "tool", "wait"}
EXECUTION_STATES = {"invoked", "completed", "failed", "cancelled"}


@dataclass(frozen=True)
class AssetIdentity:
    namespace: str
    logical_id: str
    version: str | None = None
    provenance: Literal["native", "caller_declared", "legacy"] = "caller_declared"

    def __post_init__(self) -> None:
        if not isinstance(self.namespace, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.namespace):
            raise ValueError("Invalid identity namespace")
        for value in (self.logical_id, self.version):
            if value is not None and (not isinstance(value, str) or not value or len(value) > 256
                                      or any(ord(c) < 32 for c in value)):
                raise ValueError("Invalid identity label")
        if self.logical_id is None or self.provenance not in {"native", "caller_declared", "legacy"}:
            raise ValueError("Invalid identity provenance")

    @property
    def key(self) -> str:
        return f"{self.namespace}:{self.logical_id}"


@dataclass(frozen=True)
class Association:
    kind: Kind
    identity: AssetIdentity
    state: Literal["configured", "loaded", "invoked"] = "loaded"
    evidence: Literal["native", "caller_declared", "contextual", "legacy"] = "contextual"
    invocation_span_id: str | None = None
    invocation_trace_id: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS or not isinstance(self.identity, AssetIdentity):
            raise ValueError("Invalid association identity")
        if self.state not in {"configured", "loaded", "invoked"} or self.evidence not in {
            "native", "caller_declared", "contextual", "legacy"
        }:
            raise ValueError("Invalid association state or evidence")
        if self.invocation_span_id is not None and (not isinstance(self.invocation_span_id, str) or
                not re.fullmatch(r"[0-9a-f]{16}", self.invocation_span_id) or
                int(self.invocation_span_id, 16) == 0):
            raise ValueError("Invalid association invocation ID")
        if self.state != "invoked" and self.invocation_span_id is not None:
            raise ValueError("Contextual state cannot claim an invocation")
        if self.invocation_trace_id is not None and (not isinstance(self.invocation_trace_id, str) or
                not re.fullmatch(r"[0-9a-f]{32}", self.invocation_trace_id) or
                int(self.invocation_trace_id, 16) == 0):
            raise ValueError("Invalid association trace ID")
        if (self.invocation_span_id is None) != (self.invocation_trace_id is None):
            raise ValueError("Invocation references require both trace and span IDs")
        if self.evidence in {"native", "caller_declared"} and self.state == "invoked" and (
            self.invocation_span_id is None
        ):
            raise ValueError("Invocation evidence requires an invocation ID")

    @property
    def measured(self) -> bool:
        return self.state == "invoked" and self.evidence in {"native", "caller_declared"} and (
            self.identity.provenance != "legacy"
        )


@dataclass(frozen=True)
class SpanLink:
    relationship: Literal["waits_for"]
    trace_id: str
    span_id: str
    evidence: Literal["native", "caller_declared"] = "caller_declared"

    def __post_init__(self) -> None:
        if self.relationship != "waits_for" or self.evidence not in {"native", "caller_declared"}:
            raise ValueError("Invalid span link")
        for name, value, length in (("trace_id", self.trace_id, 32), ("span_id", self.span_id, 16)):
            if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
                raise ValueError(f"Invalid link {name}")
            if int(value, 16) == 0:
                raise ValueError(f"Zero link {name}")


def stable_id(namespace: str, native_id: str, length: int = 32) -> str:
    """Opaque stable identity; native identifiers and paths are not retained."""
    return hashlib.sha256(f"{namespace}\0{native_id}".encode()).hexdigest()[:length]


def timestamp_ns(value: str | None) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return None
        return int(instant.timestamp() * 1_000_000_000)
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class Capabilities:
    adapter: str
    version: str
    workflows: Quality
    agents: Quality
    skills: Quality
    models: Quality
    tools: Quality
    durations: Quality
    tokens: Quality
    limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    scope: Literal["run", "model_call"] = "model_call"
    quality: Quality = "direct"
    source: str = "explicit"

    def __post_init__(self) -> None:
        if self.scope not in {"run", "model_call"} or self.quality not in QUALITIES:
            raise ValueError("Invalid token scope or quality")
        if not isinstance(self.source, str) or self.source not in TOKEN_SOURCES:
            raise ValueError("Invalid token source")
        for name in TOKEN_FIELDS:
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or None")
        for subset, total in (("cached_input_tokens", "input_tokens"),
                              ("reasoning_tokens", "output_tokens")):
            a, b = getattr(self, subset), getattr(self, total)
            if a is not None and b is not None and a > b:
                raise ValueError(f"{subset} exceeds {total}")
        if self.quality == "unavailable" and any(getattr(self, f) is not None for f in TOKEN_FIELDS):
            raise ValueError("Unavailable usage cannot contain token counts")


@dataclass(frozen=True)
class SpanRecord:
    trace_id: str
    span_id: str
    kind: Kind
    name: str
    harness: str
    adapter: str
    parent_span_id: str | None = None
    start_ns: int | None = None
    end_ns: int | None = None
    outcome: Outcome = "unknown"
    duration_quality: Quality = "unavailable"
    usage: TokenUsage | None = None
    agent_name: str | None = None
    skill_name: str | None = None
    skill_version: str | None = None
    model: str | None = None
    error_type: str | None = None
    source_version: str | None = None
    adapter_version: str = "0.2.0"
    schema_version: int = 3
    identity: AssetIdentity | None = None
    associations: tuple[Association, ...] = ()
    links: tuple[SpanLink, ...] = ()
    lifecycle: Lifecycle = "observed"
    filtered_parent: bool = False

    def __post_init__(self) -> None:
        for name, length in (("trace_id", 32), ("span_id", 16), ("parent_span_id", 16)):
            value = getattr(self, name)
            if value is None and name == "parent_span_id":
                continue
            if not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value):
                raise ValueError(f"Invalid {name}")
            if int(value, 16) == 0:
                raise ValueError(f"Zero {name}")
        if self.parent_span_id == self.span_id:
            raise ValueError("Operation cannot parent itself")
        if type(self.schema_version) is not int or self.schema_version != 3 or self.kind not in KINDS:
            raise ValueError("Unsupported record schema or kind")
        if self.outcome not in {"ok", "error", "cancelled", "unknown"}:
            raise ValueError("Invalid outcome")
        if self.duration_quality not in QUALITIES:
            raise ValueError("Invalid duration quality")
        if self.lifecycle not in {"configured", "loaded", "invoked", "completed", "failed", "cancelled", "observed"}:
            raise ValueError("Invalid lifecycle state")
        if self.identity is not None and not isinstance(self.identity, AssetIdentity):
            raise ValueError("Invalid asset identity")
        if not isinstance(self.associations, tuple) or not all(isinstance(a, Association) for a in self.associations):
            raise ValueError("Invalid associations")
        if not isinstance(self.links, tuple) or not all(isinstance(link, SpanLink) for link in self.links):
            raise ValueError("Invalid links")
        if self.links and self.kind != "wait":
            raise ValueError("Only wait spans can contain dependency links")
        if any(link.trace_id == self.trace_id and link.span_id == self.span_id for link in self.links):
            raise ValueError("Operation cannot wait for itself")
        if type(self.filtered_parent) is not bool:
            raise ValueError("Invalid filtered-parent flag")
        if self.lifecycle in {"configured", "loaded"} and (
            self.usage is not None or self.duration_quality != "unavailable" or self.end_ns is not None
            or self.outcome != "unknown"
        ):
            raise ValueError("Configuration/loading cannot claim execution measurements")
        if self.lifecycle == "invoked" and (
            self.end_ns is not None or self.duration_quality != "unavailable" or self.outcome != "unknown"
        ):
            raise ValueError("Invoked lifecycle must remain open")
        if (self.lifecycle == "failed" and self.outcome != "error") or (
            self.lifecycle == "cancelled" and self.outcome != "cancelled"
        ) or (self.lifecycle == "completed" and self.outcome not in {"ok", "unknown"}):
            raise ValueError("Lifecycle and outcome disagree")
        for value in (self.start_ns, self.end_ns):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError("Timestamp must be a nonnegative integer or None")
        if self.start_ns is not None and self.end_ns is not None and self.end_ns < self.start_ns:
            raise ValueError("End precedes start")
        if self.duration_quality != "unavailable" and self.duration_seconds is None:
            raise ValueError("Duration quality requires both timestamps")
        if self.usage is not None:
            if not isinstance(self.usage, TokenUsage):
                raise ValueError("Invalid usage")
            if (self.usage.scope == "run" and self.kind != "workflow") or (
                self.usage.scope == "model_call" and self.kind != "model"
            ):
                raise ValueError("Token scope does not match operation kind")
            if self.usage.scope == "run" and self.parent_span_id is not None:
                raise ValueError("Run usage belongs on a root workflow")
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name not in {"start_ns", "end_ns", "usage", "schema_version", "identity", "associations", "links", "filtered_parent"} and (
                value is not None and not isinstance(value, str)
            ):
                raise ValueError("Metadata labels must be strings")
            if field.name in {"name", "harness", "adapter"} and not value:
                raise ValueError(f"{field.name} is required")
            if isinstance(value, str) and (len(value) > 256 or any(ord(c) < 32 for c in value)):
                raise ValueError("Metadata label is too long or contains control characters")

    @property
    def key(self) -> str:
        return f"{self.trace_id}/{self.span_id}"

    @property
    def duration_seconds(self) -> float | None:
        if self.start_ns is None or self.end_ns is None:
            return None
        return (self.end_ns - self.start_ns) / 1_000_000_000

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "SpanRecord":
        # Reject unrecognized keys rather than silently accepting transcript content.
        data = dict(value)
        schema = data.get("schema_version", 1)
        if type(schema) is not int or schema not in {1, 2, 3}:
            raise ValueError("Unsupported record schema")
        if schema == 1:
            # Historical labels are not upgraded into proof of invocations or ownership.
            if any(k in data for k in ("identity", "associations", "links", "lifecycle", "filtered_parent")):
                raise ValueError("Schema 1 cannot contain later-schema fields")
            data["schema_version"] = 2
            data["lifecycle"] = "observed"
            schema = 2
        if schema == 2:
            if "links" in data:
                raise ValueError("Schema 2 cannot contain schema 3 fields")
            data["schema_version"] = 3
            data["links"] = ()
        if data.get("identity") is not None:
            data["identity"] = AssetIdentity(**data["identity"])
        associations = data.get("associations", ())
        if not isinstance(associations, (list, tuple)) or any(
            not isinstance(a, dict) or not isinstance(a.get("identity"), dict) for a in associations
        ):
            raise ValueError("Invalid association objects")
        data["associations"] = tuple(Association(**{**a, "identity": AssetIdentity(**a["identity"])})
                                     for a in associations)
        links = data.get("links", ())
        if not isinstance(links, (list, tuple)) or any(not isinstance(link, dict) for link in links):
            raise ValueError("Invalid span link objects")
        data["links"] = tuple(SpanLink(**link) for link in links)
        if data.get("usage") is not None:
            usage = dict(data["usage"])
            source = usage.get("source", "explicit")
            if not isinstance(source, str) or source not in TOKEN_SOURCES:
                usage["source"] = "other"
            data["usage"] = TokenUsage(**usage)
        return cls(**data)


@dataclass(frozen=True)
class EventRecord:
    """One structured operational event using the OpenTelemetry Logs data model."""

    event_id: str
    event_name: str
    severity_text: Severity
    timestamp_ns: int
    harness: str
    attributes: dict[str, str | int | bool]
    body: str | None = None
    trace_id: str | None = None
    span_id: str | None = None
    associations: tuple[Association, ...] = ()
    observed_timestamp_ns: int | None = None
    schema_version: int = 2

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 2:
            raise ValueError("Unsupported event schema")
        if (not isinstance(self.event_id, str) or not re.fullmatch(r"[0-9a-f]{32}", self.event_id)
                or int(self.event_id, 16) == 0):
            raise ValueError("Invalid event ID")
        if not isinstance(self.event_name, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){1,7}", self.event_name
        ):
            raise ValueError("Event name must be a dotted lowercase identifier")
        if not isinstance(self.severity_text, str) or self.severity_text not in SEVERITY_NUMBERS:
            raise ValueError("Invalid severity")
        if type(self.timestamp_ns) is not int or not 0 <= self.timestamp_ns <= MAX_OTEL_TIMESTAMP_NS:
            raise ValueError("Timestamp must be a uint64 nanosecond value")
        if self.observed_timestamp_ns is not None and (
            type(self.observed_timestamp_ns) is not int
            or not 0 <= self.observed_timestamp_ns <= MAX_OTEL_TIMESTAMP_NS
        ):
            raise ValueError("Observed timestamp must be a uint64 nanosecond value or None")
        if (not isinstance(self.harness, str) or not self.harness or len(self.harness) > 256
                or any(ord(c) < 32 for c in self.harness)):
            raise ValueError("Harness is required")
        if self.body is not None and (
            not isinstance(self.body, str) or len(self.body) > 1024
            or any(ord(c) < 32 and c not in "\t\n\r" for c in self.body)
        ):
            raise ValueError("Event body is too long or contains control characters")
        for name, value, length in (("trace_id", self.trace_id, 32), ("span_id", self.span_id, 16)):
            if value is not None and (
                not isinstance(value, str) or not re.fullmatch(f"[0-9a-f]{{{length}}}", value)
                or int(value, 16) == 0
            ):
                raise ValueError(f"Invalid {name}")
        if (self.trace_id is None) != (self.span_id is None):
            raise ValueError("Event correlation requires both trace and span IDs")
        if not isinstance(self.attributes, dict) or len(self.attributes) > 64:
            raise ValueError("Event attributes must be an object with at most 64 fields")
        for key, value in self.attributes.items():
            if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]{0,127}", key):
                raise ValueError("Invalid event attribute name")
            if key.startswith(("workflow_ledger.", "agent_observability.")):
                raise ValueError("Event attributes cannot override reserved workflow_ledger fields")
            if isinstance(value, str):
                if len(value) > 1024 or any(ord(c) < 32 for c in value):
                    raise ValueError("Invalid event attribute value")
            elif type(value) is int and not MIN_OTEL_INTEGER <= value <= MAX_OTEL_INTEGER:
                raise ValueError("Integer event attributes must fit OpenTelemetry int64")
            elif type(value) not in {int, bool}:
                raise ValueError("Event attributes must be strings, integers, or booleans")
        if not isinstance(self.associations, tuple) or not all(
            isinstance(association, Association) for association in self.associations
        ):
            raise ValueError("Invalid event associations")

    @property
    def severity_number(self) -> int:
        return SEVERITY_NUMBERS[self.severity_text]

    def to_dict(self) -> dict:
        attributes = dict(self.attributes)
        associations = []
        for association in self.associations:
            value = asdict(association)
            if association.identity.version is None:
                value["identity"].pop("version")
            if association.invocation_span_id is None:
                value.pop("invocation_span_id")
                value.pop("invocation_trace_id")
            associations.append(value)
        attributes.update({
            "workflow_ledger.event_id": self.event_id,
            "workflow_ledger.schema_version": self.schema_version,
            "workflow_ledger.associations": associations,
        })
        record = {
            "Timestamp": self.timestamp_ns,
            "SeverityText": self.severity_text,
            "SeverityNumber": self.severity_number,
            "EventName": self.event_name,
            "Resource": {"Attributes": {"workflow_ledger.harness": self.harness}},
            "InstrumentationScope": {"Name": "workflow-ledger"},
            "Attributes": attributes,
        }
        if self.observed_timestamp_ns is not None:
            record["ObservedTimestamp"] = self.observed_timestamp_ns
        if self.body is not None:
            record["Body"] = self.body
        if self.trace_id is not None:
            record["TraceId"] = self.trace_id
            record["SpanId"] = self.span_id
        return record

    @classmethod
    def from_dict(cls, value: dict) -> "EventRecord":
        if not isinstance(value, dict):
            raise ValueError("Event record must be an object")
        data = dict(value)
        attributes = data.pop("Attributes", None)
        resource = data.pop("Resource", None)
        scope = data.pop("InstrumentationScope", None)
        if not isinstance(attributes, dict) or not isinstance(resource, dict) or not isinstance(scope, dict):
            raise ValueError("Event record requires Attributes, Resource, and InstrumentationScope")
        attributes = dict(attributes)
        resource_attributes = resource.get("Attributes")
        if (not isinstance(resource_attributes, dict)
                or set(resource_attributes) not in ({"workflow_ledger.harness"},
                                                     {"agent_observability.harness"})
                or set(scope) != {"Name"}
                or scope.get("Name") not in {"workflow-ledger", "agent-observability"}):
            raise ValueError("Unsupported event Resource or InstrumentationScope")
        namespace = ("agent_observability" if "agent_observability.event_id" in attributes
                     else "workflow_ledger")
        harness = resource_attributes.get(f"{namespace}.harness")
        event_id = attributes.pop(f"{namespace}.event_id", None)
        schema_version = attributes.pop(f"{namespace}.schema_version", None)
        if schema_version != 2:
            raise ValueError("Unsupported event schema")
        associations = attributes.pop(f"{namespace}.associations", ())
        if not isinstance(associations, (list, tuple)) or any(
            not isinstance(item, dict) or not isinstance(item.get("identity"), dict)
            for item in associations
        ):
            raise ValueError("Invalid event associations")
        mapping = {
            "Timestamp": "timestamp_ns",
            "ObservedTimestamp": "observed_timestamp_ns",
            "EventName": "event_name",
            "SeverityText": "severity_text",
            "Body": "body",
            "TraceId": "trace_id",
            "SpanId": "span_id",
        }
        normalized = {internal: data[external] for external, internal in mapping.items()
                      if external in data}
        normalized.update({
            "event_id": event_id,
            "harness": harness,
            "attributes": attributes,
            "associations": tuple(
                Association(**{**item, "identity": AssetIdentity(**item["identity"])})
                for item in associations
            ),
            "schema_version": schema_version,
        })
        severity_number = data.get("SeverityNumber")
        if set(data) - set(mapping) - {"SeverityNumber"}:
            raise ValueError("Unsupported OpenTelemetry log record fields")
        record = cls(**normalized)
        if severity_number != record.severity_number:
            raise ValueError("Severity text and number disagree")
        return record
