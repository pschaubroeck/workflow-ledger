from __future__ import annotations

import asyncio

from agents.tracing import agent_span, custom_span, function_span, generation_span, response_span, trace

from workflow_ledger.adapters.openai_agents import (
    OpenAIAgentsTraceProcessor,
    bind_current_sdk_context,
    install_local_tracing,
)
from workflow_ledger.model import AssetIdentity, Association, stable_id
from workflow_ledger.policy import CollectionPolicy, Selector
from workflow_ledger.recorder import Recorder


class Sink:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)
        return True


def latest_records(records):
    latest = {}
    for record in records:
        latest[record.key] = record
    return list(latest.values())


def test_real_sdk_lifecycle_maps_safe_records_and_token_subsets():
    sink = Sink()
    install_local_tracing(type("Recorder", (), {"emit": lambda _, r: sink.emit(r)})())
    try:
        with trace("workflow-secret-sentinel"):
            with agent_span("agent-secret-sentinel"):
                with generation_span(
                    model="gpt-4.1-mini",
                    usage={"input_tokens": 20, "output_tokens": 9,
                           "input_tokens_details": {"cached_tokens": 5},
                           "output_tokens_details": {"reasoning_tokens": 2}},
                ):
                    pass
                with function_span("tool-secret-sentinel", input="private argument", output="private result"):
                    pass
    finally:
        # SDK's setter is global; restore an empty, local-only configuration.
        from agents.tracing import set_trace_processors
        set_trace_processors([])

    assert len(sink.records) >= 5
    assert any(r.lifecycle == "invoked" for r in sink.records)
    records = latest_records(sink.records)
    by_kind = {record.kind: record for record in records}
    workflow, agent, model, tool = (by_kind[k] for k in ("workflow", "agent", "model", "tool"))
    assert workflow.kind == "workflow"
    assert agent.kind == "agent" and agent.parent_span_id == workflow.span_id
    assert model.kind == "model" and model.parent_span_id == agent.span_id
    assert model.usage.input_tokens == 20
    assert model.usage.cached_input_tokens == 5
    assert model.usage.output_tokens == 9
    assert model.usage.reasoning_tokens == 2
    assert model.agent_name == agent.agent_name
    assert tool.kind == "tool" and tool.parent_span_id == agent.span_id
    assert tool.agent_name == agent.agent_name
    assert all(r.trace_id == workflow.trace_id for r in sink.records)
    assert all(r.outcome == "unknown" for r in records)
    serialized = repr([r.to_dict() for r in records])
    for secret in ("workflow-secret-sentinel", "agent-secret-sentinel", "tool-secret-sentinel",
                   "private argument", "private result"):
        assert secret not in serialized


def test_install_local_tracing_replaces_sdk_processor_list():
    from agents.tracing import get_trace_provider, set_trace_processors

    sink = Sink()
    recorder = type("Recorder", (), {"emit": lambda _, r: sink.emit(r)})()
    processor = install_local_tracing(recorder)
    try:
        providers = get_trace_provider()._multi_processor._processors
        assert tuple(providers) == (processor,)
        with trace("local-only-proof"):
            pass
        assert [r.lifecycle for r in sink.records] == ["invoked", "completed"]
    finally:
        set_trace_processors([])


def test_native_workflow_identity_is_stable_across_trace_invocations():
    sink = Sink()
    processor = OpenAIAgentsTraceProcessor(type("Recorder", (), {"emit": lambda _, r: sink.emit(r)})())
    from agents.tracing import set_trace_processors
    set_trace_processors([processor])
    try:
        with trace("same-workflow-name"):
            pass
        with trace("same-workflow-name"):
            pass
    finally:
        set_trace_processors([])
    workflows = [r for r in latest_records(sink.records) if r.kind == "workflow"]
    assert len(workflows) == 2
    assert workflows[0].trace_id != workflows[1].trace_id
    assert workflows[0].identity == workflows[1].identity


def test_concurrent_sdk_traces_keep_distinct_trace_ids():
    sink = Sink()
    processor = OpenAIAgentsTraceProcessor(type("Recorder", (), {"emit": lambda _, r: sink.emit(r)})())
    from agents.tracing import set_trace_processors
    set_trace_processors([processor])
    async def one(label):
        with trace(label):
            with agent_span("worker"):
                await asyncio.sleep(0)
    async def run_both():
        await asyncio.gather(one("first"), one("second"))
    try:
        asyncio.run(run_both())
    finally:
        set_trace_processors([])
    agent_records = [r for r in latest_records(sink.records) if r.kind == "agent"]
    assert len(agent_records) == 2
    assert len({r.trace_id for r in agent_records}) == 2
    workflow_ids = {r.trace_id: r.span_id for r in sink.records if r.kind == "workflow"}
    assert all(r.parent_span_id == workflow_ids[r.trace_id] for r in agent_records)


def test_tool_error_records_outcome_without_error_content():
    from agents.tracing import set_trace_processors

    sink = Sink()
    processor = OpenAIAgentsTraceProcessor(type("Recorder", (), {"emit": lambda _, r: sink.emit(r)})())
    set_trace_processors([processor])
    try:
        with trace("error-workflow"):
            with function_span("tool", input="argument sentinel") as span:
                span.set_error({"message": "error message sentinel", "data": {"secret": "data sentinel"}})
    finally:
        set_trace_processors([])
    tool = next(r for r in latest_records(sink.records) if r.kind == "tool")
    assert tool.outcome == "error"
    assert tool.error_type is None  # The SDK error schema carries no exception class.
    serialized = repr(tool.to_dict())
    assert "sentinel" not in serialized


def test_response_span_reads_only_model_and_token_fields():
    from openai.types.responses import Response, ResponseUsage
    from agents.tracing import set_trace_processors

    sink = Sink()
    processor = OpenAIAgentsTraceProcessor(type("Recorder", (), {"emit": lambda _, r: sink.emit(r)})())
    set_trace_processors([processor])
    response = Response(
        id="resp_native_id", created_at=1, model="gpt-4.1", object="response",
        output=[{"type": "message", "id": "msg_x", "status": "completed", "role": "assistant",
                 "content": [{"type": "output_text", "text": "response body sentinel", "annotations": []}]}],
        parallel_tool_calls=False, tool_choice="auto", tools=[], status="completed",
        usage=ResponseUsage(input_tokens=11, input_tokens_details={"cached_tokens": 4, "cache_write_tokens": 0},
                            output_tokens=7, output_tokens_details={"reasoning_tokens": 3}, total_tokens=18),
    )
    try:
        with trace("response-workflow"):
            with agent_span("response-agent"):
                with response_span(response=response):
                    pass
    finally:
        set_trace_processors([])
    record = next(r for r in latest_records(sink.records) if r.kind == "model")
    assert record.model == "gpt-4.1"
    assert (record.usage.input_tokens, record.usage.cached_input_tokens,
            record.usage.output_tokens, record.usage.reasoning_tokens) == (11, 4, 7, 3)
    assert record.agent_name is not None
    assert "resp_native_id" not in repr(record.to_dict())
    assert "response body sentinel" not in repr(record.to_dict())


def test_child_agent_attribution_and_unsupported_span_cleanup():
    from agents.tracing import set_trace_processors

    sink = Sink()
    processor = OpenAIAgentsTraceProcessor(type("Recorder", (), {"emit": lambda _, r: sink.emit(r)})())
    set_trace_processors([processor])
    try:
        with trace("nested-workflow"):
            with agent_span("outer-agent"):
                with agent_span("inner-agent"):
                    with function_span("inner-tool"):
                        pass
                with custom_span("unmapped"):
                    pass
    finally:
        set_trace_processors([])
    by_kind = [r for r in latest_records(sink.records) if r.kind == "agent"]
    outer = next(r for r in by_kind if r.parent_span_id == next(
        w.span_id for w in sink.records if w.kind == "workflow"))
    inner = next(r for r in by_kind if r.parent_span_id == outer.span_id)
    tool = next(r for r in latest_records(sink.records) if r.kind == "tool")
    assert tool.parent_span_id == inner.span_id
    assert tool.agent_name == inner.agent_name
    assert processor.diagnostics["unsupported_span_kinds"] == 1
    assert not processor._spans and not processor._span_agents


def test_native_identities_policy_filtering_and_invocation_associations():
    from agents.tracing import set_trace_processors

    sink = Sink()
    target_id = f"openai-agents:{stable_id('openai-agents-agent', 'selected-agent')}"
    recorder = Recorder(sink, harness="openai-agents",
                       policy=CollectionPolicy(include=(Selector("agent", target_id),)))
    processor = OpenAIAgentsTraceProcessor(recorder)
    set_trace_processors([processor])
    try:
        with trace("filtered-workflow"):
            with agent_span("ignored-agent"):
                with function_span("ignored-tool"):
                    pass
            with agent_span("selected-agent"):
                with function_span("selected-tool"):
                    with generation_span(model="gpt-4.1-mini"):
                        pass
    finally:
        set_trace_processors([])
    by_kind = {r.kind: r for r in latest_records(sink.records)}
    assert set(by_kind) == {"agent", "tool", "model"}
    agent, tool, model = by_kind["agent"], by_kind["tool"], by_kind["model"]
    assert agent.identity.namespace == "openai-agents" and agent.identity.version is None
    assert agent.identity.logical_id == stable_id("openai-agents-agent", "selected-agent")
    assert all(a.measured for a in tool.associations if a.kind == "agent")
    assert any(a.kind == "tool" and a.measured for a in model.associations)
    assert agent.filtered_parent
    assert not tool.filtered_parent and not model.filtered_parent
    serialized = repr([r.to_dict() for r in sink.records])
    assert "selected-agent" not in serialized and "selected-tool" not in serialized


def test_explicit_skill_association_binds_to_sdk_model_span_without_ownership():
    from agents.tracing import set_trace_processors

    sink = Sink()
    recorder = Recorder(sink, harness="openai-agents")
    processor = OpenAIAgentsTraceProcessor(recorder)
    skill = Association("skill", AssetIdentity("caller", "quarterly-skill", None,
                                                provenance="caller_declared"),
                        "loaded", "caller_declared")
    set_trace_processors([processor])
    try:
        with trace("explicit-skill-workflow"):
            with agent_span("agent"):
                with recorder.associate(skill):
                    with bind_current_sdk_context(recorder, processor):
                        with generation_span(model="gpt-4.1-mini"):
                            pass
    finally:
        set_trace_processors([])
    model = next(r for r in latest_records(sink.records) if r.kind == "model")
    association = next(a for a in model.associations if a.kind == "skill")
    assert association.state == "loaded"
    assert association.evidence == "caller_declared"
    assert not association.measured
    assert association.invocation_span_id is None


def test_disabled_adapter_callbacks_do_not_inspect_span_data_or_change_sdk_setup(monkeypatch):
    import workflow_ledger.adapters.openai_agents as adapter

    class DisabledRecorder:
        enabled = False
        def emit(self, _record):
            raise AssertionError("disabled recorder received record")

    def forbidden(_span):
        raise AssertionError("disabled adapter inspected SDK source")

    class BombSpan:
        span_id = property(forbidden)
        trace_id = property(forbidden)
        parent_id = property(forbidden)
        span_data = property(forbidden)

    processor = adapter.OpenAIAgentsTraceProcessor(DisabledRecorder())
    monkeypatch.setattr(adapter, "set_trace_processors", forbidden)
    assert adapter.install_local_tracing(DisabledRecorder()).diagnostics["disabled"] == 1
    processor.on_span_start(BombSpan())
    processor.on_span_end(BombSpan())
    assert processor.diagnostics["rejected_records"] == 0
    assert processor.diagnostics["callback_conversion_errors"] == 0
    assert processor.diagnostics["delivery_errors"] == 0


def test_callback_conversion_failures_are_bounded_and_content_free():
    class DisabledRecorder:
        enabled = True

        def emit(self, _record):
            return True

    class InvalidSpan:
        @property
        def span_id(self):
            raise RuntimeError("private conversion failure sentinel")

    processor = OpenAIAgentsTraceProcessor(DisabledRecorder())
    processor.MAX_CALLBACK_CONVERSION_ERRORS = 3
    for _ in range(processor.MAX_CALLBACK_CONVERSION_ERRORS + 2):
        processor.on_span_end(InvalidSpan())

    diagnostics = processor.diagnostics
    assert diagnostics["callback_conversion_errors"] == processor.MAX_CALLBACK_CONVERSION_ERRORS
    assert diagnostics["delivery_errors"] == 0
    assert "private conversion failure sentinel" not in repr(diagnostics)


def test_recorder_delivery_failures_are_visible_and_separate_from_callback_errors():
    class RejectingSink:
        def emit(self, _record):
            return False

    recorder = Recorder(RejectingSink(), harness="openai-agents")
    processor = OpenAIAgentsTraceProcessor(recorder)
    native_trace = type("Trace", (), {"trace_id": "native-trace-id", "name": "workflow"})()

    processor.on_trace_start(native_trace)
    processor.on_trace_end(native_trace)
    processor.force_flush()
    before_shutdown = processor.diagnostics
    processor.shutdown()
    after_shutdown = processor.diagnostics

    assert before_shutdown["delivery_errors"] == 2
    assert after_shutdown["delivery_errors"] == 2
    assert after_shutdown["callback_conversion_errors"] == 0


def test_workflow_selection_attaches_explicit_invocation_at_sdk_trace_root():
    from agents.tracing import set_trace_processors
    from workflow_ledger.report import ownership

    sink = Sink()
    workflow_key = f"openai-agents:{stable_id('openai-agents-workflow-asset', 'selected-workflow')}"
    recorder = Recorder(sink, harness="openai-agents",
                        policy=CollectionPolicy(include=(Selector("workflow", workflow_key),)))
    processor = OpenAIAgentsTraceProcessor(recorder)
    set_trace_processors([processor])
    try:
        with trace("selected-workflow"):
            with bind_current_sdk_context(recorder, processor):
                with recorder.span("skill", "explicit", identity=AssetIdentity("caller", "lookup", "1")):
                    with generation_span(model="gpt-4.1-mini"):
                        pass
    finally:
        set_trace_processors([])
    rows = latest_records(sink.records)
    assert {r.kind for r in rows} == {"workflow", "skill", "model"}
    workflow = next(r for r in rows if r.kind == "workflow")
    skill = next(r for r in rows if r.kind == "skill")
    model = next(r for r in rows if r.kind == "model")
    assert skill.parent_span_id == workflow.span_id and skill.trace_id == workflow.trace_id
    assert not skill.filtered_parent
    assert ownership(model, "skill") == skill.identity
    assert ownership(model, "model") == model.identity
    assert all(ownership(r, "workflow") == workflow.identity for r in rows)
