import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from workflow_ledger.storage import JsonlStore

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "workflow-ledger"


def test_built_plugin_contains_canonical_skill_and_runnable_hooks(tmp_path):
    spec = importlib.util.spec_from_file_location("package_plugin", ROOT / "tools/package_plugin.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    marketplace = tmp_path / "marketplace"
    output = builder.package(tmp_path / "plugin.zip", marketplace)
    catalog = json.loads((marketplace / ".agents/plugins/marketplace.json").read_text())
    installable = marketplace / catalog["plugins"][0]["source"]["path"]
    assert catalog["plugins"][0]["name"] == "workflow-ledger"
    assert catalog["plugins"][0]["source"]["path"] == "./plugins/workflow-ledger"
    assert (installable / "plugin.json").is_file()
    assert (installable / "skills/workflow-ledger-operations/SKILL.md").is_file()
    assert catalog["name"] == "workflow-ledger-local"
    assert catalog["interface"]["displayName"] == "Workflow Ledger Local"
    with zipfile.ZipFile(output) as archive:
        prefix = "workflow-ledger/"
        manifest = json.loads(archive.read(prefix + "plugin.json"))
        assert manifest["name"] == "workflow-ledger"
        assert manifest["extensions"]["com.openai"]["interface"]["displayName"] == "Workflow Ledger"
        assert manifest["version"] == "0.3.0"
        extension = manifest["extensions"]["com.openai"]
        assert len(extension["interface"]["shortDescription"]) <= 30
        hooks = json.loads(archive.read(prefix + extension["hooks"][2:]))["hooks"]
        assert set(hooks) == {"SessionStart", "SessionEnd", "PreToolUse", "PostToolUse",
                              "SubagentStart", "SubagentStop"}
        assert archive.read(prefix + "hooks/dispatch.py") == (PLUGIN / "hooks/dispatch.py").read_bytes()
        skill = "workflow-ledger-operations"
        skill_text = archive.read(prefix + f"skills/{skill}/SKILL.md").decode()
        assert "name: workflow-ledger-operations" in skill_text
        assert "# Workflow Ledger Operations" in skill_text
        assert archive.read(prefix + f"skills/{skill}/SKILL.md") == (
            ROOT / ".agents/skills" / skill / "SKILL.md").read_bytes()
        assert not any("__pycache__" in name or name.endswith(".jsonl") for name in archive.namelist())


def test_codex_package_bundles_declared_skill_path(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "package_plugin", ROOT / "tools/package_plugin.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    output = builder.package(tmp_path / "codex-plugin.zip", format="codex")
    with zipfile.ZipFile(output) as archive:
        prefix = "workflow-ledger/"
        manifest = json.loads(archive.read(prefix + ".codex-plugin/plugin.json"))
        skill_dir = manifest["skills"].removeprefix("./")
        skill = "workflow-ledger-operations"
        skill_path = prefix + f"{skill_dir}{skill}/SKILL.md"
        assert manifest["skills"] == "./skills/"
        assert skill_path in archive.namelist()
        assert not any(name == prefix + "plugin.json" for name in archive.namelist())
        assert archive.read(skill_path) == (
            ROOT / ".agents/skills" / skill / "SKILL.md").read_bytes()


def launch(payload, **configuration):
    env = {key: value for key, value in os.environ.items() if not key.startswith("WORKFLOW_LEDGER_")}
    env.update(configuration)
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.run([sys.executable, str(PLUGIN / "hooks/dispatch.py")],
                          input=json.dumps(payload), text=True, capture_output=True, env=env)


def test_plugin_disabled_and_missing_configuration_are_fail_open(tmp_path):
    store = tmp_path / "must-not-exist.jsonl"
    disabled = launch({"secret": "private payload"}, WORKFLOW_LEDGER_STORE=str(store))
    assert disabled.returncode == 0 and json.loads(disabled.stdout) == {} and not disabled.stderr
    bad = launch({"secret": "private payload"}, WORKFLOW_LEDGER_ENABLED="1", WORKFLOW_LEDGER_STORE=str(store))
    assert bad.returncode == 0 and json.loads(bad.stdout) == {}
    assert json.loads(bad.stderr) == {"error": "missing_absolute_configuration"}
    assert not store.exists()


def test_configured_plugin_launcher_delivers_sanitized_fixture(tmp_path):
    store = tmp_path / "hook.jsonl"
    result = launch({"hook_event_name": "SessionStart", "session_id": "raw-private-id",
                     "source": "startup", "cwd": "/private/location",
                     "transcript_path": "/private/transcript", "prompt": "private prompt"},
                    WORKFLOW_LEDGER_ENABLED="1", WORKFLOW_LEDGER_PYTHON=sys.executable, WORKFLOW_LEDGER_STORE=str(store))
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    records = JsonlStore(store).read()
    assert len(records) == 1 and records[0].kind == "workflow"
    payload = store.read_text()
    for forbidden in ("raw-private-id", "/private/location", "/private/transcript", "private prompt"):
        assert forbidden not in payload


def test_plugin_surfaces_invalid_retention_settings_safely(tmp_path):
    store = tmp_path / "hook.jsonl"
    settings = tmp_path / "retention.json"
    settings.write_text('{"rotation_bytes": 100, "archive_cap_bytes": 50}', encoding="utf-8")
    result = launch({"hook_event_name": "SessionStart", "session_id": "session"},
                    WORKFLOW_LEDGER_ENABLED="1", WORKFLOW_LEDGER_PYTHON=sys.executable, WORKFLOW_LEDGER_STORE=str(store),
                    WORKFLOW_LEDGER_RETENTION_POLICY=str(settings))
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert json.loads(result.stderr) == {"error": "retention_policy_error"}
    assert not store.exists()


def test_launcher_forwards_lock_contention_without_untrusted_diagnostics(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("hook_dispatch", PLUGIN / "hooks/dispatch.py")
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    monkeypatch.setenv("WORKFLOW_LEDGER_ENABLED", "1")
    monkeypatch.setenv("WORKFLOW_LEDGER_PYTHON", sys.executable)
    monkeypatch.setenv("WORKFLOW_LEDGER_STORE", "/chosen/store.jsonl")
    response = subprocess.CompletedProcess([], 0, stderr=json.dumps({
        "diagnostics": {"lock_timeouts": 1, "storage_errors": 0,
                        "permission_denied": 2, "storage_full": 1,
                        "storage_unavailable": 3, "private_payload": "secret",
                        "filesystem_path": "/private/store", "observed": -1}}).encode())
    monkeypatch.setattr(dispatch.subprocess, "run", lambda *args, **kwargs: response)
    assert dispatch.main() == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == {}
    assert json.loads(output.err) == {"diagnostics": {
        "lock_timeouts": 1, "storage_errors": 0, "permission_denied": 2,
        "storage_full": 1, "storage_unavailable": 3,
    }}
    assert "/private/store" not in output.err and "secret" not in output.err
