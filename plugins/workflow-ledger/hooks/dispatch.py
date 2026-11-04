"""Portable, fail-open launcher for a deliberately configured local interpreter."""

import json
import os
from pathlib import Path
import subprocess
import sys


def dispatch():
    if os.environ.get("WORKFLOW_LEDGER_ENABLED") != "1":
        return 0
    interpreter = os.environ.get("WORKFLOW_LEDGER_PYTHON")
    store = os.environ.get("WORKFLOW_LEDGER_STORE")
    if not interpreter or not Path(interpreter).is_absolute() or not store or not Path(store).is_absolute():
        print(json.dumps({"error": "missing_absolute_configuration"}), file=sys.stderr)
        return 0
    try:
        result = subprocess.run(
            [interpreter, "-m", "workflow_ledger.adapters.codex_hooks"],
            stdin=sys.stdin, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=8, check=False,
        )
        if result.returncode:
            print(json.dumps({"error": "hook_process_failed"}), file=sys.stderr)
        elif result.stderr:
            # Only forward the adapter's tiny numeric diagnostics/static codes.
            value = json.loads(result.stderr)
            diagnostics = value.get("diagnostics", {})
            safe = {key: count for key, count in diagnostics.items()
                    if key in {"unsupported_events", "rejected_events", "missing_starts",
                               "filtered_records", "storage_errors", "delivery_errors",
                               "lock_timeouts", "permission_denied", "storage_full",
                               "storage_unavailable", "missing_ids", "observed"}
                    and type(count) is int and count >= 0}
            if safe:
                print(json.dumps({"diagnostics": safe}), file=sys.stderr)
            if value.get("error") in {"invalid_configuration", "invalid_payload",
                                     "hook_failed", "policy_error", "retention_policy_error"}:
                print(json.dumps({"error": value["error"]}), file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired, ValueError, AttributeError, TypeError):
        print(json.dumps({"error": "hook_launch_failed"}), file=sys.stderr)
    return 0


def main():
    try:
        return dispatch()
    finally:
        # SubagentStop requires JSON on stdout. A neutral object also works for
        # the other configured events and never requests continuation/blocking.
        print("{}")


if __name__ == "__main__":
    raise SystemExit(main())
