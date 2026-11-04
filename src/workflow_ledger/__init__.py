"""Optional instrumentation and local observation of agent workflows."""

from importlib.metadata import PackageNotFoundError, version

from .model import (AssetIdentity, Association, Capabilities, EventRecord, Kind, Severity,
                    SpanLink, SpanRecord, TokenUsage)
from .policy import CollectionPolicy, Selector
from .retention import RetentionPolicy
from .recorder import Recorder
from .storage import JsonlStore
from .event_store import EventStore

__all__ = ["AssetIdentity", "Association", "Capabilities", "CollectionPolicy", "EventRecord",
           "EventStore", "JsonlStore", "Kind", "Recorder", "RetentionPolicy", "Selector",
           "Severity", "SpanLink", "SpanRecord", "TokenUsage"]
try:
    __version__ = version("workflow-ledger")
except PackageNotFoundError:
    __version__ = "0.3.0"
