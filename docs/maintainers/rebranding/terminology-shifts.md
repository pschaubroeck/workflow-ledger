---
type: Terminology Map
title: Workflow Ledger terminology shifts
description: Map former Agent Observability terminology to the Workflow Ledger public project and record compatibility behavior.
---

# Workflow Ledger terminology shifts

This page records how the public reference project applies the accepted name.

## Product language

| Former term | Public term | Treatment |
| --- | --- | --- |
| Agent Observability | Workflow Ledger | Use as the product name. Use the former name only when explaining historical data or migration. |
| Agent Observability core/library | Workflow Ledger core/library | Describe the harness-neutral, zero-dependency local recording library. |
| Agent Observability documentation | Workflow Ledger documentation | Public user and maintainer documentation uses the new name. |
| Local observations | Workflow Ledger records | “Observation” remains valid when referring to an individual measured fact or established schema concept. |
| OpenTelemetry export | OpenTelemetry export | Capability name stays the same; export remains optional and explicitly initiated. |

## Public identifiers

| Former identifier | Workflow Ledger identifier | Compatibility behavior |
| --- | --- | --- |
| Distribution `agent-observability` | `workflow-ledger` | The public package uses the new distribution name. |
| Python module `agent_observability` | `workflow_ledger` | The public import namespace is renamed. |
| CLI `agent-observability` | `workflow-ledger` | The public command uses the new name. |
| CLI `agent-observability-marker` | `workflow-ledger-marker` | The public marker command uses the new name. |
| Config directory `.agent-observability/` | `.workflow-ledger/` | New initialization uses the new directory. Existing user files are not silently moved. |
| Skill `agent-observability-operations` | `workflow-ledger-operations` | The bundled skill source and installation instructions use the new name. |
| Codex plugin identifiers containing `agent-observability` | Corresponding `workflow-ledger` identifiers | The public plugin and packaging helper use the new identifiers. |
| Codex hook environment variables with the `AWO_` prefix | `WORKFLOW_LEDGER_` prefix | The public plugin and hook instructions use the descriptive prefix. |
| OTel service and instrumentation scope `agent-observability` | `workflow-ledger` | New exports use the Workflow Ledger name. Readers accept the former scope. |
| OTel event attributes `agent_observability.*` | `workflow_ledger.*` | New event records use the new prefix; readers accept the former prefix. |

## Data and behavior that remain stable

- Local workflow, agent, skill, model-call, tool, and wait record semantics are
  unchanged.
- Evidence-based ownership, unknown values, lifecycle revisions, and explicit
  selection and disable behavior remain part of the data contract.
- Readers accept the prior event namespace and instrumentation scope so prior
  Agent Observability event records remain readable.
- The former name may appear in historical data and compatibility code. It
  should not appear in current product instructions except where needed to
  explain that compatibility.
