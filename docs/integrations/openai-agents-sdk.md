---
type: guide
title: Optional OpenAI Agents SDK integration
description: Observe supported OpenAI Agents SDK lifecycle spans and add explicit caller-declared skill boundaries.
---

# Optional OpenAI Agents SDK integration

This adapter is an optional source integration for the Python core. It observes
supported lifecycle spans from OpenAI Agents SDK tracing and writes safe,
bounded records through the local `Recorder`. The core and other integrations do
not require the Agents SDK.

## Contents

- [Install the optional extra](#install-the-optional-extra)
- [Observe native SDK spans](#observe-native-sdk-spans)
- [Declare skill execution explicitly](#declare-skill-execution-explicitly)
- [Limits and diagnostics](#limits-and-diagnostics)
- [Next steps](#next-steps)

## Install the optional extra

Install the pinned SDK integration extra alongside the package:

```text
.venv/bin/python -m pip install -e '.[agents]'
```

Compatibility checks cover `openai-agents==0.23.1`; see
[compatibility](../reference/compatibility.md) before relying on a different SDK version.

## Observe native SDK spans

Install the local processor before creating traces, and only when this
application owns the global tracing setup for the SDK:

```python
from agents import Agent, RunConfig, Runner
from workflow_ledger import JsonlStore, Recorder
from workflow_ledger.adapters.openai_agents import install_local_tracing

observer = Recorder(JsonlStore("telemetry.jsonl"), harness="openai-agents")
processor = install_local_tracing(observer)

agent = Agent(name="Researcher", instructions="Application instructions")
result = Runner.run_sync(
    agent,
    "Application input",
    run_config=RunConfig(trace_include_sensitive_data=False),
)
```

Calling `install_local_tracing` replaces every globally registered processor for
the SDK, including its default remote exporter. It doesn't preserve processors
the application already registered. If your application registers another
processor or exporter, compose the intended processor list using
`OpenAIAgentsTraceProcessor`. Importing the adapter alone doesn't change SDK
tracing configuration.

The adapter maps supported workflow, agent, model, and function/tool spans. It
hashes native workflow, agent, and tool names before retaining them and doesn't
serialize SDK span payloads or error messages. It records provider-observed usage
only when supported SDK span data exposes usage values; absent fields remain
unknown. The observer doesn't make provider calls. The application run above
does make its normal SDK/provider calls.

## Declare skill execution explicitly

The SDK has no dedicated skill lifecycle span type. Wrap the actual skill call
with the recorder while binding it to the current SDK trace context:

```python
from workflow_ledger import AssetIdentity
from workflow_ledger.adapters.openai_agents import bind_current_sdk_context

lookup = AssetIdentity("my-app", "lookup", version="1")

# Inside the active SDK trace/span, around the existing skill execution:
with bind_current_sdk_context(observer, processor):
    with observer.span("skill", "Lookup", identity=lookup):
        run_lookup_skill()
```

This declares the skill boundary and preserves the current SDK parent
relationship. Supported SDK child spans can inherit the explicit association
active when they start. The adapter doesn't infer skill execution from agent
instructions, names, or loaded/configured state. Multiple active skill
identities or versions can leave usage unattributed, as defined by the
[attachment contract](../reference/attachment-contract.md).

## Limits and diagnostics

The adapter omits unsupported span types and counts them in
`processor.diagnostics`.
Callback conversion failures, missing lifecycle data, filtering, and recorder
delivery failures also have content-free diagnostic counts. Inspect these after
SDK callbacks finish or after calling `force_flush()` on the SDK trace when
appropriate. Diagnostics don't include source payloads or exception text.

This adapter observes only SDK callbacks delivered to the installed processor.
An adapter callback doesn't prove that every operation produced a trace span.
Skill boundaries require
explicit wrappers, missing usage remains unknown, and SDK trace completion does
not by itself prove workflow success. The [compatibility page](../reference/compatibility.md)
lists the verified SDK version and scope.

## Next steps

- Start with the harness-neutral [Python quickstart](../guides/getting-started.md)
  if you don't need SDK callbacks.
- See the [attachment contract](../reference/attachment-contract.md) for lifecycle and
  ownership rules.
