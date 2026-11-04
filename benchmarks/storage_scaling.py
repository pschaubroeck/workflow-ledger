"""Measure local JSONL report and marker cost as a store grows.

All inputs are synthetic. The script never opens the user's configured store.
Run from the repository root with the project environment:

    .venv/bin/python benchmarks/storage_scaling.py --output docs/storage-scaling.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

from workflow_ledger.model import Association, AssetIdentity, SpanRecord
from workflow_ledger.retention import archive_paths
from workflow_ledger.sessions import SessionStore


REPO = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SIZES = (1_000, 5_000, 10_000, 25_000, 50_000)
TRIALS = 5
WARMUPS = 1


def fixture_record(index: int) -> SpanRecord:
    trace_id = f"{index + 1:032x}"
    span_id = f"{index + 1:016x}"
    identity = AssetIdentity("benchmark", "synthetic-operation", "1")
    association = Association(
        kind="skill", identity=identity, state="invoked", evidence="caller_declared",
        invocation_span_id=span_id, invocation_trace_id=trace_id,
    )
    return SpanRecord(
        trace_id=trace_id, span_id=span_id, kind="skill", name=identity.key,
        harness="codex", adapter="shared_markers", adapter_version="0.3.0",
        start_ns=1_000_000, end_ns=2_000_000, outcome="ok", duration_quality="inferred",
        identity=identity, associations=(association,), lifecycle="completed",
    )


def seed_store(path: Path, size: int) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for index in range(size):
            handle.write(json.dumps(fixture_record(index).to_dict(), separators=(",", ":")) + "\n")


def marker_trial(path: Path, number: int) -> dict[str, float]:
    marker = SessionStore(path)
    identity = AssetIdentity("benchmark", "synthetic-marker", "1")
    session = f"storage-scaling-{number}"
    invocation = f"invocation-{number}"
    start = time.perf_counter_ns()
    opened = marker.start("skill", identity, session_id=session, invocation_id=invocation)
    start_ns = time.perf_counter_ns() - start
    if not opened.get("recorded"):
        raise RuntimeError("Synthetic marker start was not recorded")
    finish = time.perf_counter_ns()
    closed = marker.finish(opened["trace_id"], opened["span_id"], outcome="ok")
    finish_ns = time.perf_counter_ns() - finish
    if not closed.get("recorded"):
        raise RuntimeError("Synthetic marker finish was not recorded")
    start_ms = start_ns / 1_000_000
    finish_ms = finish_ns / 1_000_000
    return {"start_ms": start_ms, "finish_ms": finish_ms, "pair_ms": start_ms + finish_ms}


def report_trial(path: Path) -> float:
    command = [PYTHON, "-m", "workflow_ledger", "report", "--store", str(path), "--format", "text"]
    started = time.perf_counter_ns()
    result = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False)
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if result.returncode != 0:
        raise RuntimeError(f"Synthetic report failed with exit status {result.returncode}")
    return elapsed_ms


def run(sizes: tuple[int, ...], trials: int) -> dict:
    results = []
    with tempfile.TemporaryDirectory(prefix="workflow-ledger-storage-scaling-") as temp:
        root = Path(temp)
        for size in sizes:
            path = root / f"synthetic-{size}.jsonl"
            seed_store(path, size)
            seeded_bytes = path.stat().st_size
            # Warm file parsing, filesystem metadata, and the report command once.
            marker_trial(path, size * 100 - 1)
            report_trial(path)
            marker_trials = []
            report_trials = []
            for trial in range(trials):
                marker_trials.append(marker_trial(path, size * 100 + trial))
                report_trials.append(report_trial(path))
            archives = archive_paths(path)
            active_bytes = path.stat().st_size
            archive_bytes = sum(archive.stat().st_size for archive in archives)
            start_ms = [row["start_ms"] for row in marker_trials]
            finish_ms = [row["finish_ms"] for row in marker_trials]
            pair_ms = [row["pair_ms"] for row in marker_trials]
            report_ms = report_trials
            results.append({
                "seed_records": size,
                "seed_bytes": seeded_bytes,
                "seed_mib": round(seeded_bytes / (1024 * 1024), 3),
                "trials": trials,
                "marker_start_ms": {
                    "raw": [round(value, 3) for value in start_ms],
                    "median": round(statistics.median(start_ms), 3),
                    "min": round(min(start_ms), 3),
                    "max": round(max(start_ms), 3),
                },
                "marker_finish_ms": {
                    "raw": [round(value, 3) for value in finish_ms],
                    "median": round(statistics.median(finish_ms), 3),
                    "min": round(min(finish_ms), 3),
                    "max": round(max(finish_ms), 3),
                },
                "marker_pair_ms": {
                    "raw": [round(value, 3) for value in pair_ms],
                    "median": round(statistics.median(pair_ms), 3),
                    "min": round(min(pair_ms), 3),
                    "max": round(max(pair_ms), 3),
                },
                "report_text_cli_ms": {
                    "raw": [round(value, 3) for value in report_ms],
                    "median": round(statistics.median(report_ms), 3),
                    "min": round(min(report_ms), 3),
                    "max": round(max(report_ms), 3),
                },
                "final_records_including_marker_trials": size + trials + WARMUPS,
                "final_active_bytes": active_bytes,
                "final_archive_count": len(archives),
                "final_archive_bytes": archive_bytes,
                "final_total_bytes": active_bytes + archive_bytes,
            })
    return {
        "benchmark": "synthetic-jsonl-store-scaling",
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "filesystem_path_type": "temporary local directory",
            "cpu_count": os.cpu_count(),
        },
        "conditions": {
            "synthetic_only": True,
            "real_observation_store_opened": False,
            "fixture_shape": "completed shared-marker skill records with one caller-declared invocation association",
            "marker_measurement": "in-process SessionStore start and finish, including file lock and active-file parse; a write may rotate and purge archives; excludes process startup and CLI policy loading",
            "report_measurement": "fresh Python process running the human-readable report CLI; includes process startup and full read, parse, summarize, and text rendering",
            "fixture_setup_and_cleanup_timed": False,
            "warmups_per_size": WARMUPS,
            "trials": trials,
            "seed_sizes": list(sizes),
            "summary": "median and observed range; not an SLA or stable tail-latency estimate",
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=SIZES)
    parser.add_argument("--trials", type=int, default=TRIALS)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.trials < 1 or any(size < 1 for size in args.sizes):
        parser.error("sizes and trials must be positive")
    data = run(tuple(args.sizes), args.trials)
    rendered = json.dumps(data, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
