# Safety Model

Automatic actions are bounded Agent invocation, outcome validation, deterministic stage transitions, retry with exponential backoff, and runtime artifact creation. The tool never automatically commits, pushes, tags, merges, publishes, releases, or changes a frozen authority.

Human gates are group approval, plan freeze, and final acceptance. Hard stops include `OPEN_CONTRACT_ISSUE`, `ARCHITECTURE_DECISION_REQUIRED`, `PLAN_EXPANSION_REQUIRED`, security-sensitive or destructive actions, unauthorized external side effects, frozen-source mismatch, merge conflict, unrelated Git contamination that cannot be isolated, workflow digest changes, invalid outcomes after retry exhaustion, and recovery uncertainty.

A dirty working tree is not automatically a failure. Expected target artifacts and the orchestrator tracker are classified separately from frozen authority changes and unrelated changes. Frozen source integrity is checked by SHA-256 and optional Git commit/tag metadata.
