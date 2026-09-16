# ADR-010: Bounded Run Step Budget 与 Lifetime Telemetry 分离

**Status:** Implemented in CWO 0.8.19

## Context

PAIS 的长期 typed run 在多个日期和恢复周期后把持久化 `WorkflowState.total_steps` 累计到 100。
旧实现同时把该 lifetime counter 与 `policy.max_total_steps` 比较，因此一个新的、正常的 bounded run
在开始下一阶段前进入不可恢复 `MAX_TOTAL_STEPS`。同一 policy 已在 `run()` 本地循环中提供 step bound；
持久化比较把单次防循环预算错误地变成了 workflow lifetime ceiling。

## Decision

- `max_total_steps` 只限制一次 `Orchestrator.run()` 的 scheduler iteration 数量。
- `total_steps` 保持为跨 invocation 的持久化 telemetry，不在 `step()` 入口参与阻断。
- 单次 invocation 确实耗尽预算且状态仍为 `RUNNING` 时，CWO 写入 canonical、可恢复的
  `MAX_TOTAL_STEPS` hard stop，并记录当时的 `blocked_stage`。
- `recover` 仅在没有 active run、存在明确 `blocked_stage` 时恢复该 stop；不清零或伪造
  `total_steps`，也不采用任何 workspace。

## Consequences

长生命周期 workflow 可以跨多次 `run` / `recover` 继续，同时每次 invocation 仍有确定的 loop
containment。若某个逻辑循环持续存在，它会在每个 bounded invocation 再次触发同一可审计 stop，
而不会被 lifetime counter 的历史值混淆。

## Related Artifacts / Evidence

- `src/contract_workflow/orchestrator.py`
- `tests/test_cwo.py`
- `docs/architecture.md`
