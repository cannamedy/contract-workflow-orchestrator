# ADR-005: Legacy Compatibility 与 Typed Workflow 边界

**Status:** Implemented

## Context

CWO 在 typed pipeline 引入前已经有基于 Contract、Plan、Plan Graph 和 Task 的 workflow。已有
项目不能因为升级 CWO 而被迫一次性迁移；但启用 typed pipeline 后，如果仍允许旧的 propagation
token 或 legacy Plan 在 typed upstream 缺失时悄悄驱动任务，就会再次产生 typed bypass。

## Decision

- 没有 `artifact_pipeline` 的旧 workflow 继续走 legacy Contract/Plan compatibility adapter。
- 明确配置 `artifact_pipeline` 的 workflow，Artifact Graph 是唯一 canonical propagation graph。
- Analyzer 只声明 direct artifact impact；CWO 计算 artifact dependency closure 和执行顺序。
- typed workflow 在 `PLAN_GRAPH_BUILD` 前必须拥有并 pin 所需 accepted typed upstream；不得 fallback
  到 legacy Plan/Graph。
- 旧 propagation derivatives 可被标记为 superseded 并作为 forensic evidence 保留，但不能
  再被当作当前 typed authority。

## Rationale

兼容性需要保留旧项目的行为，安全性又需要禁止新项目在声明 typed graph 后走另一条隐蔽路径。
这两个条件通过“配置决定 adapter 边界”同时满足，而不是通过运行时猜测。

## Alternatives Considered

- 所有项目强制 typed migration：破坏已有工作流。
- 所有项目永远允许 legacy fallback：会使 typed prerequisites 失去意义。
- 按 stage 名称或自然语言 token 混合决定路径：不可审计且易产生 bypass。

## Consequences

typed 项目第一次运行可能显示多个 artifact `MISSING` 或 `BLOCKED`，这是等待合法传播而非
启动故障。项目迁移需要保留 legacy evidence，并在新 Plan/Task candidate 中建立完整 traceability。

## Related Artifacts / Evidence

- `src/contract_workflow/orchestrator.py`
- `src/contract_workflow/propagation.py`
- `src/contract_workflow/plan_graph.py`
- `tests/test_typed_authority_propagation.py`
