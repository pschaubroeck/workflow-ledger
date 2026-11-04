"""Exact, deterministic selection shared by recorders and source adapters."""

from dataclasses import dataclass, replace
import json
from pathlib import Path
import re

from .model import KINDS, EventRecord, SpanRecord


@dataclass(frozen=True)
class Selector:
    scope: str
    identity: str

    def __post_init__(self):
        if not isinstance(self.scope, str) or self.scope not in KINDS | {"runtime"} or not isinstance(self.identity, str) or not self.identity:
            raise ValueError("Invalid selector")
        if self.scope != "runtime" and ":" not in self.identity:
            raise ValueError("Asset selectors require namespace:logical_id")

    def matches(self, record: SpanRecord) -> bool:
        if self.scope == "runtime":
            return self.identity == record.harness
        if record.kind == self.scope and record.identity is not None and record.identity.key == self.identity:
            return True
        return any(a.kind == self.scope and a.identity.key == self.identity and a.measured
                   for a in record.associations)


@dataclass(frozen=True)
class CollectionPolicy:
    enabled: bool = True
    include: tuple[Selector, ...] = ()
    exclude: tuple[Selector, ...] = ()

    def __post_init__(self):
        if type(self.enabled) is not bool or not all(
            isinstance(rows, tuple) and all(isinstance(s, Selector) for s in rows)
            for rows in (self.include, self.exclude)
        ):
            raise ValueError("Invalid collection policy")

    def may_observe_runtime(self, harness: str) -> bool:
        """Known runtime exclusion/disable skips source work before inspecting it."""
        if not self.enabled or Selector("runtime", harness) in self.exclude:
            return False
        return not self.include or any(s.scope != "runtime" or s.identity == harness for s in self.include)

    def allows(self, record: SpanRecord) -> bool:
        if not self.may_observe_runtime(record.harness) or any(s.matches(record) for s in self.exclude):
            return False
        if not self.include or any(s.matches(record) for s in self.include):
            return True
        # A selected native Codex agent needs its session root to preserve its
        # real parent link. Keep only this minimal structural context; explicit
        # exclusions above still take precedence and child associations inherit
        # that exclusion. Other Codex events remain governed by exact selectors.
        return (
            record.harness == "codex"
            and record.kind == "workflow"
            and record.identity is not None
            and record.identity.key == "codex-workflow:session"
            and any(s.scope == "agent" and re.fullmatch(r"codex-agent:[0-9a-f]{32}", s.identity)
                    for s in self.include)
        )

    def select_records(self, records: list[SpanRecord]) -> list[SpanRecord]:
        """Filter query/export input and distinguish known filtered parent links."""
        latest = list({r.key: r for r in records}.values())
        all_keys = {(r.trace_id, r.span_id) for r in latest}
        selected = [r for r in latest if self.allows(r)]
        kept_keys = {(r.trace_id, r.span_id) for r in selected}
        return [replace(r, filtered_parent=True) if r.parent_span_id is not None and
                (r.trace_id, r.parent_span_id) in all_keys - kept_keys else r for r in selected]

    def allows_event(self, record: EventRecord) -> bool:
        if not self.enabled:
            return False
        if any(selector.scope == "runtime" and selector.identity == record.harness
               for selector in self.exclude):
            return False
        if any(selector.scope != "runtime" and any(
            association.kind == selector.scope and association.identity.key == selector.identity
            and association.measured for association in record.associations
        ) for selector in self.exclude):
            return False
        if not self.include:
            return True
        return any(
            (selector.scope == "runtime" and selector.identity == record.harness)
            or (selector.scope != "runtime" and any(
                association.kind == selector.scope and association.identity.key == selector.identity
                and association.measured for association in record.associations
            ))
            for selector in self.include
        )

    def select_events(self, records: list[EventRecord]) -> list[EventRecord]:
        return [record for record in records if self.allows_event(record)]

    @classmethod
    def from_dict(cls, value: dict) -> "CollectionPolicy":
        if not isinstance(value, dict) or set(value) - {"enabled", "include", "exclude"}:
            raise ValueError("Invalid policy object")
        converted = dict(value)
        for key in ("include", "exclude"):
            rows = converted.get(key, [])
            if not isinstance(rows, list):
                raise ValueError("Selectors must be a list")
            if any(not isinstance(row, dict) or set(row) != {"scope", "identity"} for row in rows):
                raise ValueError("Invalid selector object")
            converted[key] = tuple(Selector(**row) for row in rows)
        return cls(**converted)

    @classmethod
    def load(cls, path: str | Path) -> "CollectionPolicy":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
