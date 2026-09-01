# Architecture

The control plane has six small responsibilities:

* `WorkflowConfig` loads the project-owned YAML policy and computes its SHA-256 digest.
* `StateMachine` is a deterministic, side-effect-free transition layer.
* `AgentRunner` isolates invocation. `MockRunner` is a formal test path; `CodexCliRunner` is the live CLI adapter. `CodexAppServerRunner` is an intentional future extension seam.
* `Outcome` is the only Agent result consumed by the state machine. Human reports and stdout are evidence, never control input.
* `StateStore` atomically replaces `state.json`; per-run artifacts and `events.jsonl` are durable recovery evidence.
* `GitAudit` classifies changes in relation to workflow-declared outputs and frozen authorities.

Recovery reads only `state.json`, run artifacts, the current workflow, Git, and project traceability artifacts. It never relies on previous Codex conversation memory, hidden context, or model memory. A valid outcome left behind before a state transition is reconciled idempotently. An unfinished invocation with unknown side effects enters `HARD_STOP` with `RECOVERY_UNCERTAIN`.
