# ADR-003: RunWorkspace 隔离与并发人类工作

**Status:** Implemented

## Context

Agent 需要能够在受限 runtime 中执行，但真实 project working tree 同时包含 Human Draft、既有
未提交修改和其他用户工作。若 Agent 直接以真实项目为 cwd，一次越界写入就可能破坏 authority
或覆盖用户内容；若把所有 dirty state 都视为污染，又会不必要地停止安全工作。

## Decision

每次 Agent invocation 都从 invocation-start 的真实项目视图创建 external RunWorkspace。Agent
有效 cwd 必须是该 workspace；Codex 的 `-C` 不能指向 authoritative origin。结束时 CWO 对
workspace diff、real-project baseline、authority、allowed paths、Git audit 和 target drift
做确定性检查。

只读阶段丢弃 workspace mutation；候选阶段只 externalize 已声明候选；Task execution/patch
通过 validated changed-file set transactional commit-back。实时 filesystem mirroring 不被允
许。

真实项目 drift 分为 authority、accepted upstream、current target、local draft 和 unrelated
concurrent drift。前三类按安全策略阻塞或拒绝；local draft 与不相关且未被 Agent 改写的并发
变化应保留并允许继续。

## Rationale

隔离将 Agent 的“可能有副作用”转换成可验证的 workspace diff。baseline 使 CWO 能证明 commit-
back 不会覆盖 invocation 期间 Human 对目标文件的新修改。该模型同时保留 Human 继续编辑
Draft 的能力。

## Alternatives Considered

- 直接让 Agent 运行在真实项目：无法提供事务边界。
- 只 checkout HEAD：会丢失用户 dirty work。
- 全局忽略 dirty paths：会掩盖 Agent 对未授权文件的修改。
- 依赖 Prompt 要求 Agent 自律：不能作为安全控制。

## Consequences

RunWorkspace 和 external runtime state 增加了快照与证据成本。当前环境若没有 kernel namespace
隔离，absolute-path 访问仍是 residual risk，因此 origin fingerprint 和 commit-back gate 不
能删除。

## Related Artifacts / Evidence

- `src/contract_workflow/workspace.py`
- `src/contract_workflow/runners/codex_cli.py`
- `src/contract_workflow/orchestrator.py`
- `tests/test_workspace_isolation.py`
- `tests/test_cwo.py`
