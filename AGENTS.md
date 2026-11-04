# Workflow Ledger

Keep the zero-dependency measurement core separate from optional adapters and
export integrations. Collection runs ordinary code; the companion skill guides
setup and interpretation. Collector/backend infrastructure belongs elsewhere.

Preserve schema migration, evidence-based ownership, unknown values, lifecycle
revisions, and explicit selection/disable semantics. Never derive skill execution
from transcript mentions or configured/loaded state. Source adapters retain safe
operational metadata and omit source content.

Use the repository's pytest suite, Ruff over `src tests benchmarks examples plugins tools`,
and Python build entry point for requested verification. Install optional extras
only when that integration is in scope. Provider calls and external backend
validation require an explicitly supplied environment; synthetic fixtures do not
prove live compatibility. Keep runtime JSONL stores out of Git.
