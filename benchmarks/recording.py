#!/usr/bin/env python3
"""Reproducible local overhead probe for workflow-ledger.

Only synthetic records and labels are used. Results are written to the supplied
scratch path; no package telemetry or user content is collected.
"""
from __future__ import annotations

import argparse
import cProfile
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import pstats
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from workflow_ledger.model import TokenUsage  # noqa: E402
from workflow_ledger.recorder import Recorder  # noqa: E402
from workflow_ledger.storage import JsonlStore  # noqa: E402


class NullSink:
    def emit(self, record):
        return True


class JsonSink:
    def __init__(self):
        self.rows = []

    def emit(self, record):
        self.rows.append(json.dumps(record.to_dict(), separators=(",", ":")))
        return True


def nested_run(recorder):
    with recorder.span("workflow", "probe"):
        with recorder.span("agent", "worker"):
            with recorder.span("skill", "lookup", skill_version="1"):
                with recorder.span("model", "synthetic") as model:
                    model.usage = TokenUsage(input_tokens=10, source="synthetic")


def load_baseline(path):
    # Package-qualified module name keeps its relative .model import functional.
    name = "workflow_ledger._recording_baseline_storage"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import baseline source: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module.JsonlStore


def make_sink(kind, directory, store_class):
    if kind == "disabled":
        return NullSink(), False
    if kind == "noop":
        return NullSink(), True
    if kind == "json_capture":
        return JsonSink(), True
    return store_class(directory / "spans.jsonl"), True


def measure_recording(runs, trials, baseline_store=None):
    results = {}
    for stage in ("disabled", "noop", "json_capture", "jsonl_disk"):
        samples = []
        for trial in range(trials):
            with tempfile.TemporaryDirectory(prefix="recording-probe-") as temp:
                sink_class = baseline_store if stage == "jsonl_disk" and baseline_store else JsonlStore
                sink, enabled = make_sink(stage, Path(temp), sink_class)
                recorder = Recorder(sink, enabled=enabled)
                started = time.perf_counter_ns()
                for _ in range(runs):
                    nested_run(recorder)
                samples.append(time.perf_counter_ns() - started)
                check = {}
                if stage == "jsonl_disk":
                    latest = sink.read()  # explicitly untimed end-of-run correctness read
                    physical_lines = sink.path.read_bytes().count(b"\n")
                    check = {"latest_records": len(latest), "physical_lines": physical_lines,
                             "storage_errors": sink.diagnostics["storage_errors"]}
                    if len(latest) != 4 * runs or physical_lines != 8 * runs or check["storage_errors"]:
                        raise RuntimeError(f"JSONL correctness check failed: {check}")
                if check:
                    results.setdefault(stage + "_checks", []).append(check)
            print(f"recording {stage}: trial {trial + 1}/{trials} complete", flush=True)
        results[stage] = {"raw_ns": samples, "median_ns": statistics.median(samples),
                          "ns_per_workflow": [v / runs for v in samples],
                          "median_ns_per_workflow": statistics.median(samples) / runs}
    return results


def filesystem_probe(iterations):
    """Measure each writer filesystem primitive with fixed synthetic payload."""
    results = {}
    with tempfile.TemporaryDirectory(prefix="recording-fs-probe-") as temp:
        root = Path(temp)
        parent = root / "nested"
        file = parent / "probe.jsonl"
        payload = b'{"synthetic":true}\n'
        parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(payload)
        cases = {
            "mkdir_exist_ok": lambda: parent.mkdir(parents=True, exist_ok=True),
            "exists_and_stat": lambda: (file.exists(), file.stat().st_size if file.exists() else 0),
            "open_read_tail": lambda: read_tail(file),
            "open_append": lambda: append_line(file, payload),
        }
        for name, operation in cases.items():
            samples = []
            for _ in range(iterations):
                started = time.perf_counter_ns()
                operation()
                samples.append(time.perf_counter_ns() - started)
            results[name] = {"raw_ns": samples, "median_ns": statistics.median(samples)}
    return results


def measure_jsonl_pairs(runs, trials, baseline_store):
    """Alternate old/new disk trials, each with a common untimed noop warmup."""
    classes = {"current": JsonlStore, "baseline": baseline_store}
    samples = {name: [] for name in classes}
    checks = {name: [] for name in classes}
    orders = []
    for trial in range(trials):
        order = ("current", "baseline") if trial % 2 == 0 else ("baseline", "current")
        orders.append(list(order))
        for name in order:
            with tempfile.TemporaryDirectory(prefix=f"recording-{name}-") as temp:
                warmup = Recorder(NullSink())
                for _ in range(10):
                    nested_run(warmup)
                store = classes[name](Path(temp) / "spans.jsonl")
                recorder = Recorder(store)
                started = time.perf_counter_ns()
                for _ in range(runs):
                    nested_run(recorder)
                samples[name].append(time.perf_counter_ns() - started)
                latest = store.read()  # untimed verification
                lines = store.path.read_bytes().count(b"\n")
                check = {"latest_records": len(latest), "physical_lines": lines,
                         "storage_errors": store.diagnostics["storage_errors"]}
                if len(latest) != 4 * runs or lines != 8 * runs or check["storage_errors"]:
                    raise RuntimeError(f"{name} JSONL correctness check failed: {check}")
                checks[name].append(check)
        print(f"paired JSONL trial {trial + 1}/{trials} complete ({order[0]} then {order[1]})", flush=True)
    return {"trial_order": orders, **{name: {"raw_ns": values, "median_ns": statistics.median(values),
                   "ns_per_workflow": [value / runs for value in values],
                   "median_ns_per_workflow": statistics.median(values) / runs,
                   "checks": checks[name]} for name, values in samples.items()}}


def read_tail(file):
    if file.exists() and file.stat().st_size:
        with file.open("rb") as stream:
            stream.seek(-1, os.SEEK_END)
            return stream.read(1)
    return b""


def append_line(file, payload):
    descriptor = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(payload)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=30, help="synthetic workflows per trial (default: 30)")
    parser.add_argument("--trials", type=int, default=3, help="repetitions per stage (default: 3)")
    parser.add_argument("--baseline-source", type=Path,
                        help="optional preserved storage.py source for same-process old/new comparison")
    parser.add_argument("--filesystem-iterations", type=int, default=100,
                        help="bounded synthetic primitive timings (default: 100)")
    parser.add_argument("--output", type=Path, default=Path("work/recording-probe/results.json"))
    args = parser.parse_args()
    if args.runs < 1 or args.trials < 1 or args.filesystem_iterations < 1:
        parser.error("runs, trials, and filesystem-iterations must be positive")
    payload = {"python": sys.version, "platform": platform.platform(), "runs": args.runs,
               "trials": args.trials,
               "conditions": "Fresh temporary store per trial; synchronous nested workflow/agent/skill/model; synthetic token usage; 8 start/terminal JSONL revisions per workflow; no network; serialization sink captures JSON strings; disabled, no-op, capture, and disk stages; filesystem microbench uses bounded synthetic bytes.",
               "recording": measure_recording(args.runs, args.trials),
               "filesystem": filesystem_probe(args.filesystem_iterations)}
    if args.baseline_source:
        old_store = load_baseline(args.baseline_source)
        print("starting paired preserved/current JSONL comparison", flush=True)
        payload["paired_jsonl"] = measure_jsonl_pairs(args.runs, args.trials, old_store)
        payload["profiles_10_workflows"] = {}
        for name, store_class in (("baseline", old_store), ("current", JsonlStore)):
            print(f"profiling {name} JSONL at 10 synthetic workflows", flush=True)
            with tempfile.TemporaryDirectory(prefix=f"recording-profile-{name}-") as temp:
                profiler = cProfile.Profile()
                profiler.enable()
                store = store_class(Path(temp) / "profile.jsonl")
                recorder = Recorder(store)
                for _ in range(10):
                    nested_run(recorder)
                profiler.disable()
                stats = pstats.Stats(profiler).strip_dirs().sort_stats("cumulative")
                cumulative = io.StringIO()
                stats.stream = cumulative
                stats.print_stats(20)
                stats.sort_stats("tottime")
                tottime = io.StringIO()
                stats.stream = tottime
                stats.print_stats(20)
                payload["profiles_10_workflows"][name] = {
                    "top_cumulative": cumulative.getvalue(), "top_tottime": tottime.getvalue()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"saved synthetic benchmark results to {args.output.resolve()}", flush=True)


if __name__ == "__main__":
    main()
