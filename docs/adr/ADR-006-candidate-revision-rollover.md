# ADR-006: 未接受 Authority Candidate 的 revision rollover

**Status:** Implemented

## Context

Human 可能在旧候选尚未完成 review 或 approval 时提交修订版。若把新内容覆盖旧字段，会丢
失旧候选为何被拒绝、哪个 Decision 绑定了旧 hash 以及分析使用了哪一个 snapshot 的证据。
若每次修订都强制创建并行 Change，又会让同一条未接受意图出现多个 propagation。

## Decision

对同一 authority source、path 和未改变的 accepted base：

- 新 remote content 与当前未接受 candidate 不同，且存在完整 immutable snapshot 时，可以在同
  一 CR 内 rollover；
- 旧 revision 标记 `SUPERSEDED`，原因记录为新的 Human submission，旧 snapshot、analysis、
  review 和 Decision 完整保留；
- 新 revision 成为 `ACTIVE`，获得新的 revision identity 和 candidate-bound HumanDecision；
- 新 revision 必须重新执行 Authority Change Analysis，比较基准仍是 accepted base，而不是
  从未 accepted 的旧 candidate；
- rollover 不要求额外的“确认 rollover” Decision；remote submission 本身已表达 Human intent；
- 如果旧 candidate 已经 accepted，新的 remote revision 不得 rollover，而应形成新的 CR 或排
  队处理，不能中断 accepted authority 下游传播；
- 重复检查必须幂等，网络失败不能回退已完成 rollover。

## Rationale

保留 lineage 能回答“有哪些候选、为什么旧候选失效、当前审查的是哪一版”；同 CR rollover
又避免未接受草稿形成并行传播。Accepted boundary 是不可跨越的地基保护。

## Alternatives Considered

- 直接覆盖 `candidate_hash`：丢失 forensic lineage。
- 每个 remote commit 新建 CR：产生并行 propagation，且把同一未接受候选拆散。
- 继续使用旧 Decision：可能把旧 hash 的批准误当成新 revision 的批准。

## Consequences

Decision identity 必须绑定 candidate hash。旧 Decision 只能可审计地 `SUPERSEDED`，不能修改
原问题来伪装为新候选的审批。下游 artifact 在新 Human Authority 未批准前继续阻塞。

## Related Artifacts / Evidence

- `src/contract_workflow/remote.py`
- `src/contract_workflow/orchestrator.py`
- `src/contract_workflow/artifacts.py`
- `tests/test_remote_authority.py`
