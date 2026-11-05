---
type: guide
title: Get started with Workflow Ledger
description: Use an AI assistant to check or install the GitHub release wheel and initialize a harness-neutral local capture setup.
---

# Get started with Workflow Ledger

Ask an AI assistant to check the Python environment you want to use, download
the wheel from the [latest GitHub Release](https://github.com/pschaubroeck/workflow-ledger/releases/latest)
if needed, then help choose and initialize local capture. Ask before installing
the downloaded wheel. Releases are distributed as downloadable files; PyPI
publishing is not configured.

## Contents

- [Requirements](#requirements)
- [Ask an AI assistant to set it up](#ask-an-ai-assistant-to-set-it-up)
- [Install the release manually](#install-the-release-manually)
- [Manual setup](#manual-setup)
- [Next steps](#next-steps)
- [Contributor source-checkout](#contributor-source-checkout)

## Requirements

- Python 3.10 or newer.
- Access to GitHub Releases to download the package wheel.
- An AI assistant that can inspect the environment and run commands after you
  authorize them. This workflow is harness-agnostic; host-specific setup, such
  as Codex hooks, is documented in the corresponding integration guide.

## Ask an AI assistant to set it up

The assistant can help with package installation and local initialization. The
following example is independent of any particular AI harness. Adapt the prompt
to your assistant and review each proposed change before authorizing it.

### AI prompt

> Help me set up Workflow Ledger. First ask which Python environment I want
> to use if that isn't clear, then check whether the package is available in
> that environment. If it is missing, download the Python wheel from the
> [latest GitHub Release](https://github.com/pschaubroeck/workflow-ledger/releases/latest)
> and offer to install that local wheel with the selected Python interpreter.
> Ask before installing. Do not change package-index settings, inspect or print
> credentials, or put secrets in commands. If no compatible wheel is attached
> to the release, stop and tell me what is missing.
>
> Use that same Python interpreter for all later commands, for example
> `python -m workflow_ledger --help`. After installing, run this command
> to verify the package before continuing. Use the package's narrow baseline,
> which selects only `skill=personal:workflow-ledger-operations` and
> `workflow=caller:observability-context`. Don't add other assets or choose
> broad capture unless I explicitly ask for `--all`. If I don't want capture
> yet, use `--disabled`.
>
> Suggest the default policy and store paths under `~/.workflow-ledger/`;
> ask whether I want custom paths. Ask whether I want the bundled
> `workflow-ledger-operations` skill installed. If I do, use a skill
> directory only when the runtime clearly identifies one; otherwise ask me for
> the location. Explain that skill discovery or a new session may be needed
> depending on the host, and that this does not configure runtime hooks.
>
> Do not change setup files yet. Show me the exact
> `python -m workflow_ledger init` preview command and its output. Ask
> before running it with `--apply`. After approval, verify the policy, store
> path, optional skill installation, and CLI capabilities. Do not configure
> host-specific hook environments.

### Example AI response

> The package isn't installed in the Python environment you selected. I can
> download the wheel from the latest GitHub Release and install it into that
> environment. May I install it?

After you approve and the installation succeeds, the assistant can continue:

> The package is available in the Python environment you selected. Its baseline
> selects only the operations skill and caller workflow context. I won't add
> other assets. You can choose `--disabled` to initialize with capture off, or
> explicitly choose `--all` for broad capture.
>
> I suggest the default store and policy paths under
> `~/.workflow-ledger/`. Do you want to keep those? Would you like the
> bundled operations skill installed? If so, I need a runtime skill directory
> unless the host identifies one clearly. I will preserve existing user-managed
> files and will not replace a different policy unless you explicitly choose
> that.
>
> After we choose the paths and optional skill location, I will run
> `python -m workflow_ledger init` in preview mode and show you the
> proposed changes. For example, with default paths and the operations skill
> installed at `/chosen/assistant/skills`, I would run:
>
> ```text
> python -m workflow_ledger init --store ~/.workflow-ledger/observations.jsonl --policy ~/.workflow-ledger/policy.json --skill-root /chosen/assistant/skills --dry-run
> ```
>
> The preview should summarize the selected identity, policy and store paths,
> and skill destination without writing files. Here is representative output
> (paths depend on your environment):
>
> ```json
> {
>   "status": "preview",
>   "policy_path": "/home/user/.workflow-ledger/policy.json",
>   "policy_action": "create",
>   "store_path": "/home/user/.workflow-ledger/observations.jsonl",
>   "collection_enabled": true,
>   "selection_mode": "selected",
>   "include": [{"scope": "skill", "identity": "personal:my-skill"}],
>   "exclude": [],
>   "skill_path": "/chosen/assistant/skills/workflow-ledger-operations",
>   "skill_action": "install"
> }
> ```
>
> I will show the actual output and ask whether you approve applying those
> changes. If you approve, I will repeat the same command with `--apply`, then
> verify the setup and report any limitation. I won't configure optional host
> hooks as part of this shared setup.

The example intentionally stops for your choices and approval. An assistant
must not infer which identities you want recorded or silently authorize
installation or setup writes. The example identity and source are placeholders;
use only an exact value established by your runtime's supported information.
Installing a skill does not enable runtime hooks. Whether the current session
can use a newly installed skill depends on the host; a reload or new session
may be needed. Follow the appropriate integration guide for runtime-specific
behavior.

## Install the release manually

Download the `.whl` file from the [latest GitHub Release](https://github.com/pschaubroeck/workflow-ledger/releases/latest),
then install it into the Python environment you intend the assistant to use:

```text
python3 -m pip install /path/to/downloaded-workflow-ledger.whl
python3 -m workflow_ledger --help
python3 -m workflow_ledger init --help
```

## Manual setup

You can run the setup command yourself. It previews by default; `--apply` writes
the narrow operations-skill and caller-workflow baseline:

```text
python -m workflow_ledger init --store ~/.workflow-ledger/observations.jsonl --policy ~/.workflow-ledger/policy.json --dry-run
```

To apply after reviewing the preview, add `--apply`. To install the bundled
operations skill as well, add `--skill-root PATH` using your runtime's skill
directory. If you initialized with `--disabled` and later choose an exact
identity, preview the new selection with `--replace-policy`; this explicitly
replaces the disabled policy after you review it. See
[Skills and agents](skills-and-agents.md) for selection modes, policy
replacement, and skill preservation behavior.

## Next steps

- Follow [Skills and agents](skills-and-agents.md) for `init` options and
  caller-declared markers.
- Use the [contributor source-checkout instructions](#contributor-source-checkout)
  only when developing from this repository.
- Read the [measurement model](../concepts/measurement-model.md) for evidence,
  timing, attribution, and unknown values.
- Review [runtime integrations](../README.md#integrations) for optional host
  capture.

## Contributor source-checkout

The following development path installs from a repository checkout. It is not
the user release install path:

```text
python3 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python examples/quickstart.py --store /tmp/observations.jsonl
.venv/bin/workflow-ledger report --store /tmp/observations.jsonl --format text
```

The example records `workflow`, `agent`, and `tool` spans through the Python
API. It makes no model or external tool calls. For application instrumentation,
wrap existing work in [`Recorder` spans](../reference/cli-and-storage.md) and
attach only measurements you have evidence for.
