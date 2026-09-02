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

The default policy is conservative: gated mode, no automatic commit, push, tag, merge, release, or external side effect. `cwo doctor` is read-only. `cwo resume` continues from durable state. Live Codex integration is available only when `codex` is found on PATH; the adapter uses the locally audited `codex exec -C <project> -` contract and never parses prose verdicts.

Projects declare `.contract-workflow/workflow.yaml`. See `examples/workflow.yaml` for a generic configuration. Runtime state defaults to `~/.local/share/contract-workflow/<project-key>` and can be redirected with `CWO_STATE_DIR`; it is kept outside the target Git repository.

Every Agent run writes `runs/<run-id>/outcome.json`, `prompt.md`, separated stdout/stderr, and metadata. Only a schema-validated outcome plus deterministic checks can select the next transition. Missing, malformed, mismatched, or unknown outcomes are retryable up to the configured bound; unresolved uncertainty becomes a hard stop.

## Scoped Human Gates

`ARCHITECTURE_DECISION_REQUIRED` and unresolved `OPEN_CONTRACT_ISSUE` outcomes create durable machine-readable Decision Requests. Each request records its question, options, authority context, and directly affected work. The scheduler computes only the downstream dependency closure: directly affected work is `BLOCKED_BY_HUMAN_DECISION`, dependent work is `WAITING_DEPENDENCY`, and independent READY work continues sequentially. The workflow becomes `WAITING_FOR_HUMAN` only when pending decisions exist and no READY work remains.

Resolve a request with a declared option or explicit answer. CWO writes an ADR under the external runtime state directory and re-schedules without rerunning completed unaffected work. Exact resolved ADR matches may satisfy a future equivalent request; ambiguous or new questions remain pending.

See [docs/architecture.md](docs/architecture.md), [docs/state-machine.md](docs/state-machine.md), and [docs/safety-model.md](docs/safety-model.md).
