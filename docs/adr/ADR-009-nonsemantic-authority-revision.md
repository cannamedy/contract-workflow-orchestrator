# ADR-009: Non-semantic Authority Revision 的 Provenance-only 接受

**Status:** Implemented in CWO 0.8.18

## Context

PAIS 的真实 typed run 在 Machine Contract patch 后观察到新的远端 Human Authority Set revision。
独立分析把它分类为 C1、`semantic_change=false`，且 task 与 artifact 影响集均为空。旧实现虽然先把
revision 标为 accepted，却仍启动一个空 typed propagation。Scheduler 随后按 Authority content hash
变化把已接受 Engineering Specification 误判为 stale，并把 `EXTERNAL` Human Guide 排入 generation。

## Decision

C0/C1 revision 只有在无 semantic、task 和 artifact 影响时才可自动接受。接受时 CWO 必须：

- 校验 immutable candidate snapshot；Authority Set 还必须校验 manifest aggregate 与每个 member snapshot；
- 完整更新 external accepted ledger，不写入项目中的 local Human Draft；
- 保留所有 downstream artifact 内容与 lifecycle 状态，只把 Human Guide 的直接依赖 provenance 从
  已分析的 base hash rebase 到新 accepted hash；
- 不创建 empty typed propagation；
- 若 Authority analysis 打断了 typed candidate，则按其 durable status 恢复
  `ARTIFACT_VALIDATION`、`ARTIFACT_REVIEW` 或 `ARTIFACT_PATCH`；
- 对旧版已持久化的 empty propagation，只允许从 CWO artifact records 与匹配的 candidate store 恢复，
  并明确记录没有采用失败 workspace 或新 candidate。

## Consequences

非语义 Authority 修订仍具有新的 accepted provenance identity，但不会迫使内容相同且已审查的下游
规范重新生成。任何非空 task/artifact impact、语义变化、snapshot 不匹配或多个中断 candidate 都会
拒绝这条快捷路径，继续使用正常传播或明确恢复失败。

## Related Artifacts / Evidence

- `src/contract_workflow/orchestrator.py`
- `tests/test_typed_authority_propagation.py`
- `docs/state-machine.md`
