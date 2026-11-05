---
type: reference
title: Compatibility and Verified Scope
description: Summarize implemented integrations and the environments and event paths verified for Workflow Ledger.
---

# Compatibility and Verified Scope

This page distinguishes implemented behavior from the narrower set of
environments and event paths covered by tests and smoke checks. Adapter versions refer
to source mappings and are independent of the Python package version.

| Surface | Implemented behavior | Verified scope and limits |
| --- | --- | --- |
| Python core | Python 3.10+; no runtime dependencies; explicit spans, local reporting, and line-delimited JSON storage | GitHub Actions is configured for Python 3.10–3.14. Callers must declare boundaries and usage evidence. |
| Record schema | Schema 3 with schema 1 and 2 migration | Explicit wait spans and `waits_for` links; historical records gain no inferred links; the parser rejects unknown future schemas. |
| Structured events | Caller-recorded OpenTelemetry Logs data model records in a separate local JSONL store; shared retry and terminal-failure vocabulary | The core does not emit these vocabulary events automatically. Generic caller events remain supported; event records are not included in the OTLP span/metric exporter. |
| Local store | Revisions, retention, and one writer process per store | Serialized calls within a process; recovery, archive, and thread behavior have focused tests. It's not a multi-writer database. |
| Shared markers | Cross-process caller-declared workflow, skill, agent, and wait boundaries | Local marker behavior and recovery exercised; caller-supplied identity and parent links remain the source of attribution. |
| Codex artifacts | Selected rollout import | Best-effort cumulative session usage and an inferred observation window; no native skill, tool, or agent invocation attribution. See the [Codex guide](../integrations/codex.md). |
| Codex hooks | Optional local plugin for supported native lifecycle events | Session, tool, and subagent events live-smoked with Codex CLI 0.160.0 and 0.162.0. The saved plugin setting remains disabled; hooks don't provide native skill lifecycle or token usage. See the [Codex guide](../integrations/codex.md). |
| OpenAI Agents SDK | Optional SDK lifecycle adapter and caller-declared skill wrappers | Tested with `openai-agents` 0.23.1 using local SDK activity without provider calls. See the [Agents SDK guide](../integrations/openai-agents-sdk.md). |
| OpenTelemetry | Optional OpenTelemetry Protocol snapshot exporter over HTTP | Tested with exporter 1.45.0 and an in-process receiver. Live backend compatibility requires an operator-supplied endpoint and environment. See the [OpenTelemetry guide](../integrations/opentelemetry.md). |

Missing provider usage remains unknown. Inferred windows and overlapping spans
don't prove execution or causation. Review the [measurement model](../concepts/measurement-model.md)
before interpreting reports.

## Distribution status

The `v0.3.0` package release includes wheel and source-distribution artifacts;
the installed-package smoke checks also run in GitHub Actions. PyPI publishing
is not configured. Local integration tests establish no live provider or
external backend compatibility.

These implementation notes summarize the current scope; they are not a general
compatibility guarantee. CI results and changes to verified runtime paths are
recorded in the GitHub Actions checks and release notes.
