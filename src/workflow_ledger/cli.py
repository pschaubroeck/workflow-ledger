"""Explicit local CLI; source reads and optional exports require user arguments."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from .bootstrap import DEFAULT_ROOT, initialize, parse_selector
from .model import AssetIdentity, Association, TokenUsage
from .policy import CollectionPolicy
from .recorder import Recorder
from .report import GROUP_FIELDS, explain_report, explain_workflow, summarize
from .retention import RetentionPolicy, resolve_retention_policy
from .storage import JsonlStore


def demo(store: JsonlStore, scenario: str = "success", policy: CollectionPolicy | None = None) -> dict:
    recorder = Recorder(store, harness="example", policy=policy)

    def model_call():
        with recorder.span("model", "example.generation") as activity:
            activity.model = "synthetic-model"
            activity.usage = TokenUsage(input_tokens=100, cached_input_tokens=20,
                                        output_tokens=30, reasoning_tokens=10, source="synthetic")

    async def tool(name: str):
        with recorder.span("tool", name):
            await asyncio.sleep(0)

    async def tools():
        await asyncio.gather(tool("example.lookup"), tool("example.check"))

    with recorder.span("workflow", f"example.{scenario}"):
        if scenario == "attachment":
            recorder.observe("skill", AssetIdentity("caller", "example.loaded-only"), "loaded")
            for name in ("included", "excluded"):
                with recorder.span("agent", f"example.{name}"):
                    with recorder.span("skill", f"example.{name}.skill", skill_version="1"):
                        model_call()
            with recorder.associate(Association("skill", AssetIdentity("caller", "example.loaded-only"))):
                with recorder.span("agent", "example.contextual"):
                    model_call()
            with recorder.span("skill", "example.outer"):
                with recorder.span("skill", "example.inner"):
                    model_call()
            return {"synthetic": True, "recorder": recorder.diagnostics, "store": store.diagnostics}
        with recorder.span("agent", "example.worker"):
            with recorder.span("skill", "example.research", skill_version="1"):
                if scenario == "parallel":
                    asyncio.run(tools())
                else:
                    with recorder.span("tool", "example.lookup"):
                        pass
                model_call()
                if scenario == "failure":
                    try:
                        with recorder.span("tool", "example.failure"):
                            raise ValueError("This content must not be retained")
                    except ValueError:
                        pass
    return {"synthetic": True, "recorder": recorder.diagnostics, "store": store.diagnostics}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Local agent workflow telemetry")
    result.add_argument("--disabled", action="store_true",
                        help="Return before reading, writing, importing adapters, or exporting")
    result.add_argument("--policy", type=Path,
                        help="Explicit JSON selection policy; put before the subcommand")
    result.add_argument("--retention-policy", type=Path,
                        help="Optional JSON rotation and archive-cap settings")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("demo", "import-codex", "report", "workflow", "export-otel", "cost-view"):
        command = commands.add_parser(name)
        command.add_argument("--store", type=Path, required=True)
        if name == "demo":
            command.add_argument("--scenario", choices=("success", "parallel", "failure", "attachment"),
                                 default="success")
        elif name == "import-codex":
            command.add_argument("source", type=Path)
        elif name == "report":
            command.add_argument("--group-by", choices=tuple(GROUP_FIELDS))
            command.add_argument("--format", choices=("json", "text"), default="json")
        elif name == "workflow":
            command.add_argument("trace_id")
            command.add_argument("--format", choices=("json", "text"), default="json")
        elif name == "export-otel":
            command.add_argument("--include-archives", action="store_true",
                                 help="Explicitly include retained JSONL archives in this export")
            command.add_argument("--endpoint", required=True,
                                 help="Explicit OTLP HTTP base URL; no ambient default endpoint")
            command.add_argument("--service-name", default="workflow-ledger")
            command.add_argument("--timeout", type=float, default=5.0)
            command.add_argument("--headers-env",
                                 help="Explicit environment variable containing a JSON header object")
    capabilities = commands.add_parser("capabilities")
    capabilities.add_argument("adapter", choices=("explicit", "codex", "agents", "codex-hooks"))
    init = commands.add_parser("init", help="Preview or initialize local collection and AI guidance")
    selection = init.add_mutually_exclusive_group()
    selection.add_argument("--select", action="append", metavar="SCOPE=IDENTITY",
                           help="Select exact assets; repeat for additional assets (overrides the baseline)")
    selection.add_argument("--all", action="store_true",
                           help="Explicitly select all supported records")
    selection.add_argument("--disabled", dest="init_disabled", action="store_true",
                           help="Install AI guidance but leave collection disabled")
    init.add_argument("--store", type=Path, default=DEFAULT_ROOT / "observations.jsonl",
                      help="Local JSONL store path")
    init.add_argument("--policy", dest="init_policy", type=Path,
                      default=argparse.SUPPRESS, help="Selection policy path")
    init.add_argument("--exclude", action="append", default=[], metavar="SCOPE=IDENTITY",
                      help="Exclude one exact asset; repeat as needed")
    init.add_argument("--skill-root", type=Path,
                      help="Install the bundled operations skill under this harness skill directory")
    init.add_argument("--replace-policy", action="store_true",
                      help="Replace a different existing policy after reviewing the preview")
    apply_mode = init.add_mutually_exclusive_group()
    apply_mode.add_argument("--dry-run", dest="init_apply", action="store_false",
                            help="Show intended changes without writing (the default)")
    apply_mode.add_argument("--apply", dest="init_apply", action="store_true",
                            help="Apply the previewed local changes")
    init.set_defaults(init_apply=False)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.disabled:
        print(json.dumps({"disabled": True}))
        return 0
    try:
        if args.command == "init":
            try:
                selected = tuple(parse_selector(value) for value in (args.select or ()))
                if not selected and not args.all and not args.init_disabled:
                    selected = (
                        parse_selector("skill=personal:workflow-ledger-operations"),
                        parse_selector("workflow=caller:observability-context"),
                    )
                excluded = tuple(parse_selector(value) for value in args.exclude)
                output = initialize(
                    policy_path=(getattr(args, "init_policy", None) or args.policy
                                 or DEFAULT_ROOT / "policy.json"),
                    store_path=args.store,
                    include=selected,
                    exclude=excluded,
                    all_records=args.all,
                    disabled=args.init_disabled,
                    skill_root=args.skill_root,
                    apply=args.init_apply,
                    replace_policy=args.replace_policy,
                )
            except ValueError as exc:
                print(json.dumps({"error": "initialization_blocked", "reason": str(exc)}))
                return 2
            except OSError:
                print(json.dumps({"error": "initialization_filesystem_error"}))
                return 2
            print(json.dumps(output, indent=2))
            return 0
        policy = CollectionPolicy.load(args.policy) if args.policy else CollectionPolicy()
        if not policy.enabled:
            print(json.dumps({"disabled": True}))
            return 0
        runtime = {"demo": "example", "import-codex": "codex"}.get(args.command)
        if runtime is not None and not policy.may_observe_runtime(runtime):
            print(json.dumps({"disabled": True, "runtime_filtered": True}))
            return 0
        if args.command == "capabilities":
            if args.adapter == "explicit":
                capability = Recorder.capabilities
            elif args.adapter == "codex":
                from .adapters.codex import CAPABILITIES
                capability = CAPABILITIES
            elif args.adapter == "codex-hooks":
                from .adapters.codex_hooks import CAPABILITIES
                capability = CAPABILITIES
            else:
                from .adapters.openai_agents import CAPABILITIES
                capability = CAPABILITIES
            output = capability.to_dict()
        else:
            retention = (RetentionPolicy.load(args.retention_policy) if args.retention_policy
                         else resolve_retention_policy())
            store = JsonlStore(args.store, retention=retention)
            if args.command == "demo":
                output = demo(store, args.scenario, policy=policy)
                if output["recorder"]["delivery_errors"] or output["store"]["storage_errors"]:
                    print(json.dumps(output))
                    return 1
            elif args.command == "import-codex":
                from .adapters.codex import import_codex
                if args.source.resolve() == args.store.resolve():
                    raise ValueError("Source and telemetry store must be different files")
                result = import_codex(args.source, policy=policy)
                delivered = sum(store.emit(record) for record in result.records)
                output = {"records_observed": len(result.records), "records_delivered": delivered,
                          "import": result.diagnostics, "store": store.diagnostics}
                if (not result.records and not result.diagnostics.get("filtered_records", 0)) or delivered != len(result.records):
                    print(json.dumps(output))
                    return 1
            elif args.command == "export-otel":
                from .otel import export_records
                headers = {}
                if args.headers_env:
                    headers = json.loads(os.environ.get(args.headers_env, ""))
                    if not isinstance(headers, dict):
                        raise ValueError("Headers must be a JSON object")
                output = export_records(
                    policy.select_records(store.read(include_archives=args.include_archives)),
                    endpoint=args.endpoint,
                                        service_name=args.service_name, timeout=args.timeout,
                                        headers=headers)
                # Failed delivery is visible to command callers; instrumented workflows stay free.
                if output.get("export_errors", 0):
                    print(json.dumps(output))
                    return 1
            elif args.command == "workflow":
                records = [r for r in policy.select_records(store.read()) if r.trace_id == args.trace_id]
                if args.format == "text":
                    print(explain_workflow(records, store.diagnostics))
                    return 0 if records else 1
                output = {"records": [r.to_dict() for r in records],
                          "summary": summarize(records), "store": store.diagnostics}
                if not records:
                    print(json.dumps(output))
                    return 1
            else:
                records = store.read()
                selected = policy.select_records(records)
                output = summarize(selected, getattr(args, "group_by", None))
                output["query_filtered_records"] = len(records) - len(selected)
                output["store"] = store.diagnostics
                if args.command == "cost-view":
                    output["consumer_contract"] = {
                        "purpose": "Observed token aggregates for external analyzers",
                        "accounting": "Root run counts override model-call counts per field",
                        "sqlite": "Not included; remains an independent cross-check",
                        "attribution": "Supported record metadata only; unknowns remain unknown",
                        "synthetic_usage": "Retain token source labels; synthetic data is not real usage",
                    }
                elif args.format == "text":
                    print(explain_report(output))
                    return 0
        print(json.dumps(output, indent=2))
        return 0
    except ImportError:
        print(json.dumps({"error": "Optional integration dependency is unavailable"}))
        return 2
    except (OSError, UnicodeError, ValueError) as exc:
        # Paths and source content can be present in exception messages.
        print(json.dumps({"error": type(exc).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
