---
type: Decision Record
title: Workflow Ledger rebranding
description: Record the accepted Workflow Ledger name and how the public reference project applies it.
---

# Workflow Ledger rebranding

## Decision

On 2026-11-04, the product name **Workflow Ledger** was accepted. This public
reference project uses `workflow-ledger` as its distribution and command name.

The name connects linked operational records to an organized, durable ledger.
The user considers descriptive “workflow ledger” usage and the separate
Agentic Ledger product insufficient to create meaningful confusion in this
product's space. The name also moves the product away from the crowded
telemetry and observability brand category.

## Product description

Workflow Ledger is a harness-neutral, zero-dependency Python library for
recording linked workflow, agent, skill, model-call, tool, and explicit-wait
operations as local operational metadata. Optional adapters attach supported
runtime events. OpenTelemetry export remains explicit. The product does not
inspect transcripts, infer execution from configuration or mentions, or
operate collectors, dashboards, or telemetry backends.

## Public project status

The GitHub reference distribution applies the new name to the package, Python
module, commands, default config directory, bundled skill, plugin, and public
documentation. The repository contains product documentation, maintainer
guidance, and GitHub Actions workflows for the personal public project.

The OpenTelemetry event attribute namespace uses `workflow_ledger.*` in new
records. Readers retain compatibility with the former `agent_observability.*`
namespace and former instrumentation scope for previously recorded event data.
Other collection semantics, explicit selection, source ownership, and unknown
values are unchanged.
