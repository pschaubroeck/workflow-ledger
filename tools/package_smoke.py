"""Exercise setup through the installed wheel's command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile


def _run(cli: Path, arguments: list[str], *, cwd: Path, env: dict[str, str]) -> dict:
    result = subprocess.run(
        [str(cli), *arguments],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError(
            f"{cli.name} {' '.join(arguments)} failed with {result.returncode}: "
            f"stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{cli.name} returned invalid JSON: {result.stdout}") from exc


def verify_installed_package(cli: Path) -> None:
    cli = cli.resolve()
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    with tempfile.TemporaryDirectory(prefix="workflow-ledger-package-smoke-") as raw_root:
        root = Path(raw_root)
        policy = root / "config" / "policy.json"
        store = root / "data" / "observations.jsonl"
        skill_root = root / "assistant" / "skills"
        options = [
            "init",
            "--select", "skill=personal:package-smoke",
            "--policy", str(policy),
            "--store", str(store),
            "--skill-root", str(skill_root),
        ]

        preview = _run(cli, options, cwd=root, env=env)
        skill_path = skill_root / "workflow-ledger-operations"
        if preview.get("status") != "preview" or preview.get("policy_action") != "create":
            raise RuntimeError(f"Unexpected setup preview: {preview}")
        if preview.get("skill_action") != "install":
            raise RuntimeError(f"Expected bundled skill installation in preview: {preview}")
        if policy.exists() or store.exists() or skill_root.exists():
            raise RuntimeError("Setup preview created local files")

        applied = _run(cli, [*options, "--apply"], cwd=root, env=env)
        if applied.get("status") != "applied":
            raise RuntimeError(f"Unexpected setup result: {applied}")
        if not policy.is_file():
            raise RuntimeError("Setup did not write the collection policy")
        policy_data = json.loads(policy.read_text(encoding="utf-8"))
        if policy_data.get("include") != [
            {"scope": "skill", "identity": "personal:package-smoke"}
        ]:
            raise RuntimeError(f"Unexpected installed policy: {policy_data}")
        if not (skill_path / "SKILL.md").is_file():
            raise RuntimeError("Installed wheel did not provide the operations skill")
        marker_path = skill_path / ".workflow-ledger-managed.json"
        if not marker_path.is_file():
            raise RuntimeError("Installed skill is missing its management marker")
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        if marker.get("artifact_type") != "workflow-ledger-skill-installation":
            raise RuntimeError(f"Unexpected skill marker: {marker}")
        if not marker.get("digest"):
            raise RuntimeError(f"Skill marker has no digest: {marker}")
        if store.exists():
            raise RuntimeError("Setup created the observation store before recording data")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", required=True, type=Path, help="Installed wheel CLI path")
    args = parser.parse_args(argv)
    verify_installed_package(args.cli)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
