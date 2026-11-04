"""Minimal local example using only Workflow Ledger's public Python API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from workflow_ledger import JsonlStore, Recorder


def run(store_path: str | Path) -> dict:
    """Record a small workflow without calling a model or external tool."""
    store = JsonlStore(store_path)
    recorder = Recorder(store, harness="my-application")

    with recorder.span("workflow", "example-request"):
        with recorder.span("agent", "researcher"):
            with recorder.span("tool", "local-lookup"):
                pass  # Replace this block with the application's existing work.

    records = store.read()
    return {
        "records_written": len(records),
        "recorder_diagnostics": recorder.diagnostics,
        "store_diagnostics": store.diagnostics,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=Path("observations.jsonl"))
    args = parser.parse_args()
    print(json.dumps(run(args.store), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
