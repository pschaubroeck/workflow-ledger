import json
import subprocess
import sys

from workflow_ledger.cli import main
from workflow_ledger.model import AssetIdentity
from workflow_ledger.policy import CollectionPolicy, Selector
from workflow_ledger.sessions import SessionStore


def test_disabled_cli_reads_and_writes_nothing(tmp_path, capsys):
    store = tmp_path / "must-not-exist"
    assert main(["--disabled", "import-codex", str(tmp_path / "absent"), "--store", str(store)]) == 0
    assert json.loads(capsys.readouterr().out)["disabled"] is True
    assert not store.exists()


def test_demo_report_and_unknown_workflow(tmp_path, capsys):
    store = str(tmp_path / "store.jsonl")
    assert main(["demo", "--scenario", "parallel", "--store", store]) == 0
    capsys.readouterr()
    assert main(["report", "--store", store, "--group-by", "skill"]) == 0
    summary = json.loads(capsys.readouterr().out)
    skill = next(g for g in summary["groups"] if g["group"].startswith("caller:example.research"))
    assert skill["tokens"]["input_tokens"]["observed"] == 100
    assert skill["tokens"]["input_tokens"]["source"] == {"synthetic": 1}
    assert main(["workflow", "nonexistent", "--store", store]) == 1


def test_text_workflow_explains_selected_skill_parent(tmp_path, capsys):
    store = tmp_path / "store.jsonl"
    workflow_identity = AssetIdentity("caller", "observability-context")
    skill_identity = AssetIdentity("personal", "ps-ascii-diagrams")
    policy = CollectionPolicy(include=(
        Selector("workflow", workflow_identity.key),
        Selector("skill", skill_identity.key),
    ))
    parent = SessionStore(store, policy).start(
        "workflow", workflow_identity, session_id="task", invocation_id="context", timestamp=100
    )
    child = SessionStore(store, policy).start(
        "skill", skill_identity, session_id="task", invocation_id="skill", parent=parent, timestamp=110
    )
    SessionStore(store, policy).finish(child["trace_id"], child["span_id"], outcome="ok", timestamp=150)
    SessionStore(store, policy).finish(parent["trace_id"], parent["span_id"], outcome="ok", timestamp=160)

    assert main(["workflow", parent["trace_id"], "--store", str(store), "--format", "text"]) == 0
    output = capsys.readouterr().out
    assert "caller:observability-context" in output
    assert "personal:ps-ascii-diagrams" in output
    assert "Parent: workflow caller:observability-context." in output


def test_optional_integrations_not_imported_by_core():
    script = "import sys, workflow_ledger; assert 'agents' not in sys.modules; assert 'opentelemetry.sdk' not in sys.modules"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_explicit_header_env_validation_does_not_expose_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEST_OTLP_HEADERS", '"PRIVATE_SENTINEL"')
    result = main(["export-otel", "--store", str(tmp_path / "empty"),
                   "--endpoint", "https://example.invalid", "--headers-env", "TEST_OTLP_HEADERS"])
    assert result == 2
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().out


def test_policy_disabled_and_runtime_excluded_skip_source_reads(tmp_path, capsys):
    store = tmp_path / "absent-store"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"enabled": False}))
    assert main(["--policy", str(policy), "export-otel", "--store", str(store),
                 "--endpoint", "https://example.invalid"]) == 0
    assert json.loads(capsys.readouterr().out)["disabled"]
    policy.write_text(json.dumps({"exclude": [{"scope": "runtime", "identity": "codex"}]}))
    assert main(["--policy", str(policy), "import-codex", str(tmp_path / "absent-source"),
                 "--store", str(store)]) == 0
    assert json.loads(capsys.readouterr().out)["runtime_filtered"]
    assert not store.exists()
    assert main(["--disabled", "--policy", str(tmp_path / "absent-policy"),
                 "report", "--store", str(store)]) == 0


def test_attachment_demo_selection_and_query_filtering(tmp_path, capsys):
    store = tmp_path / "store.jsonl"
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"include": [{"scope": "agent", "identity": "caller:example.included"}]}))
    assert main(["demo", "--scenario", "attachment", "--store", str(store)]) == 0
    capsys.readouterr()
    assert main(["--policy", str(policy), "report", "--store", str(store)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["groups"][0]["tokens"]["input_tokens"]["observed"] == 100
    assert report["query_filtered_records"] > 0
    assert report["filtered_ancestry_gaps"] == 1
    assert report["orphan_parent_links"] == 0


def test_init_cli_previews_then_applies_selection(tmp_path, capsys):
    policy = tmp_path / "policy" / "policy.json"
    store = tmp_path / "store" / "observations.jsonl"
    common = [
        "init", "--select", "skill=personal:example",
        "--policy", str(policy), "--store", str(store),
    ]

    assert main(common) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["status"] == "preview"
    assert preview["include"] == [{"scope": "skill", "identity": "personal:example"}]
    assert not policy.exists()
    assert not store.exists()

    assert main([*common, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert CollectionPolicy.load(policy).include == (Selector("skill", "personal:example"),)
    assert not store.exists()


def test_init_cli_defaults_to_narrow_baseline(tmp_path, capsys):
    policy = tmp_path / "policy.json"
    store = tmp_path / "observations.jsonl"

    assert main(["init", "--policy", str(policy), "--store", str(store)]) == 0
    preview = json.loads(capsys.readouterr().out)

    assert preview["status"] == "preview"
    assert preview["collection_enabled"] is True
    assert preview["selection_mode"] == "selected"
    assert preview["include"] == [
        {"scope": "skill", "identity": "personal:workflow-ledger-operations"},
        {"scope": "workflow", "identity": "caller:observability-context"},
    ]
    assert preview["exclude"] == []
    assert not policy.exists()
    assert not store.exists()


def test_init_cli_reports_invalid_selector_as_json_error(tmp_path, capsys):
    assert main([
        "init", "--select", "malformed", "--policy", str(tmp_path / "policy.json"),
        "--store", str(tmp_path / "observations.jsonl"),
    ]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == "initialization_blocked"
    assert "SCOPE=IDENTITY" in result["reason"]
