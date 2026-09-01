# Contract Workflow Orchestrator

Contract Workflow Orchestrator (`cwo`) is a project-neutral Python 3.11+ control plane for a single sequential workflow. It chooses which stage runs next, validates machine-readable agent outcomes, persists recovery state, audits Git scope, and stops for human decisions. A Skill determines HOW a stage is performed; the Orchestrator determines WHAT STAGE RUNS NEXT.

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
```

The default policy is conservative: gated mode, no automatic commit, push, tag, merge, release, or external side effect. `cwo doctor` is read-only. `cwo resume` continues from durable state. Live Codex integration is available only when `codex` is found on PATH; the adapter uses the locally audited `codex exec -C <project> -` contract and never parses prose verdicts.

Projects declare `.contract-workflow/workflow.yaml`. See `examples/workflow.yaml` for a generic configuration. Runtime state defaults to `~/.local/share/contract-workflow/<project-key>` and can be redirected with `CWO_STATE_DIR`; it is kept outside the target Git repository.

Every Agent run writes `runs/<run-id>/outcome.json`, `prompt.md`, separated stdout/stderr, and metadata. Only a schema-validated outcome plus deterministic checks can select the next transition. Missing, malformed, mismatched, or unknown outcomes are retryable up to the configured bound; unresolved uncertainty becomes a hard stop.

See [docs/architecture.md](docs/architecture.md), [docs/state-machine.md](docs/state-machine.md), and [docs/safety-model.md](docs/safety-model.md).
