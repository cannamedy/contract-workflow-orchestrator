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

若 JSON candidate 声明 hash-checked `candidate_files`，该列表是主 candidate 的 linked-file
projection，而不是仅供描述的旁路内容。Linked path 必须位于 configured `accepted_path` 的目录
树内；已有 target 必须由 candidate 的 accepted baseline hash 明确固定，无 baseline 的 target
只能创建，不能覆盖。Project Validator workspace 必须同时物化主文件和全部 linked files，且
validator evidence 必须固定完整 projection。

Independent Reviewer 的 RunWorkspace 也必须物化同一组 hash-checked linked projection。语义
review 若只看到主 candidate，而其余路径仍来自 real-project baseline，会产生不可采信的
missing/stale-file finding，并使 Validator 与 Reviewer 实际审查不同的 artifact。Review
materialization 不改变 patch 的可写 scope；`ARTIFACT_PATCH` 仍只允许主 candidate 文件发生变化。

Artifact 只有在 candidate、upstream、validator、review、Decision、drift 和依赖前置条件
均满足时，才由 deterministic CWO code 从 `APPROVED` 经 `PROMOTION_READY` 变为 `ACCEPTED`。
Agent shell 不直接复制或覆盖 accepted artifact。`HUMAN_GATE` 复用既有 HumanDecision，
`EXTERNAL` 由外部 authority 接受。

`AUTO` / 已批准的 `HUMAN_GATE` linked projection 由 CWO 先持久化 `PREPARED` 文件清单，再逐
文件 atomic replace，最后记录 `COMMITTED`。中断恢复只接受每个 target 仍等于记录的 before
或 new hash；任何第三种内容都视为 target drift。只有完整 projection 到达 new hashes 后，Artifact
才可进入 `ACCEPTED`。

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
Linked projection 扩大了单个 artifact 的物理 promotion 集合，因此 CWO 负责 path confinement、
baseline/creation protection、完整 validator evidence 与可恢复的 multi-file commit；candidate
或 Agent 不能绕过这些前置条件自行扩大或提交写入。

## Related Artifacts / Evidence

- `src/contract_workflow/project_validator.py`
- `src/contract_workflow/candidate_projection.py`
- `src/contract_workflow/artifacts.py`
- `src/contract_workflow/orchestrator.py`
- `tests/test_project_validator.py`
- `tests/test_artifacts.py`
