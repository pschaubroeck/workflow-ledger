---
name: workflow-ledger-operations
description: Record selected skill or agent execution using shared markers, configure optional Codex lifecycle capture, and inspect or explicitly export local workflow observations.
---

# Workflow Ledger Operations

Use this skill for setup, capability discovery, selective application attachment,
local report queries, importing a specifically named Codex artifact, or operator-
requested OTLP export in the `workflow-ledger` project.

## Shared execution markers

When agent instructions or the user select a skill/agent for observation, use the
shared marker command around its actual execution. Resolve the chosen interpreter,
store, policy, and session from existing task configuration; ask only for missing
required values. A session label is a caller-chosen correlation key, hashed before
retention; it must be reused across participating calls. Do not obtain it by
scanning transcripts. Hooks use the runtime session identifier; use that identifier
only when the host supplies it, otherwise keep the marker session separate.

```text
python -m workflow_ledger.sessions --store /chosen/telemetry.jsonl --policy /chosen/policy.json start --session SESSION --invocation INVOCATION --kind skill --identity personal:my-skill
python -m workflow_ledger.sessions --store /chosen/telemetry.jsonl --policy /chosen/policy.json finish --handle TRACE_ID:SPAN_ID --outcome ok
```

The CLI returns `timestamp_ns` and the opaque trace/span handle on each marker
attempt, including failed writes. It classifies storage failures without exposing
paths or exception text. Use a stable explicit `--session` and `--invocation` for
the first start. To repair a failed start later, retry with its returned handle
and timestamp:

```text
python -m workflow_ledger.sessions --store /chosen/telemetry.jsonl --policy /chosen/policy.json start --retry-handle TRACE_ID:SPAN_ID --timestamp-ns ORIGINAL_TIMESTAMP --kind skill --identity personal:my-skill
```

To repair a failed finish, repeat it with the same handle, known outcome, and
returned timestamp. Do not rerun the observed skill just to repair its marker.

The shared marker CLI also accepts `--kind workflow` for a caller-declared
context parent. For the personal global opt-in rule, the policy must select both
the exact asset and `workflow:caller:observability-context`. Start that context
immediately before the first selected invocation, pass its handle with the
child's `--parent`, and finish the context only when the enclosing task ends.
Reuse that parent within one task while giving each child a distinct invocation
key. Its duration begins at the selected invocation; it is not whole-task time.
Never fabricate host-provided identities or drop a missing/filtered parent to
bypass selection.

Retain the opaque trace/span handle returned by start and finish that same handle.
Use a stable `--invocation` key for retries of the same start; a new execution needs
a new key. Pass `--parent TRACE_ID:SPAN_ID` only for an explicitly known enclosing
invocation in the same session. Never use a session-global "current skill" across
concurrent agents. Supported finish outcomes are `ok`, `error`, `cancelled`, and
`unknown`. Only finish when execution is known to have ended; interrupted or
unobserved completion stays open. A filtered/disabled marker must never be
described as recorded. If a marker fails with `permission_denied`, pause before
starting the selected work or before declaring capture complete. Request host
filesystem write permission for only the configured store directory and wait
for the user's decision. If approved, retry the same operation with its original
timestamp and handle. If permission is denied, or the failure is `storage_full`,
`storage_unavailable`, or `lock_timeout`, explain the capture gap and ask whether
to stop or continue without observation. Do not silently proceed. If the user
chooses after-the-fact repair, repeat the original operation with the returned
timestamp/handle after the storage issue is corrected; do not rerun the work.

If an explicit parent is missing/filtered, retain the capture gap; never drop the
parent link to bypass selection. Resolve stable invocation keys within each asset
and task so two distinct executions cannot collide.

Markers are caller-declared boundaries. Their elapsed windows can include idle
time and marker overhead; they do not measure hidden model calls or token usage.
Hooks independently observe supported native session/tool/subagent events, with
no inferred skill ownership. Recording commands also run through host tool hooks;
do not interpret their tool spans as product work or extra skill invocations.
Use `capabilities codex-hooks` to inspect coverage. No export is automatic.

## Operate the installed package

Use the Python interpreter the user chose to install the package, for example:

```text
python -m workflow_ledger capabilities explicit
python -m workflow_ledger capabilities codex
python -m workflow_ledger capabilities agents
python -m workflow_ledger report --store /path/the-user-selected/telemetry.jsonl
python -m workflow_ledger workflow TRACE_ID --store /path/the-user-selected/telemetry.jsonl
```

Do not assume an editable installation, a virtual environment location, or a
telemetry-store path. If the module is unavailable, report that the package is
not installed for the chosen interpreter and point to the repository's setup
instructions; do not install dependencies without the user's direction.

## Initialize local setup

When the user asks to install or initialize Workflow Ledger, resolve the
Python interpreter, local store path, policy path, and target harness skill
directory if they want this skill installed. Ask only for choices that are
missing. The baseline initializer enables only
`skill=personal:workflow-ledger-operations` and
`workflow=caller:observability-context`. Never choose `--all` unless the user
explicitly wants broad collection. If no capture should start yet, use
`--disabled`.

Show the planned selection, paths, policy action, and skill action before
writing. Preview with the installed command:

```text
workflow-ledger init --store /chosen/observations.jsonl --policy /chosen/policy.json --skill-root /chosen/harness/skills --dry-run
```

The command defaults to a preview. Apply only after the user authorizes the
displayed changes, repeating the same arguments with `--apply`. Existing
policies are preserved; use `--replace-policy` only after showing the current
and proposed selections. Existing unmanaged or locally modified skill
directories are preserved and reported as conflicts. A successful setup creates
the policy and installs the companion skill; it does not enable native hooks,
make provider calls, or export observations.

For a runtime with no shared skill directory, omit `--skill-root`; the AI
assistant can still use the package CLI for the current task. Follow that
runtime's integration guide before installing host-specific hooks or plugins.

Collection is ordinary Python instrumentation and adapter code and makes no
provider calls. Agent-driven marker instructions can add tool calls and context
tokens to the observed task. The companion skill gives
operator guidance; it is not a passive collector and does not start collection
by itself.

## Attach selectively

First inspect the relevant capability and resolve the requested application,
agents, skills, or workflows from the task and existing context. Ask only when
the requested scope cannot be resolved. Use exact identities for selected assets
and honor an explicitly requested whole-runtime scope. Exclusions take precedence
over inclusions. When using a
policy file, place the global option before the command:

```text
python -m workflow_ledger --policy /path/the-user-selected/policy.json report --store /path/the-user-selected/telemetry.jsonl
```

Use `--disabled` before the command for an unconditional early return. It exits
before reading policy, stores, or source artifacts, importing optional adapters,
writing records, or exporting. A policy with `enabled: false` also disables the
operation. Never broaden capture to make an example work; ask which assets should
be selected when that is unclear.

For explicit Python instrumentation, follow the package API and wrap existing
application work. For example:

```python
from workflow_ledger import AssetIdentity, CollectionPolicy, JsonlStore, Recorder, Selector

skill = AssetIdentity("my-app", "lookup", version="1")
policy = CollectionPolicy(include=(Selector("skill", skill.key),))
observer = Recorder(JsonlStore("telemetry.jsonl"), harness="my-runtime", policy=policy)
with observer.span("skill", "Lookup", identity=skill):
    run_existing_lookup()
```

Do not infer skills from setup files, instructions, or transcripts. A loaded or
configured asset is not an invocation and does not own tokens. Keep versions
unknown when evidence does not establish them; multiple active versions or
owners remain ambiguous. Missing counts stay unknown, and known root totals are
not allocated to children without ownership evidence.

## Query, import, and export

Query only a store the user identifies. `report` supports optional grouping;
`workflow TRACE_ID` inspects one run, and `cost-view` emits the sanitized usage
view. Treat reports as observations with incomplete coverage, not proof of full
capture or a workflow quality score. Durations may be inferred or overlapping.

For a repeatable plain-language interpretation, use the package's text renderer
instead of interpreting JSON fields ad hoc:

```text
python -m workflow_ledger --policy /the/selected/policy.json report --store /the/selected/store.jsonl --format text
python -m workflow_ledger --policy /the/selected/policy.json report --store /the/selected/store.jsonl --group-by skill --format text
python -m workflow_ledger --policy /the/selected/policy.json workflow TRACE_ID --store /the/selected/store.jsonl --format text
```

Pass the chosen policy before the subcommand when the query should use that
selection. Omit it only when the operator intends to inspect all stored records.

Explain the rendered result in terms of observed records, outcomes, duration
quality, token availability, query filtering, and store health. Preserve its
caveats: an unavailable token count is not zero; marker duration is an operation
window, not active model time; selected records do not prove complete capture;
an `ok` outcome is recorded event status, not a review of the work product; and
duration does not measure skill quality. Do not infer
unobserved work or ownership to make the report sound more complete.

Import only a specific Codex rollout/artifact file that the user selected:

```text
python -m workflow_ledger import-codex /path/the-user-selected/rollout.jsonl --store /path/the-user-selected/telemetry.jsonl
```

Never search or scan user rollout directories. The importer is best-effort for a
narrow, unstable JSONL shape. It uses the latest cumulative usage snapshot and
does not add incremental-looking `last_token_usage` fields. Missing or decreasing
counts remain unknown or ambiguous; inferred duration can include idle time.
Imported artifacts do not establish native skill invocation, model-call timing,
session outcome, or complete capture.

Export only when the operator explicitly asks and supplies the OTLP endpoint;
use authentication only when the operator supplies the intended header source.
For example, `export-otel --endpoint https://collector.example` requires an
explicit `--store`; `--headers-env NAME` reads only that named environment
variable. Do not discover ambient credentials or infer endpoints. This sends
selected local records to the stated endpoint. If existing authorization covers
that destination, do not request redundant approval. When endpoint or auth setup
is absent, stop before export and explain what is needed.

## Privacy and limits

Use only explicitly named artifacts, policies, and stores. Preserve the scope of
existing authorization and avoid reading unrelated rollout, credential, or
application data. Keep prompts, responses, tool arguments/results, native session
IDs, and raw artifact paths out of retained telemetry and exports. Do not echo
source content in diagnostics. Do not guess ownership, skill
usage, versions, or missing counts. The package does not configure a registry,
collector/backend infrastructure, dashboards, cloud deployment, or provider
calls; its optional OTLP export sends telemetry only to an explicitly supplied
operator endpoint.
