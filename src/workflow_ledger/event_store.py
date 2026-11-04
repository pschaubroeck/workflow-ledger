"""Append-only local JSONL storage for structured events."""

import json
from pathlib import Path
import threading

from .model import EventRecord
from .retention import RetentionPolicy, append_record, archive_paths, resolve_retention_policy, store_lock


class EventStore:
    """Persist immutable event records in a separate file from operation spans."""

    def __init__(self, path: str | Path, retention: RetentionPolicy | None = None):
        self.path = Path(path)
        self.retention = resolve_retention_policy(retention)
        self.diagnostics = {"malformed_records": 0, "storage_errors": 0}
        self._lock = threading.RLock()

    def emit(self, record: EventRecord) -> bool:
        if not isinstance(record, EventRecord):
            raise ValueError("EventStore accepts EventRecord values")
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with store_lock(self.path):
                    payload = json.dumps(record.to_dict(), separators=(",", ":")) + "\n"
                    append_record(self.path, payload, {}, self.retention)
                return True
            except (OSError, UnicodeError):
                self.diagnostics["storage_errors"] += 1
                return False

    def read(self, *, include_archives: bool = True) -> list[EventRecord]:
        with self._lock:
            if not self.path.exists() and not self.path.parent.exists():
                self.diagnostics["malformed_records"] = 0
                return []
            with store_lock(self.path):
                sources = (archive_paths(self.path) if include_archives else [])
                if self.path.exists():
                    sources.append(self.path)
                records = []
                malformed = 0
                for source in sources:
                    with source.open("rb") as handle:
                        for raw_line in handle:
                            try:
                                line = raw_line.decode("utf-8")
                                records.append(EventRecord.from_dict(json.loads(line)))
                            except (ValueError, TypeError, AttributeError, json.JSONDecodeError,
                                    UnicodeError):
                                malformed += 1
                self.diagnostics["malformed_records"] = malformed
                return records
