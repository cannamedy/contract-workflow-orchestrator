# State Machine

| Current stage | Outcome / event | Next stage |
|---|---|---|
| INITIALIZING | setup | READY |
| READY | setup | TASK_EXECUTION |
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

## Scoped Human Gates

Human authority is not a global stop. A pending Decision Request marks directly affected work as `BLOCKED_BY_HUMAN_DECISION`; only descendants that require those items become `WAITING_DEPENDENCY`. Independent work stays `READY` and runs sequentially. CWO enters `WAITING_FOR_HUMAN` only when pending decisions remain and no READY work exists.
