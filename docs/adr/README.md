# CWO Architecture Decision Records

本目录记录会影响长期架构、责任边界、兼容性或治理方式的决定。它不把每个 bug fix、每个
函数选择或每次测试结果都升格为 ADR。

## Decision index

| ID | 主题 | 当前状态 |
| --- | --- | --- |
| [ADR-001](ADR-001-human-authority-and-submission-boundary.md) | Human Authority、Local Draft 与 Remote Submission 边界 | Implemented |
| [ADR-002](ADR-002-typed-artifact-pipeline.md) | Typed Artifact Pipeline 作为可配置传播图 | Implemented |
| [ADR-003](ADR-003-run-workspace-and-concurrent-work.md) | RunWorkspace 隔离与并发人类工作 | Implemented |
| [ADR-004](ADR-004-validation-review-and-promotion.md) | Deterministic Validation、Independent Review 与 Promotion | Implemented |
| [ADR-005](ADR-005-legacy-compatibility-and-typed-boundary.md) | Legacy compatibility 与 typed workflow 边界 | Implemented |
| [ADR-006](ADR-006-candidate-revision-rollover.md) | 未接受 Authority Candidate 的 revision rollover | Implemented |

“Implemented”表示该决定已体现在当前 CWO 代码、配置模型或测试中；不表示所有未来扩展都
已完成，也不追溯虚构历史会议或批准人。PAIS-specific Decision、Requirement 和 review
evidence 仍归 PAIS repository 或其 external runtime state 管理。
