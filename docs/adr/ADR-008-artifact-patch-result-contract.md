# ADR-008: Artifact Patch 语义结果、Candidate 变化与恢复边界

**Status:** Implemented in CWO 0.8.17

## Context

通用 `ARTIFACT_PATCH` 曾只消费 `APPROVED` 与 candidate hash/content。真实 PAIS Machine Contract
运行证明这不足以区分四种情况：Agent 确实修改了 candidate、Agent 判断无需内容修改、修补被上游
Authority 阻塞，以及 Agent 没有返回合法语义结果。仅凭 candidate 文件存在、hash 声明或 workspace
是否变化，既不能证明修补成功，也不能安全决定下一阶段。

同一次运行还证明：在 `danger-full-access` 降级环境中，失败 Agent 可能越过 RunWorkspace 直接改写
external candidate store。即使 state 没有采用该结果，candidate bytes 也可能不再匹配已记录 hash。

## Decision

`ARTIFACT_PATCH` 必须返回 `artifact.patch_result`，并使用以下互斥状态：

- `PATCH_APPLIED`：top-level verdict 为 `APPROVED`，且 candidate hash 必须相对当前记录发生变化；
- `NO_PATCH_NEEDED`：candidate 不得变化，必须提供非空 reasoning，并按当前 patch finding 完整提供
  `finding_reconciliation`；CWO 将 artifact 返回独立 `ARTIFACT_REVIEW`，不得直接 promotion；
- `PATCH_BLOCKED`：candidate 不得变化，必须声明上游依赖或 Human Authority blocker，并使用已有
  `OPEN_CONTRACT_ISSUE` / `ARCHITECTURE_DECISION_REQUIRED` scoped Decision 机制；
- `EXECUTION_FAILED`：不是 Agent 可声明的语义成功。缺失、malformed 或自相矛盾的 patch result
  由 CWO runtime 在既有 `execution_failure` evidence 中规范化记录。

任何成功 `PATCH_APPLIED` 仍须重新 deterministic validation，再进入 independent semantic review；
只有 deterministic CWO promotion logic 可以接受 artifact。

对于已证明的旧版 PAIS stop，recovery 可以在严格满足以下条件时恢复最后 adopted candidate：失败
invocation 已完成且 workspace 无 mutation、real drift 无 authority/upstream/target blocker、下游尚未
promotion，并且某个更早的成功 CWO outcome 同时匹配 artifact id、candidate hash 和 state 中记录的
last-outcome summary。恢复只复制该已采用 outcome 的 canonical content；永不采用失败 workspace 或
失败 invocation 声称的新 candidate。

## Rationale

这使“语义结论”和“候选内容变化”成为两份必须相互一致的证据，并保持既有 Outcome、
HumanDecision、Validator、Review 和 Promotion 框架为唯一控制面。受限恢复依赖 durable CWO
evidence，而不是聊天记忆、partial stdout 或人工重建。

## Consequences

- 旧 Agent prompt 必须更新为新的 patch result contract；没有显式结果的 patch 会进入 bounded retry。
- `NO_PATCH_NEEDED` 增加一次独立审查，但不会把 patch Agent 变成 promotion authority。
- external store 仍不是 kernel-level sandbox；因此 prompt scope、hash 检查和 evidence-bound recovery
  都必须保留。

## Related Artifacts / Evidence

- `src/contract_workflow/artifacts.py`
- `src/contract_workflow/orchestrator.py`
- `src/contract_workflow/prompt_builder.py`
- `tests/test_artifact_patch_results.py`
- `docs/state-machine.md`
