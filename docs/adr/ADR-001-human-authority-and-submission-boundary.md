# ADR-001: Human Authority 与提交边界

**Status:** Implemented

## Context

Human 可能在本地工作树中持续编辑下一版架构文档，而 CWO 同时需要稳定处理已经提交的上一
版。若把本地文件变化直接当作 authority change，正常 Draft 工作会触发错误的传播、冲掉
分析输入，或把 Human 变成每一步都要操作 workflow 的人。

## Decision

对于声明 remote authority 的项目：

- local checkout 是 Human Draft Workspace，不是 change trigger；
- 配置的 remote branch 上的精确 authority path/blob 是 submitted authority source；
- CWO 在 external bare cache 中读取 remote，并保存 commit、blob、content hash 和 immutable
  snapshot；
- 只有 authority blob/content 真正改变才进入 Authority Change Intake；无关 remote commit 是
  no-op；
- 当前运行绑定 immutable snapshot，不读取正在变化的 local Draft；
- Human Authority promotion 与 downstream artifact promotion 分离。

通用 CWO 保留 Authority Role 抽象，不把 `Human Guide` 文档风格硬编码为所有项目的最高层
形式。

## Rationale

提交行为是 Human 对某一版权威输入的明确表达，本地编辑则是尚未提交的工作过程。分开两
者让 Human 可以继续工作，也让 Agent 的输入具有可重建的 commit/blob provenance。

## Alternatives Considered

- 监听本地 authority 文件：会把 Draft 误当提交，且无法在 Agent 和 Human 同时写入时区分
  意图。
- 只比较 remote commit：无关文档变化会产生假 authority change。
- 由 Agent 直接读取当前本地文件：输入不稳定，恢复时不可证明使用了哪一版。

## Consequences

CWO 需要 remote cache、snapshot 和 ledger；网络失败必须保留旧状态。Human 若要提交新权威，
必须通过配置的 remote source，而不是仅保存本地文件。

## Related Artifacts / Evidence

- `src/contract_workflow/remote.py`
- `src/contract_workflow/authority.py`
- `tests/test_remote_authority.py`
- `docs/safety-model.md`
