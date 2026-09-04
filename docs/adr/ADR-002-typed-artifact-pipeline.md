# ADR-002: Typed Artifact Pipeline 作为可配置传播图

**Status:** Implemented

## Context

只用固定的 Contract/Plan stage 无法表达 Machine Contract、Conformance Specification 和
Implementation Design 的责任边界；把这些内容硬编码成大量 PAIS-specific stage 又会让 CWO
承担 Skill 的 HOW，并使不同项目无法复用。

## Decision

CWO 提供通用 `EngineeringArtifact`、Artifact lifecycle、依赖 DAG、candidate/accepted 分
离、generic generation/validation/review/patch stage，以及 `AUTO`、`HUMAN_GATE`、`EXTERNAL`
promotion policy。项目通过 `artifact_pipeline` 配置声明实际 artifact graph、Skill role、
validator role、依赖和路径。

当前支持的 kind 是：

```text
HUMAN_GUIDE
ENGINEERING_SPEC
MACHINE_CONTRACT
CONFORMANCE_SPEC
IMPLEMENTATION_DESIGN
IMPLEMENTATION_PLAN
PLAN_GRAPH
TASK_CONTRACT
```

CWO 只决定 WHAT artifact 应存在、何时可运行和哪些依赖已满足；Skill 决定内容如何产生和
如何做语义审查。没有 typed 配置的旧 workflow 使用兼容 adapter。

## Rationale

将 artifact identity、dependency、lifecycle 和内容方法分离，既能加入新的标准层，又不会
把某个项目的规范写作规则复制到 CWO。Plan Graph 是 Plan 的可重建 projection，不是第二个
Human Authority。

## Alternatives Considered

- 为每个 artifact kind 增加固定 stage：会造成 stage explosion 和 project coupling。
- 继续用 `required_propagation` 文本 token：无法确定性计算闭包、hash 和 stale derivation。
- 让每个 Skill 自己调度下游：会形成多个不一致的 workflow 控制平面。

## Consequences

项目必须为启用的 artifact 提供合适 Skill、validator 和内容约定。CWO 可安全保存缺失或候
选状态，但不能凭空生成项目语义。

## Related Artifacts / Evidence

- `src/contract_workflow/models.py`
- `src/contract_workflow/artifacts.py`
- `src/contract_workflow/scheduler.py`
- `schemas/workflow.schema.json`
- `tests/test_artifacts.py`
- `tests/test_typed_authority_propagation.py`
