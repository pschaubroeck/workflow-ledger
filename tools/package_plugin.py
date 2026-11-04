"""Build a private local plugin archive from the canonical companion skill."""

import argparse
import json
from pathlib import Path
import shutil
import tempfile
import zipfile


def package(output: Path, marketplace: Path | None = None, format: str = "portable") -> Path:
    """Build portable assets, or Codex assets without the competing root manifest."""
    if format not in {"portable", "codex"}:
        raise ValueError("Plugin format must be portable or codex")
    root = Path(__file__).resolve().parents[1]
    name = "workflow-ledger"
    with tempfile.TemporaryDirectory() as temporary:
        stage = Path(temporary) / name
        shutil.copytree(root / "plugins" / name, stage,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        skill = "workflow-ledger-operations"
        shutil.copytree(root / ".agents" / "skills" / skill, stage / "skills" / skill)
        # Validate both source manifests before selecting the target format.
        native = json.loads((stage / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        portable = json.loads((stage / "plugin.json").read_text(encoding="utf-8"))
        if any(native[key] != portable[key] for key in ("name", "version")):
            raise ValueError("Native and portable plugin identities must match")
        for key in ("skills", "hooks"):
            if not (stage / native[key]).exists():
                raise ValueError(f"Native plugin {key} path is missing")
        if format == "codex":
            # Codex 0.160 prioritizes root plugin.json over its native manifest;
            # that portable loader does not load bundled hooks.
            (stage / "plugin.json").unlink()
        if marketplace is not None:
            # New destination only: never overwrite an operator's existing catalog.
            marketplace.mkdir(parents=True, exist_ok=False)
            shutil.copytree(stage, marketplace / "plugins" / name)
            catalog = marketplace / ".agents" / "plugins" / "marketplace.json"
            catalog.parent.mkdir(parents=True)
            catalog.write_text(json.dumps({
                "name": "workflow-ledger-local",
                "interface": {"displayName": "Workflow Ledger Local"},
                "plugins": [{"name": name,
                             "source": {"source": "local", "path": f"./plugins/{name}"},
                             "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
                             "category": "Productivity"}],
            }, indent=2) + "\n", encoding="utf-8")
        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file() and not any(p == "__pycache__" for p in path.parts):
                    archive.write(path, path.relative_to(stage.parent))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--format", choices=("portable", "codex"), default="portable",
                        help="Use codex for native hook discovery; portable preserves root plugin.json")
    parser.add_argument("--marketplace-dir", type=Path,
                        help="Create a new local marketplace directory without installing it")
    args = parser.parse_args()
    print(package(args.output, args.marketplace_dir, args.format))


if __name__ == "__main__":
    main()
