"""Local JSONL rotation, bounded archive retention, and shared file locking."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from .model import SpanRecord

try:
    import fcntl
except ImportError:  # pragma: no cover - cross-process locking is POSIX-only
    fcntl = None

MIB = 1024 * 1024
DEFAULT_ROTATION_BYTES = 5 * MIB
DEFAULT_ARCHIVE_CAP_BYTES = 250 * MIB
_ARCHIVE_SUFFIX = re.compile(r"\.archive-(\d{20})-([0-9a-f]{32})\.jsonl$")


@dataclass(frozen=True)
class RetentionPolicy:
    rotation_bytes: int = DEFAULT_ROTATION_BYTES
    archive_cap_bytes: int = DEFAULT_ARCHIVE_CAP_BYTES
    enabled: bool = True

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("Invalid retention enabled flag")
        if type(self.rotation_bytes) is not int or self.rotation_bytes < 1:
            raise ValueError("Rotation size must be a positive integer")
        if type(self.archive_cap_bytes) is not int or self.archive_cap_bytes < self.rotation_bytes:
            raise ValueError("Archive cap must be at least the rotation size")

    @classmethod
    def from_dict(cls, value: dict) -> "RetentionPolicy":
        if not isinstance(value, dict) or set(value) - {
            "enabled", "rotation_bytes", "archive_cap_bytes"
        }:
            raise ValueError("Invalid retention policy")
        return cls(**value)

    @classmethod
    def load(cls, path: str | Path) -> "RetentionPolicy":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def resolve_retention_policy(policy: RetentionPolicy | None = None) -> RetentionPolicy:
    if policy is not None:
        if not isinstance(policy, RetentionPolicy):
            raise ValueError("Invalid retention policy")
        return policy
    configured_path = os.environ.get("WORKFLOW_LEDGER_RETENTION_POLICY")
    return RetentionPolicy.load(configured_path) if configured_path else RetentionPolicy()


class StoreLockTimeout(TimeoutError):
    """The bounded cooperative lock could not be acquired in time."""


def _recover_rotation(path: Path) -> None:
    if not path.parent.exists():
        return
    prefix = f".{path.name}.rotation-"
    temporary = sorted(
        item for item in path.parent.iterdir()
        if item.name.startswith(prefix) and item.name.endswith(".tmp")
        and not item.is_symlink() and item.is_file()
    )
    if not path.exists() and temporary:
        os.replace(temporary.pop(), path)
    for item in temporary:
        item.unlink()


@contextmanager
def store_lock(path: str | Path, timeout: float = 2.0):
    """Serialize store reads, appends, rotation, and purge across POSIX writers."""
    path = Path(path)
    lock_path = path.with_name(path.name + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is None:
            yield
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise StoreLockTimeout("Store lock timed out")
                time.sleep(0.01)
        _recover_rotation(path)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(descriptor)


def archive_paths(path: str | Path) -> list[Path]:
    """Return only regular files created for this active store, oldest first."""
    path = Path(path)
    prefix = f"{path.name}.archive-"
    matches = []
    for candidate in path.parent.iterdir() if path.parent.exists() else ():
        if not candidate.name.startswith(prefix) or not _ARCHIVE_SUFFIX.search(candidate.name):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        matches.append(candidate)
    return sorted(matches, key=lambda item: item.name)


def read_store_records(path: str | Path, *, include_archives: bool = True) -> tuple[list[SpanRecord], int]:
    """Read revisions oldest-to-newest so the newest retained revision wins."""
    path = Path(path)
    sources = (archive_paths(path) if include_archives else []) + ([path] if path.exists() else [])
    latest: dict[str, SpanRecord] = {}
    malformed = 0
    for source in sources:
        with source.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = SpanRecord.from_dict(json.loads(line))
                    latest[record.key] = record
                except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                    malformed += 1
    return list(latest.values()), malformed


def read_archived_records(
    path: str | Path,
    *,
    keys: set[str] | None = None,
    predicate=None,
    selector: tuple[str, str, str] | None = None,
) -> tuple[list[SpanRecord], int]:
    """Read selected latest records from retained archives with bounded memory.

    When ``keys`` is supplied, only those record identities are retained while
    scanning. A predicate may select records by immutable identifying fields,
    such as trace, kind, and asset identity. Newer retained revisions replace
    older revisions with the same key.
    """
    if keys is None and predicate is None:
        raise ValueError("Archive selection is required")
    path = Path(path)
    archives = archive_paths(path)
    index_path = path.with_name(path.name + ".archive-index.sqlite3")
    try:
        _ensure_archive_index(index_path, archives)
        records = _query_archive_index(index_path, archives, keys, predicate, selector)
        return records, 0
    except (OSError, sqlite3.Error):
        # The index is an acceleration only. A damaged, read-only, or unavailable
        # sidecar must not make the primary JSONL store unusable.
        latest: dict[str, SpanRecord] = {}
        malformed = 0
        for source in archives:
            with source.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = SpanRecord.from_dict(json.loads(line))
                    except (ValueError, TypeError, AttributeError, json.JSONDecodeError):
                        malformed += 1
                        continue
                    if keys is not None and record.key not in keys:
                        continue
                    if predicate is not None and not predicate(record):
                        continue
                    latest[record.key] = record
        return list(latest.values()), malformed


def _ensure_archive_index(index_path: Path, archives: list[Path]) -> None:
    expected = [(item.name, item.stat().st_size, item.stat().st_mtime_ns) for item in archives]
    try:
        descriptor = os.open(index_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if index_path.is_symlink() or not index_path.is_file():
            raise OSError("Archive index is not a regular file")
        if index_path.stat().st_mode & 0o777 != 0o600:
            os.chmod(index_path, 0o600)
    else:
        os.close(descriptor)
    connection = sqlite3.connect(index_path)
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS archives (
                archive_id INTEGER PRIMARY KEY, name TEXT UNIQUE, size INTEGER, mtime_ns INTEGER
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS records (
                record_key BLOB PRIMARY KEY, archive_id INTEGER, byte_offset INTEGER,
                workflow_selector BLOB
            )"""
        )
        record_columns = {row[1] for row in connection.execute("PRAGMA table_info(records)")}
        archive_columns = {row[1] for row in connection.execute("PRAGMA table_info(archives)")}
        if (record_columns != {"record_key", "archive_id", "byte_offset", "workflow_selector"}
                or archive_columns != {"archive_id", "name", "size", "mtime_ns"}):
            connection.execute("DROP TABLE records")
            connection.execute("DROP TABLE archives")
            connection.execute(
                """CREATE TABLE archives (
                    archive_id INTEGER PRIMARY KEY, name TEXT UNIQUE, size INTEGER, mtime_ns INTEGER
                )"""
            )
            connection.execute(
                """CREATE TABLE records (
                    record_key BLOB PRIMARY KEY, archive_id INTEGER, byte_offset INTEGER,
                    workflow_selector BLOB
                )"""
            )
        current = connection.execute(
            "SELECT name, size, mtime_ns FROM archives ORDER BY name"
        ).fetchall()
        current_by_name = {name: (size, mtime_ns) for name, size, mtime_ns in current}
        expected_by_name = {name: (size, mtime_ns) for name, size, mtime_ns in expected}
        if current_by_name == expected_by_name:
            connection.commit()
            return

        removed = set(current_by_name) - set(expected_by_name)
        if removed:
            placeholders = ",".join("?" for _ in removed)
            names = sorted(removed)
            connection.execute(
                f"DELETE FROM records WHERE archive_id IN "
                f"(SELECT archive_id FROM archives WHERE name IN ({placeholders}))", names
            )
            connection.execute(
                f"DELETE FROM archives WHERE name IN ({placeholders})", names
            )

        changed = {
            name for name in current_by_name.keys() & expected_by_name.keys()
            if current_by_name[name] != expected_by_name[name]
        }
        if changed:
            # Archive files are immutable. A same-name content change means the
            # sidecar can no longer be trusted, so rebuild from retained files.
            connection.execute("DELETE FROM records")
            connection.execute("DELETE FROM archives")
            current_by_name.clear()

        needs_index = set(expected_by_name) - set(current_by_name)
        for archive, (name, size, mtime_ns) in zip(archives, expected):
            if name not in needs_index:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO archives (name, size, mtime_ns) VALUES (?, ?, ?)",
                (name, size, mtime_ns),
            )
            archive_id = connection.execute(
                "SELECT archive_id FROM archives WHERE name = ?", (name,)
            ).fetchone()[0]
            offset = 0
            with archive.open("rb") as handle:
                while line := handle.readline():
                    try:
                        record = SpanRecord.from_dict(json.loads(line))
                    except (ValueError, TypeError, AttributeError, json.JSONDecodeError, UnicodeError):
                        offset += len(line)
                        continue
                    identity = json.dumps(
                        None if record.identity is None else {
                            "namespace": record.identity.namespace,
                            "logical_id": record.identity.logical_id,
                            "version": record.identity.version,
                            "provenance": record.identity.provenance,
                        }, sort_keys=True, separators=(",", ":")
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?)",
                        (_record_key_bytes(record.key), archive_id, offset,
                         _workflow_selector(record.trace_id, record.kind, identity)),
                    )
                    offset += len(line)
        connection.commit()
        os.chmod(index_path, 0o600)
    finally:
        connection.close()


def _query_archive_index(
    index_path: Path,
    archives: list[Path],
    keys: set[str] | None,
    predicate,
    selector: tuple[str, str, str] | None,
) -> list[SpanRecord]:
    connection = sqlite3.connect(f"{index_path.as_uri()}?mode=ro", uri=True)
    try:
        clauses, parameters = [], []
        if keys is not None:
            placeholders = ",".join("?" for _ in keys)
            clauses.append(f"record_key IN ({placeholders})")
            parameters.extend(_record_key_bytes(key) for key in sorted(keys))
        if predicate is not None:
            if selector is None:
                rows = connection.execute(
                    "SELECT a.name, r.byte_offset FROM records r JOIN archives a "
                    "ON a.archive_id = r.archive_id"
                ).fetchall()
            else:
                clauses.append("workflow_selector = ?")
                parameters.append(_workflow_selector(*selector))
                where = " WHERE " + " AND ".join(clauses) if clauses else ""
                rows = connection.execute(
                    "SELECT a.name, r.byte_offset FROM records r JOIN archives a "
                    "ON a.archive_id = r.archive_id" + where, parameters
                ).fetchall()
        else:
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            rows = connection.execute(
                "SELECT a.name, r.byte_offset FROM records r JOIN archives a "
                "ON a.archive_id = r.archive_id" + where, parameters
            ).fetchall()
        by_name = {archive.name: archive for archive in archives}
        records = []
        for name, offset in rows:
            archive = by_name.get(name)
            if archive is None:
                continue
            with archive.open("rb") as handle:
                handle.seek(offset)
                records.append(SpanRecord.from_dict(json.loads(handle.readline())))
        if predicate is not None and selector is None:
            records = [record for record in records if predicate(record)]
        return records
    finally:
        connection.close()


def _record_key_bytes(key: str) -> bytes:
    trace_id, span_id = key.split("/", 1)
    return bytes.fromhex(trace_id + span_id)


def _workflow_selector(trace_id: str, kind: str, identity_json: str) -> bytes:
    digest = hashlib.sha256()
    for value in (trace_id, kind, identity_json):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.digest()


def _carry_forward(records: dict[str, SpanRecord]) -> list[SpanRecord]:
    """Keep open spans and their retained parent chain in the new active file."""
    by_span = {(record.trace_id, record.span_id): record for record in records.values()}
    pending = [(record.trace_id, record.span_id) for record in records.values()
               if record.lifecycle == "invoked"]
    keep: set[str] = set()
    while pending:
        trace_id, span_id = pending.pop()
        record = by_span.get((trace_id, span_id))
        if record is None or record.key in keep:
            continue
        keep.add(record.key)
        if record.parent_span_id is not None:
            pending.append((record.trace_id, record.parent_span_id))
    return [records[key] for key in sorted(keep)]


def _write_active(path: Path, records: list[SpanRecord], payload: str) -> Path:
    temporary = path.with_name(f".{path.name}.rotation-{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.to_dict(), separators=(",", ":")) + "\n")
        handle.write(payload)
    return temporary


def _purge_oldest(path: Path, cap_bytes: int) -> bool:
    archives = archive_paths(path)
    total = sum(archive.stat().st_size for archive in archives)
    purged = False
    for archive in archives:
        if total <= cap_bytes:
            break
        size = archive.stat().st_size
        archive.unlink()
        total -= size
        purged = True
    return purged


def append_record(
    path: str | Path,
    payload: str,
    records: dict[str, SpanRecord],
    retention: RetentionPolicy,
    *,
    needs_newline: bool = False,
) -> tuple[bool, bool]:
    """Append one revision; return whether rotation or purge changed the store."""
    path = Path(path)
    payload_bytes = len(payload.encode("utf-8")) + (1 if needs_newline else 0)
    active_size = path.stat().st_size if path.exists() else 0
    rotated = bool(retention.enabled and active_size and
                   active_size + payload_bytes > retention.rotation_bytes)
    if rotated:
        archive = path.with_name(
            f"{path.name}.archive-{time.time_ns():020d}-{uuid.uuid4().hex}.jsonl"
        )
        temporary = _write_active(path, _carry_forward(records), payload)
        os.replace(path, archive)
        os.chmod(archive, 0o600)
        os.replace(temporary, path)
    else:
        if needs_newline:
            payload = "\n" + payload
        append_fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(append_fd, "a", encoding="utf-8") as handle:
            handle.write(payload)
    purged = retention.enabled and _purge_oldest(path, retention.archive_cap_bytes)
    return rotated, purged
