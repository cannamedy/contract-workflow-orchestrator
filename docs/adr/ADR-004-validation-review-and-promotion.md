# ADR-004: Deterministic Validation、Independent Review 与 Promotion

**Status:** Implemented

## Context

Agent 可以给出语义上合理的结果，但不能单独证明文件结构、引用、hash、依赖闭包或候选物
是否真的被验证。反过来，纯 deterministic validator 也不能替代对规范含义、架构责任和兼
容性的独立语义审查。

## Decision

Artifact 采用双层检查：

```text
Project Validator
  = deterministic structure / identity / reference / traceability / hash closure

Independent Reviewer
  = semantic fidelity / architecture / compatibility / completeness challenge
```

配置了 `validator_role` 的候选必须先在 read-only RunWorkspace 中执行项目 validator，再进
入 semantic review。Validator 必须输出结构化 JSON 并与 candidate hash、exit code、workspace
mutation 和 role 一致。`FAIL` 或生成后 `ARTIFACT_MISSING` 进入 patch/recovery，不自动创建
HumanDecision；validator execution failure 单独作为基础设施失败记录。

Artifact 只有在 candidate、upstream、validator、review、Decision、drift 和依赖前置条件
均满足时，才由 deterministic CWO code 从 `APPROVED` 经 `PROMOTION_READY` 变为 `ACCEPTED`。
Agent shell 不直接复制或覆盖 accepted artifact。`HUMAN_GATE` 复用既有 HumanDecision，
`EXTERNAL` 由外部 authority 接受。

## Rationale

将“机器可确定的事实”和“需要语义判断的正确性”分开，避免模型自然语言成为 promotion
authority，也避免普通可修复缺陷被错误升级为人工审批。

## Alternatives Considered

- 只接受 Agent 的 `APPROVED`：无法防止 hash、引用和结构错误。
- 只运行 validator：无法判断规范语义是否被误读。
- 让 validator 直接修改候选：验证器不再是 read-only evidence source。
- 每个 Artifact 创建独立审批系统：会破坏现有 scoped HumanDecision 语义。

## Consequences

候选 hash 或上游 revision 变化会使旧 evidence 失效，可能需要重新 validation/review。项目
需要提供与自身 artifact 语义匹配的 validator，但不能把内容规则塞入 CWO。

## Related Artifacts / Evidence

- `src/contract_workflow/project_validator.py`
- `src/contract_workflow/artifacts.py`
- `src/contract_workflow/orchestrator.py`
- `tests/test_project_validator.py`
- `tests/test_artifacts.py`
