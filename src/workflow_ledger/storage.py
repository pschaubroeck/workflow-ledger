"""Append-only local records. One writer process per store is supported."""

import json
import os
from pathlib import Path
import threading

from .model import SpanRecord
from .retention import RetentionPolicy, append_record, read_store_records, resolve_retention_policy, store_lock


class JsonlStore:
    def __init__(self, path: str | Path, retention: RetentionPolicy | None = None):
        self.path = Path(path)
        self.retention = resolve_retention_policy(retention)
        self.diagnostics = {"malformed_records": 0, "storage_errors": 0}
        self._lock = threading.RLock()
        self._latest: dict[str, SpanRecord] | None = None
        self._prepared = False

    def _read_unlocked(self) -> list[SpanRecord]:
        records, malformed = read_store_records(self.path, include_archives=False)
        self.diagnostics["malformed_records"] = malformed
        self._latest = {record.key: record for record in records}
        return records

    def read(self, *, include_archives: bool = True) -> list[SpanRecord]:
        with self._lock:
            self._prepared = False
            if not self.path.exists() and not self.path.parent.exists():
                self._latest = {}
                self.diagnostics["malformed_records"] = 0
                return []
            with store_lock(self.path):
                active = self._read_unlocked()
                if not include_archives:
                    return active
                records, malformed = read_store_records(self.path)
                self.diagnostics["malformed_records"] = malformed
                return records

    def emit(self, record: SpanRecord) -> bool:
        """Ignore identical observations; changed observations append a revision.

        Latest revision wins when reading. Storage failures return False, leaving
        instrumentation callers free to continue their work.
        """
        with self._lock:
            try:
                if not self._prepared:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                with store_lock(self.path):
                    if self._latest is None:
                        self._read_unlocked()
                    assert self._latest is not None
                    if self._latest.get(record.key) == record:
                        return True
                    needs_newline = False
                    if not self._prepared:
                        if self.path.exists() and self.path.stat().st_size:
                            with self.path.open("rb") as existing:
                                existing.seek(-1, os.SEEK_END)
                                needs_newline = existing.read(1) != b"\n"
                    payload = json.dumps(record.to_dict(), separators=(",", ":")) + "\n"
                    rotated, purged = append_record(
                        self.path, payload, self._latest, self.retention,
                        needs_newline=needs_newline,
                    )
                    self._latest[record.key] = record
                    if rotated or purged:
                        self._read_unlocked()
                    self._prepared = True
                    return True
            except (OSError, UnicodeError):
                # A partial append must have its tail checked before retrying.
                self._prepared = False
                self.diagnostics["storage_errors"] += 1
                return False
