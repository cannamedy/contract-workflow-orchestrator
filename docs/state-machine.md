# State Machine

| Current stage | Outcome / event | Next stage |
|---|---|---|
| INITIALIZING | setup | READY |
| READY | setup | TASK_EXECUTION |
| any bounded-run boundary | registered external authority candidate | AUTHORITY_CHANGE_ANALYSIS |
| AUTHORITY_CHANGE_ANALYSIS | C0/C1, no semantic/task impact | accepted revision, then normal scheduling |
| AUTHORITY_CHANGE_ANALYSIS | C2-C4 machine-resolvable | scoped authority blockers and unaffected continuation |
| AUTHORITY_CHANGE_ANALYSIS | unresolved authority ambiguity | existing scoped Decision Request / `WAITING_FOR_HUMAN` when no READY work |
| CHANGE_PROPAGATION_PLANNING | APPROVED | `CONTRACT_REVISION` / `PLAN_REVISION` / `PLAN_GRAPH_BUILD` according to deterministic propagation plan |
| CONTRACT_REVISION | APPROVED | independent `CONTRACT_REVISION_REVIEW` |
| CONTRACT_REVISION_REVIEW | REQUIRES_PATCH | automatic Contract revision and re-review |
| PLAN_REVISION | APPROVED | independent `PLAN_REVISION_REVIEW` |
| PLAN_REVISION_REVIEW | REQUIRES_PATCH | automatic Plan revision and re-review |
| PLAN_GRAPH_BUILD | APPROVED | deterministic graph reconciliation, then `TASK_REBASE_ANALYSIS` or promotion request |
| TASK_REBASE_ANALYSIS | APPROVED | existing scoped promotion Decision Request |
| ARTIFACT_GENERATION | APPROVED | ARTIFACT_REVIEW when review is required, otherwise next artifact |
| ARTIFACT_REVIEW | REQUIRES_PATCH | ARTIFACT_PATCH |
| ARTIFACT_PATCH | APPROVED | ARTIFACT_REVIEW when review is required, otherwise next artifact |
| TASK_EXECUTION | APPROVED | TASK_INDEPENDENT_REVIEW |
| TASK_INDEPENDENT_REVIEW | REQUIRES_PATCH | TASK_PATCH |
| TASK_PATCH | APPROVED | TASK_INDEPENDENT_REVIEW |
| TASK_INDEPENDENT_REVIEW | PLAN_TASK_DEFECT | PLAN_DEFECT_RESOLUTION |
| PLAN_DEFECT_RESOLUTION | APPROVED | PLAN_REVISION_REVIEW |
| PLAN_REVISION_REVIEW | REQUIRES_PATCH | PLAN_DEFECT_RESOLUTION |
| PLAN_REVISION_REVIEW | APPROVED | HUMAN_PLAN_FREEZE |
| TASK_INDEPENDENT_REVIEW | APPROVED / gated | HUMAN_GROUP_APPROVAL |
| HUMAN_GROUP_APPROVAL | human approval | next task or FINAL_VERIFICATION |
| TASK_INDEPENDENT_REVIEW | APPROVED / autonomous | next task or FINAL_VERIFICATION |
| FINAL_VERIFICATION | APPROVED / COMPLETED, gated | HUMAN_FINAL_ACCEPTANCE |
| FINAL_VERIFICATION | APPROVED / COMPLETED, autonomous | COMPLETED |
| Agent stage | `ARCHITECTURE_DECISION_REQUIRED` / unresolved `OPEN_CONTRACT_ISSUE` | scoped Decision Request, then reschedule |
| Agent stage | security/destructive/frozen/unauthorized blocking verdict | HARD_STOP |
| `WAITING_FOR_HUMAN` | pending decisions and no READY work | `cwo decide` then `cwo resume` |

`HUMAN_PLAN_FREEZE`, `HUMAN_GROUP_APPROVAL`, and `HUMAN_FINAL_ACCEPTANCE` cannot be bypassed by a model outcome. `approve` only releases the exact pending gate.

An authority candidate is not accepted as the new baseline merely because its hash changed. `AUTHORITY_CHANGE_ANALYSIS` persists the candidate and its propagation requirements. Only a C0/C1 analysis with `semantic_change=false` and no affected task auto-accepts. Machine-resolvable semantic changes create external candidate Contract/Plan artifacts and a re-buildable `plan_graph`; accepted artifacts are promoted together only after independent reviews and the existing scoped HumanDecision. An active Agent's unauthorized authority mutation remains `UNAUTHORIZED_AUTHORITY_MUTATION` and enters `HARD_STOP`.

An explicitly configured typed artifact pipeline schedules only artifacts whose dependencies are accepted or approved. Optional disabled artifacts are skipped; unaffected artifacts retain their lifecycle state. Artifact candidate generation and review use generic stages carrying `current_artifact_id`, while task scheduling remains the existing scheduler path.

## Scoped Human Gates

Human authority is not a global stop. A pending Decision Request marks directly affected work as `BLOCKED_BY_HUMAN_DECISION`; only descendants that require those items become `WAITING_DEPENDENCY`. Independent work stays `READY` and runs sequentially. CWO enters `WAITING_FOR_HUMAN` only when pending decisions remain and no READY work exists.
