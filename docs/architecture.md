# Architecture

The control plane has seven small responsibilities:

* `WorkflowConfig` loads the project-owned YAML policy and computes its SHA-256 digest.
* `StateMachine` is a deterministic, side-effect-free transition layer.
* `AgentRunner` isolates invocation. `MockRunner` is a formal test path; `CodexCliRunner` is the live CLI adapter. `CodexAppServerRunner` is an intentional future extension seam.
* `Outcome` is the only Agent result consumed by the state machine. Human reports and stdout are evidence, never control input.
* `StateStore` atomically replaces `state.json`; per-run artifacts, `events.jsonl`, `decisions/*.json`, and `adrs/*.json` are durable recovery evidence.
* `GitAudit` classifies changes in relation to workflow-declared outputs and frozen authorities.
* The dependency-aware scheduler tracks per-task work-item status, computes the minimum necessary blocking closure, persists scoped Human Decisions and ADRs, and selects READY work sequentially.
* Authority Change Intake compares an accepted revision with an externally observed candidate. Its runtime-only ledger is stored under `authority/ledger.json` with durable records under `authority/changes/`; workflow YAML and its digest remain unchanged across normal authority evolution.

An authority mismatch between bounded Agent runs is registered as `AUTHORITY_CHANGE_DETECTED` and sent to the non-coding `AUTHORITY_CHANGE_ANALYSIS` stage. The Agent supplies semantic classification and direct task impact; CWO validates hashes, requirement IDs, anchors, task IDs, and computes dependency closure deterministically. C0/C1 non-semantic changes can be auto-accepted. C2-C4 changes remain `CHANGE_PENDING` or `PROPAGATING`, block direct work with `BLOCKED_BY_AUTHORITY_CHANGE`, and leave independent READY work schedulable. The stage is an extension seam for future Change Propagation Work Items; v0.3.0 does not rewrite Contract or Plan files.

Recovery reads only `state.json`, run artifacts, the current workflow, Git, and project traceability artifacts. It never relies on previous Codex conversation memory, hidden context, or model memory. A valid outcome left behind before a state transition is reconciled idempotently. An unfinished invocation with unknown side effects enters `HARD_STOP` with `RECOVERY_UNCERTAIN`.
