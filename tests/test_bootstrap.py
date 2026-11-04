import json

import pytest

from workflow_ledger.bootstrap import (
    SKILL_MARKER,
    SKILL_NAME,
    _install_skill,
    _skill_source,
    _tree_digest,
    initialize,
    parse_selector,
)
from workflow_ledger.policy import CollectionPolicy, Selector


def _initialize(tmp_path, **overrides):
    options = {
        "policy_path": tmp_path / "config" / "policy.json",
        "store_path": tmp_path / "data" / "observations.jsonl",
        "include": (Selector("skill", "personal:example"),),
        "exclude": (),
        "all_records": False,
        "disabled": False,
        "skill_root": None,
        "apply": False,
        "replace_policy": False,
    }
    options.update(overrides)
    return initialize(**options)


def test_preview_does_not_create_policy_store_or_skill(tmp_path):
    skill_root = tmp_path / "assistant" / "skills"
    result = _initialize(tmp_path, skill_root=skill_root)

    assert result["status"] == "preview"
    assert result["policy_action"] == "create"
    assert result["skill_action"] == "install"
    assert not (tmp_path / "config").exists()
    assert not (tmp_path / "data").exists()
    assert not skill_root.exists()


def test_apply_writes_policy_and_skill_but_not_observation_store(tmp_path):
    skill_root = tmp_path / "assistant" / "skills"
    result = _initialize(tmp_path, skill_root=skill_root, apply=True)
    policy_path = tmp_path / "config" / "policy.json"
    skill_path = skill_root / SKILL_NAME

    assert result["status"] == "applied"
    assert CollectionPolicy.load(policy_path).include == (Selector("skill", "personal:example"),)
    assert (skill_path / "SKILL.md").is_file()
    marker = json.loads((skill_path / SKILL_MARKER).read_text(encoding="utf-8"))
    assert marker["artifact_type"] == "workflow-ledger-skill-installation"
    assert marker["digest"]
    assert not (tmp_path / "data" / "observations.jsonl").exists()


@pytest.mark.parametrize(
    ("all_records", "disabled", "enabled", "selection_mode"),
    [
        (True, False, True, "all"),
        (False, True, False, "disabled"),
    ],
)
def test_apply_supports_broad_and_disabled_selection(
    tmp_path, all_records, disabled, enabled, selection_mode
):
    result = _initialize(
        tmp_path,
        all_records=all_records,
        disabled=disabled,
        include=(),
        apply=True,
    )

    assert result["selection_mode"] == selection_mode
    assert result["collection_enabled"] is enabled
    assert CollectionPolicy.load(result["policy_path"]).enabled is enabled


@pytest.mark.parametrize(
    ("all_records", "disabled", "include", "exclude"),
    [
        (True, False, (Selector("skill", "personal:example"),), ()),
        (True, True, (), ()),
        (False, True, (Selector("skill", "personal:example"),), ()),
        (False, True, (), (Selector("skill", "personal:example"),)),
    ],
)
def test_rejects_conflicting_selection_modes(tmp_path, all_records, disabled, include, exclude):
    with pytest.raises(ValueError):
        _initialize(
            tmp_path,
            all_records=all_records,
            disabled=disabled,
            include=include,
            exclude=exclude,
        )


def test_selector_requires_scope_identity_pair():
    assert parse_selector("skill=personal:example") == Selector("skill", "personal:example")
    with pytest.raises(ValueError, match="SCOPE=IDENTITY"):
        parse_selector("skill-personal:example")
    with pytest.raises(ValueError, match="Invalid selector"):
        parse_selector("unknown=personal:example")


def test_existing_equal_policy_is_unchanged(tmp_path):
    policy_path = tmp_path / "config" / "policy.json"
    policy_path.parent.mkdir()
    policy_path.write_text(
        json.dumps({
            "enabled": True,
            "include": [{"scope": "skill", "identity": "personal:example"}],
            "exclude": [],
        }),
        encoding="utf-8",
    )

    result = _initialize(tmp_path)

    assert result["policy_action"] == "unchanged"


def test_different_policy_requires_explicit_replacement(tmp_path):
    policy_path = tmp_path / "config" / "policy.json"
    policy_path.parent.mkdir()
    policy_path.write_text(json.dumps({"enabled": False}), encoding="utf-8")

    with pytest.raises(ValueError, match="--replace-policy"):
        _initialize(tmp_path, apply=True)

    _initialize(tmp_path, apply=True, replace_policy=True)
    assert CollectionPolicy.load(policy_path).include == (Selector("skill", "personal:example"),)


def test_invalid_policy_requires_explicit_replacement(tmp_path):
    policy_path = tmp_path / "config" / "policy.json"
    policy_path.parent.mkdir()
    policy_path.write_text("not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="Existing policy is invalid"):
        _initialize(tmp_path, apply=True)

    _initialize(tmp_path, apply=True, replace_policy=True)
    assert CollectionPolicy.load(policy_path).include == (Selector("skill", "personal:example"),)


def test_replace_policy_requires_existing_policy(tmp_path):
    with pytest.raises(ValueError, match="requires an existing policy"):
        _initialize(tmp_path, replace_policy=True)


def test_unmanaged_skill_is_preserved_and_policy_not_written(tmp_path):
    skill_root = tmp_path / "assistant" / "skills"
    skill_path = skill_root / SKILL_NAME
    skill_path.mkdir(parents=True)
    user_file = skill_path / "user-notes.md"
    user_file.write_text("keep this", encoding="utf-8")

    with pytest.raises(ValueError, match="unmanaged"):
        _initialize(tmp_path, skill_root=skill_root, apply=True)

    assert user_file.read_text(encoding="utf-8") == "keep this"
    assert not (tmp_path / "config" / "policy.json").exists()


def test_modified_managed_skill_is_preserved(tmp_path):
    skill_root = tmp_path / "assistant" / "skills"
    skill_path = skill_root / SKILL_NAME
    source = _skill_source()
    _install_skill(source, skill_path, _tree_digest(source))
    skill_file = skill_path / "SKILL.md"
    skill_file.write_text("locally modified", encoding="utf-8")

    with pytest.raises(ValueError, match="modified"):
        _initialize(tmp_path, skill_root=skill_root, apply=True)

    assert skill_file.read_text(encoding="utf-8") == "locally modified"
    assert not (tmp_path / "config" / "policy.json").exists()


def test_managed_old_skill_is_updated(tmp_path):
    skill_root = tmp_path / "assistant" / "skills"
    skill_path = skill_root / SKILL_NAME
    old_source = tmp_path / "old-skill"
    old_source.mkdir()
    (old_source / "SKILL.md").write_text("old version", encoding="utf-8")
    _install_skill(old_source, skill_path, _tree_digest(old_source))

    result = _initialize(tmp_path, skill_root=skill_root, apply=True)

    assert result["skill_action"] == "update"
    assert (skill_path / "SKILL.md").read_text(encoding="utf-8") == (
        _skill_source() / "SKILL.md"
    ).read_text(encoding="utf-8")
    assert not skill_path.with_name(f".{SKILL_NAME}.previous").exists()


def test_existing_backup_blocks_update_before_policy_write(tmp_path):
    skill_root = tmp_path / "assistant" / "skills"
    skill_path = skill_root / SKILL_NAME
    old_source = tmp_path / "old-skill"
    old_source.mkdir()
    (old_source / "SKILL.md").write_text("old version", encoding="utf-8")
    _install_skill(old_source, skill_path, _tree_digest(old_source))
    backup = skill_path.with_name(f".{SKILL_NAME}.previous")
    backup.write_text("preserve this", encoding="utf-8")

    with pytest.raises(ValueError, match="previous skill backup"):
        _initialize(tmp_path, skill_root=skill_root, apply=True)

    assert backup.read_text(encoding="utf-8") == "preserve this"
    assert not (tmp_path / "config" / "policy.json").exists()


def test_broken_skill_symlink_is_not_replaced(tmp_path):
    skill_root = tmp_path / "assistant" / "skills"
    skill_root.mkdir(parents=True)
    skill_path = skill_root / SKILL_NAME
    skill_path.symlink_to(tmp_path / "missing-target")

    with pytest.raises(ValueError, match="symlink"):
        _initialize(tmp_path, skill_root=skill_root, apply=True)

    assert skill_path.is_symlink()
    assert not (tmp_path / "config" / "policy.json").exists()


def test_policy_store_and_skill_paths_must_not_overlap(tmp_path):
    root = tmp_path / "shared"
    with pytest.raises(ValueError, match="must be separate"):
        _initialize(
            tmp_path,
            policy_path=root / "policy.json",
            store_path=root / "policy.json" / "child.jsonl",
        )


def test_policy_and_store_symlinks_are_rejected(tmp_path):
    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    policy_link = tmp_path / "policy-link.json"
    policy_link.symlink_to(target)

    with pytest.raises(ValueError, match="cannot be symlinks"):
        _initialize(tmp_path, policy_path=policy_link)

    store_link = tmp_path / "store-link.jsonl"
    store_link.symlink_to(tmp_path / "missing-store")
    with pytest.raises(ValueError, match="cannot be symlinks"):
        _initialize(tmp_path, store_path=store_link)
