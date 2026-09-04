# ADR-007: Human Authority Set

## Status

Implemented in CWO 0.8.0.

## Context

把 Human Authority 永久等同于一份 Human Guide，会遗漏高层工程指令、项目级人类决策和
对外部资料的参考政策。另一方面，外部 Reference 本身不能因为被引用就成为项目规范，
而不同 Human Authority 成员发生冲突时也不能由 CWO 以未经声明的固定优先级静默解决。

## Decision

CWO 使用可配置的 `HumanAuthoritySet`。每个 `AuthorityMember` 具有稳定 id、role、path、
来源和内容身份；0.8.0 支持以下角色：

- `ARCHITECTURE_GUIDE`：目标、心智模型、范围和架构边界；
- `ENGINEERING_DIRECTIVE`：Human 明确要求采用的高层工程方向、约束或兼容性要求；
- `PROJECT_DECISION`：项目级明确取舍、例外或范围受限的 Human ADR；
- `REFERENCE_POLICY`：规定外部标准、仓库或论文如何被使用。

Engineering Specification 的权威输入是 accepted Human Authority Set，而不是只读取
Architecture Guide。没有显式集合配置的旧项目合成为单成员 Architecture Guide 集合，保持
legacy workflow 行为。

集合 identity 对成员的 id、role、path、content hash 做 canonical sorting 后计算。远端集合
与既有 remote authority snapshot、ledger、candidate revision 和 rollover 体系共用；成员
级 hash 不变时可以复用成员审查证据，但集合发生变化仍必须重新进行集合级一致性和 handoff
readiness 审查。

Reference Policy 是 Human Authority，External Reference 不是 Human Authority。Reference
用途必须明确，例如 `PREFERRED_PATTERN`；它只能作为 `informed by` 证据，不能未经 Human
Authority 形式化就直接产生 normative Requirement。

Authority 成员之间存在未解决语义冲突时，CWO 保留冲突并进入明确治理/HumanDecision 边界，
不自行选择一方覆盖另一方。HumanDecision 只批准集合基线，不批准下游 Engineering Spec、
Machine Contract、Conformance、Design、Plan 或代码。

## Rationale

集合模型区分了 Authority Role 与文档样式，能够表达真实项目的多来源 Human 约束，同时
保留单一 Human Guide 项目的兼容性。canonical aggregate identity 和 immutable member
snapshots 使 candidate lineage 可审计，避免新提交覆盖旧候选或把 Reference 误升格为规范。

## Alternatives Considered

### Keep Human Guide as the sole authority

Rejected: cannot represent independent engineering directives or reference governance without
silently overloading one document.

### Add a fixed precedence chain between all authority roles

Rejected: a fixed chain can hide a genuine conflict whose resolution is itself a Human authority
choice.

### Treat external references as authority members by URL

Rejected: external material is evidence/pattern input and must be pinned; it does not become the
project's normative source merely by being referenced.

## Consequences

Positive:

- Engineering derivation receives the complete declared Human Authority input.
- Member-level change detection, immutable snapshots and same-CR rollover are auditable.
- Existing single-guide workflows remain compatible.

Tradeoffs and limits:

- Full Authority precedence, automatic reference discovery/selection and generic Target
  Understanding remain future capabilities.
- Set-level semantic review is required even when some member hashes are unchanged.
- Project configuration must declare members and external references must be pinned for
  reproducibility.

## Related Artifacts / Evidence

- `src/contract_workflow/authority_set.py`
- `src/contract_workflow/remote.py`
- `src/contract_workflow/orchestrator.py`
- `tests/test_authority_set.py`
- `docs/CWO_架构原理与设计指南.md`
- `docs/CWO_当前架构状态.md`
