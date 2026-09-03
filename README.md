# Contract Workflow Orchestrator

Contract Workflow Orchestrator (`cwo`) is a project-neutral Python 3.11+ control plane for a bounded dependency graph. It chooses which ready work item runs next, validates machine-readable agent outcomes, persists recovery state and human decisions, audits Git scope, and stops only when no runnable work remains. A Skill determines HOW a stage is performed; the Orchestrator determines WHAT STAGE RUNS NEXT.

The authority hierarchy is:

```text
Contract > Plan > Task > Code
```

The Orchestrator does not decide architecture, interpret technical Contract semantics, replace a Planner/Coding Agent/Reviewer, or silently modify frozen sources. Skills are reread by each bounded Agent invocation and are not copied into prompts.

## Quick start

```bash
python3 --version
python3 -m pip install -e .
cwo init ./my-project
cwo doctor ./my-project
cwo run ./my-project --dry-run
cwo run ./my-project
cwo approve ./my-project
cwo resume ./my-project
cwo decisions ./my-project
cwo decision show ./my-project <decision-id>
cwo decide ./my-project <decision-id> --option <option>
```

The default policy is conservative: gated mode, no automatic Git commit, push, tag, merge, release, or external side effect. `cwo doctor` is read-only. `cwo resume` continues from durable state. Live Codex integration is available only when `codex` is found on PATH; each invocation runs from an external disposable Run Workspace, and the adapter binds any configured `-C` directory to that workspace rather than the authoritative project.

Projects declare `.contract-workflow/workflow.yaml`. See `examples/workflow.yaml` for a generic configuration. Runtime state defaults to `~/.local/share/contract-workflow/<project-key>` and can be redirected with `CWO_STATE_DIR`; it is kept outside the target Git repository.

## Submitted authority checks

The local project checkout is a Human Draft Workspace. For a declared `HUMAN_GUIDE`, authority checks read `origin/main` (or `authority.remote` / `authority.branch`) through an external bare cache; local Human Guide edits do not enqueue a Change Record. Use `cwo authority check <project> --dry-run` for a one-shot check or `cwo authority watch <project> --interval 300` to poll the same check. Remote snapshots, commit/blob provenance, and remote state are kept under the external runtime directory.

## Authority Change Intake

When a declared authority file changes between bounded Agent runs, CWO records a runtime-only Change Record under `authority/changes/` and runs `AUTHORITY_CHANGE_ANALYSIS`. The ledger separates the workflow digest from accepted authority revisions, so normal Human Guide evolution does not require editing `workflow.yaml`. The Agent classifies C0-C4 and declares direct impact; CWO validates the declaration and computes downstream dependency closure. Directly affected work is `BLOCKED_BY_AUTHORITY_CHANGE`, its descendants wait for dependencies, and independent READY work continues. C0/C1 changes with no semantic or task impact are auto-accepted. Machine-resolvable C2-C4 changes continue through candidate Contract/Plan revision and independent review, a complete `plan_graph` projection, and affected-task rebase analysis. Candidates remain outside accepted project authority until one existing scoped HumanDecision promotes the reviewed baseline.

The Plan Graph is a runtime projection of the human-readable Implementation Plan, not a second authority. It is validated as a DAG with traceable requirements, anchors, paths, and outputs; graph reconciliation preserves completed unaffected work, marks superseded work terminal, and adds only new or affected work to scheduling. A project workflow may bootstrap with a bounded task declaration; after propagation, the active graph supplies the complete task set without editing the workflow YAML.

Every Agent run writes `runs/<run-id>/outcome.json`, `prompt.md`, separated stdout/stderr, and metadata. Only a schema-validated outcome plus deterministic checks can select the next transition. Missing, malformed, mismatched, or unknown outcomes are retryable up to the configured bound; unresolved uncertainty becomes a hard stop.

## Agent Run Isolation

Each run snapshots the current working-tree view, including tracked modifications, deletions, and relevant untracked files, into `workspaces/<run-id>/project` under external runtime state. Strict stages discard workspace changes. Candidate stages externalize only validated candidate artifacts. Task execution stages can apply only a validated diff inside the task scope, after confirming the authoritative origin has not drifted. Authority files remain protected even if a task declares a broad path pattern. Promotion is performed by deterministic CWO code after its hash and decision checks.

## Scoped Human Gates

`ARCHITECTURE_DECISION_REQUIRED` and unresolved `OPEN_CONTRACT_ISSUE` outcomes create durable machine-readable Decision Requests. Each request records its question, options, authority context, and directly affected work. The scheduler computes only the downstream dependency closure: directly affected work is `BLOCKED_BY_HUMAN_DECISION`, dependent work is `WAITING_DEPENDENCY`, and independent READY work continues sequentially. The workflow becomes `WAITING_FOR_HUMAN` only when pending decisions exist and no READY work remains.

Resolve a request with a declared option or explicit answer. CWO writes an ADR under the external runtime state directory and re-schedules without rerunning completed unaffected work. Exact resolved ADR matches may satisfy a future equivalent request; ambiguous or new questions remain pending.

See [docs/architecture.md](docs/architecture.md), [docs/state-machine.md](docs/state-machine.md), and [docs/safety-model.md](docs/safety-model.md).
