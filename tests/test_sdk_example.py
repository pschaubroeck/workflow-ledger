import json

from examples.sdk_attachment import run
from workflow_ledger.model import stable_id
from workflow_ledger.storage import JsonlStore


def test_local_sdk_attachment_example_records_selected_safe_fixture(tmp_path, capsys):
    path = tmp_path / "records.jsonl"
    summary = run(path)
    printed = json.loads(capsys.readouterr().out)
    assert printed == summary
    assert summary["provider_call_made"] is False
    assert summary["model_span_is_fixture_activity"] is True
    assert summary["safe_result"] == [20, 20]
    assert summary["included_agent_count"] == 2
    assert summary["diagnostics"]["recorder"]["delivery_errors"] == 0
    assert summary["diagnostics"]["sdk"]["rejected_records"] == 0
    assert summary["model_usage"] and all(usage is None for usage in summary["model_usage"])

    records = JsonlStore(path).read()
    assert not any(r.kind == "agent" and r.identity.logical_id in {
        stable_id("openai-agents-agent", "excluded-a"),
        stable_id("openai-agents-agent", "excluded-b"),
    } for r in records)
    by_kind_name = {(r.kind, r.name): r for r in records}
    native_agents = [r for r in records if r.kind == "agent"]
    assert len(native_agents) == 2
    assert {r.identity.logical_id for r in native_agents} == {
        stable_id("openai-agents-agent", "included-a"),
        stable_id("openai-agents-agent", "included-b"),
    }
    assert all(r.parent_span_id is not None for r in native_agents)
    local_tools = [r for r in records if r.kind == "tool" and r.name == "local-number-transform"]
    assert len(local_tools) == 2
    for tool in local_tools:
        assert any(a.kind == "agent" and a.measured for a in tool.associations)
        assert tool.parent_span_id is not None  # Native function_span remains physical parent.
    models = [r for r in records if r.kind == "model"]
    assert len(models) == 2 and all(r.usage is None for r in models)
    assert all(any(a.kind == "agent" and a.measured for a in r.associations) for r in models)
    model_agents = {next(a.identity.logical_id for a in r.associations
                         if a.kind == "agent" and a.measured): r for r in models}
    for agent_id, model in model_agents.items():
        assert any(a.kind == "skill" and a.measured and a.identity.logical_id ==
                   ("included-a-skill" if agent_id == stable_id("openai-agents-agent", "included-a")
                    else "included-b-skill") for a in model.associations)
    invoked_skills = [r for r in records if r.kind == "skill" and r.lifecycle == "completed"]
    assert len(invoked_skills) == 2
    # Explicit tools nest under the skill; native model spans keep the physical
    # SDK function-span parent that encloses the skill wrapper.
    assert {r.parent_span_id for r in local_tools} == {r.span_id for r in invoked_skills}
    assert {r.parent_span_id for r in models} == {r.parent_span_id for r in invoked_skills}

    skill = next(r for r in records if r.kind == "skill" and r.lifecycle == "loaded")
    assert skill.lifecycle == "loaded"
    loaded_context = next(a for r in local_tools for a in r.associations
                          if a.kind == "skill" and a.identity.logical_id == "example.loaded-skill")
    assert (loaded_context.state, loaded_context.evidence, loaded_context.invocation_span_id) == (
        "loaded", "contextual", None)
    assert all(any(a.kind == "skill" and a.measured and a.identity.namespace == "example"
                   for a in r.associations) for r in local_tools)
    failed = by_kind_name[("tool", "expected-failure-fixture")]
    cancelled = by_kind_name[("tool", "expected-cancellation-fixture")]
    assert (failed.lifecycle, failed.outcome) == ("failed", "error")
    assert (cancelled.lifecycle, cancelled.outcome) == ("cancelled", "cancelled")
    for model in models:
        assert any(a.kind == "skill" and a.measured and a.identity.namespace == "example"
                   for a in model.associations)
    assert not any(a.kind == "skill" and a.measured and a.identity.logical_id == "example.loaded-skill"
                   for r in records for a in r.associations)
    payload = path.read_text()
    for secret in ("synthetic local failure", "private argument"):
        assert secret not in payload


def test_runtime_disabled_policy_returns_before_store_or_sdk_setup(tmp_path, capsys):
    policy = tmp_path / "disabled.json"
    policy.write_text('{"enabled": false}')
    destination = tmp_path / "must-not-exist.jsonl"
    summary = run(destination, policy)
    assert json.loads(capsys.readouterr().out) == summary
    assert summary["disabled"] is True
    assert not destination.exists()
