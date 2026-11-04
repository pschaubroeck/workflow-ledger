"""Local OpenAI Agents SDK attachment demonstration; no provider call is made.

The SDK generation span below is a fixture for native model-span observation.
The function span runs deterministic local Python work only.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from workflow_ledger.model import AssetIdentity, Association, stable_id
from workflow_ledger.policy import CollectionPolicy, Selector
from workflow_ledger.recorder import Recorder
from workflow_ledger.storage import JsonlStore


def policy_for_demo() -> CollectionPolicy:
    # Two native agents are included and two are excluded by exact identity.
    inc = [f"openai-agents:{stable_id('openai-agents-agent', n)}" for n in ("included-a", "included-b")]
    exc = [f"openai-agents:{stable_id('openai-agents-agent', n)}" for n in ("excluded-a", "excluded-b")]
    return CollectionPolicy(include=tuple(Selector("agent", x) for x in inc),
                            exclude=tuple(Selector("agent", x) for x in exc))


def run(store_path: str | Path, policy_path: str | Path | None = None) -> dict:
    policy = CollectionPolicy.load(policy_path) if policy_path else policy_for_demo()
    if not policy.may_observe_runtime("openai-agents"):
        summary = {"disabled": True, "provider_call_made": False, "record_count": 0}
        print(json.dumps(summary, sort_keys=True))
        return summary
    # Optional SDK imports happen only after disabled/runtime-excluded paths.
    from agents import agent_span, function_span, generation_span, trace
    from agents.tracing import get_trace_provider, set_trace_processors
    from workflow_ledger.adapters.openai_agents import (
        bind_current_sdk_context, install_local_tracing,
    )

    store = JsonlStore(store_path)
    recorder = Recorder(store, harness="openai-agents", policy=policy)
    # install_local_tracing replaces the process-global SDK processor list. This
    # executable owns that setup and restores the prior list on every exit path.
    # SDK 0.23.1 exposes no getter; read its current processor tuple before the
    # adapter's documented process-global replacement and restore that tuple.
    previous = list(get_trace_provider()._multi_processor._processors)
    processor = None
    try:
        processor = install_local_tracing(recorder)
        # Concurrent traces demonstrate context isolation. Each has nested native
        # agent/tool/model spans; the tool's actual work remains local and safe.
        async def flow(agent: str, excluded: bool = False) -> int:
            with trace("local-sdk-fixture"):
                with agent_span(agent):
                    with bind_current_sdk_context(recorder, processor):
                        await asyncio.sleep(0)  # force trace context interleaving
                        if not excluded and agent == "included-a":
                            skill = AssetIdentity("caller", "example.loaded-skill", "1")
                            recorder.observe("skill", skill, "loaded")
                            loaded = Association("skill", skill, "loaded", "contextual")
                            with recorder.associate(loaded):
                                result = await tool_path(agent)
                            try:
                                with recorder.span("tool", "expected-failure-fixture"):
                                    raise RuntimeError("synthetic local failure")
                            except RuntimeError:
                                pass
                            try:
                                with recorder.span("tool", "expected-cancellation-fixture"):
                                    raise asyncio.CancelledError()
                            except asyncio.CancelledError:
                                pass
                            return result
                        return await tool_path(agent)

        async def tool_path(agent: str) -> int:
            with function_span("local-number-tool"):
                with bind_current_sdk_context(recorder, processor):
                    skill = AssetIdentity("example", f"{agent}-skill", "1")
                    with recorder.span("skill", f"{agent}-local-skill", identity=skill):
                        await asyncio.sleep(0)  # keep concurrent skill contexts active
                        with recorder.span("tool", "local-number-transform"):
                            result = sum((2, 3, 5)) * 2
                        # Native fixture only: deliberately omit provider usage.
                        with generation_span(model="fixture-model"):
                            pass
                return result

        async def execute():
            return await asyncio.gather(flow("included-a"), flow("included-b"),
                                        flow("excluded-a"), flow("excluded-b"))
        values = asyncio.run(execute())
    finally:
        set_trace_processors(previous)

    records = store.read()
    summary = {
        "provider_call_made": False,
        "model_span_is_fixture_activity": True,
        "safe_result": values[:2],
        "record_count": len(records),
        "included_agent_count": sum(r.kind == "agent" for r in records),
        "kinds": sorted({r.kind for r in records}),
        "model_usage": [r.to_dict()["usage"] for r in records if r.kind == "model"],
        "diagnostics": {"recorder": recorder.diagnostics, "sdk": processor.diagnostics,
                        "store": store.diagnostics},
    }
    print(json.dumps(summary, sort_keys=True))
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, help="JSONL output file")
    parser.add_argument("--policy", help="optional collection policy JSON")
    parser.add_argument("--disabled", action="store_true", help="exit without SDK imports or file writes")
    args = parser.parse_args(argv)
    if args.disabled:
        print(json.dumps({"disabled": True, "provider_call_made": False}))
        return 0
    run(args.store, args.policy)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
