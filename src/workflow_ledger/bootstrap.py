"""Safe local initialization for an installed Workflow Ledger package."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .policy import CollectionPolicy, Selector


DEFAULT_ROOT = Path.home() / ".workflow-ledger"
SKILL_NAME = "workflow-ledger-operations"
SKILL_MARKER = ".workflow-ledger-managed.json"


def _skill_source() -> Path:
    bundled = Path(__file__).parent / "_bundled" / "skills" / SKILL_NAME
    if bundled.is_dir():
        return bundled
    checkout = Path(__file__).resolve().parents[2] / ".agents" / "skills" / SKILL_NAME
    if checkout.is_dir():
        return checkout
    raise ValueError("The packaged operations skill is unavailable")


def _tree_digest(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError("Skill directories containing symlinks are unsupported")
        if item.is_file() and item.name != SKILL_MARKER:
            relative = item.relative_to(path).as_posix().encode("utf-8")
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            content = item.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


def _policy_dict(policy: CollectionPolicy) -> dict:
    return {
        "enabled": policy.enabled,
        "include": [{"scope": item.scope, "identity": item.identity} for item in policy.include],
        "exclude": [{"scope": item.scope, "identity": item.identity} for item in policy.exclude],
    }


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _atomic_write(path: Path, content: str, *, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_path = handle.name
            os.chmod(temporary_path, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary_path, path)
        else:
            os.link(temporary_path, path)
            os.unlink(temporary_path)
        temporary_path = None
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def _skill_action(source: Path, destination: Path) -> tuple[str, str]:
    source_digest = _tree_digest(source)
    if destination.is_symlink():
        raise ValueError("Skill destination is a symlink; preserving existing files")
    if not destination.exists():
        return "install", source_digest
    if not destination.is_dir():
        raise ValueError("Skill destination exists and is not a directory")
    marker_path = destination / SKILL_MARKER
    if not marker_path.is_file():
        if _tree_digest(destination) == source_digest:
            return "unchanged", source_digest
        raise ValueError("Skill destination is unmanaged; preserving existing files")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Skill installation marker is invalid; preserving existing files") from exc
    installed_digest = _tree_digest(destination)
    if (not isinstance(marker, dict)
            or marker.get("artifact_type") != "workflow-ledger-skill-installation"
            or marker.get("digest") != installed_digest):
        raise ValueError("Managed skill was modified; preserving existing files")
    action = "unchanged" if installed_digest == source_digest else "update"
    backup = destination.with_name(f".{destination.name}.previous")
    if action == "update" and (backup.exists() or backup.is_symlink()):
        raise ValueError("A previous skill backup exists; preserving it")
    return action, source_digest


def _install_skill(source: Path, destination: Path, digest: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}-", dir=destination.parent))
    backup: Path | None = None
    try:
        shutil.rmtree(staged)
        shutil.copytree(source, staged)
        marker = {"artifact_type": "workflow-ledger-skill-installation", "digest": digest}
        (staged / SKILL_MARKER).write_text(json.dumps(marker, indent=2) + "\n", encoding="utf-8")
        if destination.exists():
            backup = destination.with_name(f".{destination.name}.previous")
            if backup.exists() or backup.is_symlink():
                raise ValueError("A previous skill backup exists; preserving it")
            os.replace(destination, backup)
        try:
            os.replace(staged, destination)
        except OSError:
            if backup is not None:
                os.replace(backup, destination)
                backup = None
            raise
        if backup is not None:
            shutil.rmtree(backup)
    finally:
        if staged.exists():
            shutil.rmtree(staged)


def initialize(
    *,
    policy_path: str | Path,
    store_path: str | Path,
    include: tuple[Selector, ...],
    exclude: tuple[Selector, ...],
    all_records: bool,
    disabled: bool,
    skill_root: str | Path | None,
    apply: bool,
    replace_policy: bool,
) -> dict:
    """Preview or apply an explicit local policy and optional skill install."""
    policy_input = Path(policy_path).expanduser()
    store_input = Path(store_path).expanduser()
    if policy_input.is_symlink() or store_input.is_symlink():
        raise ValueError("Policy and observation store paths cannot be symlinks")
    policy_file = policy_input.resolve()
    store_file = store_input.resolve()
    if _paths_overlap(policy_file, store_file):
        raise ValueError("Policy and observation store paths must be separate")
    if policy_file.parent.exists() and not policy_file.parent.is_dir():
        raise ValueError("Policy parent exists and is not a directory")
    if store_file.exists() and not store_file.is_file():
        raise ValueError("Observation store path exists and is not a file")
    if store_file.parent.exists() and not store_file.parent.is_dir():
        raise ValueError("Observation store parent exists and is not a directory")
    if all_records and (include or disabled):
        raise ValueError("--all cannot be combined with selected or disabled collection")
    if disabled and include:
        raise ValueError("Disabled collection cannot include selectors")
    if disabled and exclude:
        raise ValueError("Disabled collection cannot use exclusions")
    if not all_records and not disabled and not include:
        raise ValueError("Choose at least one --select, --all, or --disabled")

    policy = CollectionPolicy(enabled=not disabled, include=() if all_records else include, exclude=exclude)
    expected = _policy_dict(policy)
    policy_action = "create"
    if policy_file.exists():
        if not policy_file.is_file():
            raise ValueError("Policy path exists and is not a file")
        try:
            existing = CollectionPolicy.load(policy_file)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            if not replace_policy:
                raise ValueError("Existing policy is invalid; use --replace-policy after review") from exc
            policy_action = "replace"
        else:
            if existing == policy:
                policy_action = "unchanged"
            elif replace_policy:
                policy_action = "replace"
            else:
                raise ValueError("Existing policy differs; use --replace-policy after review")
    elif replace_policy:
        raise ValueError("--replace-policy requires an existing policy")

    skill_source = _skill_source() if skill_root is not None else None
    skill_destination = (Path(skill_root).expanduser().resolve() / SKILL_NAME
                         if skill_root is not None else None)
    if skill_root is not None:
        skill_parent = Path(skill_root).expanduser().resolve()
        if skill_parent.exists() and not skill_parent.is_dir():
            raise ValueError("Skill root exists and is not a directory")
        if (_paths_overlap(policy_file, skill_destination)
                or _paths_overlap(store_file, skill_destination)):
            raise ValueError("Policy, store, and skill paths must be separate")
    skill_action = "not_requested"
    skill_digest = ""
    if skill_source is not None and skill_destination is not None:
        skill_action, skill_digest = _skill_action(skill_source, skill_destination)

    result = {
        "status": "applied" if apply else "preview",
        "policy_path": str(policy_file),
        "policy_action": policy_action,
        "store_path": str(store_file),
        "collection_enabled": policy.enabled,
        "selection_mode": "disabled" if disabled else "all" if all_records else "selected",
        "include": expected["include"],
        "exclude": expected["exclude"],
        "skill_path": str(skill_destination) if skill_destination is not None else None,
        "skill_action": skill_action,
    }
    if apply:
        if policy_action in {"create", "replace"}:
            _atomic_write(policy_file, json.dumps(expected, indent=2) + "\n",
                          replace=policy_action == "replace")
        if skill_destination is not None and skill_action in {"install", "update"}:
            _install_skill(skill_source, skill_destination, skill_digest)
    return result


def parse_selector(value: str) -> Selector:
    scope, separator, identity = value.partition("=")
    if not separator:
        raise ValueError("Selectors must use SCOPE=IDENTITY")
    return Selector(scope=scope, identity=identity)
