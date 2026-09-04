# Contract Workflow Orchestrator 架构原理与设计指南

## 1. 文档定位

本文档记录 Contract Workflow Orchestrator（CWO）的长期架构模型、责任边界和演化原则。
它解释 CWO 为什么存在、各层解决什么问题，以及哪些事情明确不属于 CWO。精确的字段、
状态转换、项目契约和实现任务仍由项目自己的 Engineering Specification、Machine Contract、
Implementation Design 和 Plan 定义。

CWO 的设计目标不是把一个更大的 Prompt 包装成 CLI，而是建立一个能够在上下文丢失、Agent
重启、人工并行修改和候选版本演化时仍保持可追踪性的控制平面。

## 2. CWO 要解决的问题

高层 Human Intent 要变成 Production-ready Code，中间至少要经过多次语义收敛。若只把一
段需求直接交给 Coding Agent，通常会出现几类不可审计的问题：架构决策被实现过程重新发
明，规范要求和测试目标脱节，任务依赖被凭方便排列，候选文档覆盖了已接受文档，Agent 的
部分执行结果无法判断是否已经写回项目，以及人类在继续编辑时被迫停止整个流水线。

CWO 所属的工程开发体系因此采用一条自上而下、层层递进、层层校准、层层评判审查、层层验
证、层层验收、层层夯实的传播链，最终把高层 Human Intent 转换为可追踪、可验证、可审计的
代码。CWO 负责这条链的控制条件：

```text
Authority
→ durable state
→ artifact dependency graph
→ bounded workflow stage
→ deterministic validation
→ independent review
→ evidence / promotion
→ recovery and scheduling
```

CWO 不负责替每个技术领域发明规范内容，也不负责替 Skill 编写 Schema、测试方法或模块设
计。它决定 WHAT 和 WHEN；Skill 决定 HOW。

## 3. 读者应先建立的整体模型

一个具体项目通常同时包含三种不同性质的东西：

| 层次 | 回答的问题 | 典型所有者 |
| --- | --- | --- |
| Authority | 系统应该是什么，哪些语义具有权威性？ | Human / 外部提交的权威源 |
| Engineering artifacts | 这些语义如何被正式化、机器化、设计化和拆解？ | CWO 编排，Skills 产出内容 |
| Execution evidence | 这次运行实际产生了什么？是否可验证、可恢复、可回溯？ | CWO runtime state、Validator、Reviewer、Agent |

CWO 是连接三者的控制平面，不是三者的内容替代品。它的核心对象是“带有来源、状态、依赖
和证据的受控工作”，而不是某个特定项目的 `Node`、API 或业务模型。

## 4. 逐层降低不确定性

Artifact 向下传播时，允许的自由度应逐渐减少：

```text
Semantic freedom       ↓
Architecture ambiguity  ↓
Implementation choices  ↓

Formalization           ↑
Traceability            ↑
Machine verifiability   ↑
Evidence strength       ↑
Implementation determinism ↑
```

这不是要求每层都重复上一层内容，而是要求下游不能重新发明上游本应决定的语义。上游没有
决定的事项必须保留为开放问题或交给正确的下一层；下游不能用“合理猜测”把开放问题伪装成
既定规则。

### 4.1 按层隔离缺陷

问题应在它所属的层解决：

```text
Architecture defect
→ Architecture / Human Authority

Engineering Specification defect
→ Engineering Specification

Machine Contract defect
→ Machine Contract

Conformance defect
→ Conformance Specification

Implementation Design defect
→ Implementation Design

Plan defect
→ Implementation Plan

Task defect
→ Task Contract

Code defect
→ Coding / Review / Patch
```

“让下游 AI 合理猜一下”不是缺陷修复机制。跨层下沉会把本应一次解决的问题复制到所有下
游消费者，并让后续验证无法判断哪一个解释具有权威性。

## 5. 角色与责任边界

```text
Human
  = Authority

CWO
  = state, dependency, gate, scheduling, validation hook, promotion, recovery

Skill
  = artifact content production and semantic review method

Deterministic Validator
  = structure, identity, reference, hash, traceability, closure checks

Independent Reviewer
  = semantic challenge in a separate invocation

Coding Agent / Codex
  = bounded implementation execution
```

最重要的治理原则是：

> Human is Authority, not workflow operator.

机器可确定解决的事实不应反复要求 Human 手工推动。HumanDecision 只处理真正的权威选择，
例如架构歧义、破坏性兼容策略、安全权威选择、不可逆架构路线或明确的治理审批。

Validator 失败、普通测试失败、可修复的文档缺陷、依赖重算和普通代码缺陷，不能仅因为它
们阻塞当前 work 而升级为 HumanDecision。

## 6. Authority 不是固定文档类型

当前 PAIS 案例把 Human Guide 作为最高层 Architecture Authority，但通用 CWO 不应把某种
文档风格当成永恒前提。Human Authority 是一个可配置集合；在 CWO 0.8.0 中，集合成员
可以使用四个稳定角色：

```text
Human Authority Set
├─ ARCHITECTURE_GUIDE
├─ ENGINEERING_DIRECTIVE
├─ PROJECT_DECISION
└─ REFERENCE_POLICY
```

`ARCHITECTURE_GUIDE` 负责系统目标、心智模型和边界；`ENGINEERING_DIRECTIVE` 负责 Human
要求采用的高层工程方向；`PROJECT_DECISION` 是项目级明确取舍或例外；`REFERENCE_POLICY`
规定外部资料如何被参考。被引用的外部项目不是 Authority。不同成员发生真实冲突时，CWO
不得用固定优先级静默覆盖，而应保留冲突并进入明确的 HumanDecision/治理边界。

没有 `authority.members` 的旧项目仍被解释为一个 `ARCHITECTURE_GUIDE` 单成员集合。集合
身份由成员 id、role、path、content hash 的规范化排序确定；远端提交和成员 snapshots 与
既有 Authority ledger 共用，candidate rollover 不会删除旧成员或旧 Decision 证据。

通用 Authority classifier、Authority precedence engine、自动发现和选择 Reference 仍是
目标架构，不由 0.8.0 假装实现。

### 6.1 Authority level 作为分析模型

以下层级用于帮助理解传播关系，不是当前运行时枚举：

```text
L0 Intent / Goal
L1 Architecture Authority
L2 Engineering Authority
L3 Formal / Machine Contract
L4 Verification Authority
L5 Implementation Architecture
L6 Work Contract
L7 Execution
```

同一输入混合多个层级时，应识别为 `LAYER_MIXING`，再由正确的分析或人工权威决定如何拆
分。不能因为一个文件同时提到目标、字段和代码，就默认它同时拥有所有层的权威。

## 7. Typed Engineering Artifact Pipeline

在启用 `artifact_pipeline` 的工作流中，CWO 使用 Artifact Graph 作为传播图：

```text
HUMAN_GUIDE
      ↓
ENGINEERING_SPEC
      ↓
MACHINE_CONTRACT
      ↓
CONFORMANCE_SPEC
      ↓
IMPLEMENTATION_DESIGN
      ↓
IMPLEMENTATION_PLAN
      ↓
PLAN_GRAPH
      ↓
TASK_CONTRACT
      ↓
CODE / REVIEW
      ↓
FINAL CONFORMANCE
```

这是一条语义链，不是要求每个项目必须启用全部类型。每个项目通过配置声明实际 Artifact、
依赖、Skill、Validator、Review 要求和 promotion policy。没有 typed 配置的旧项目继续使
用 legacy Contract/Plan adapter。

### 7.1 各层职责

| Artifact | 责任 |
| --- | --- |
| Human Authority | 决定目标、架构边界和不可由下游擅自改变的语义 |
| Engineering Specification | 把架构意图正式化为可实现的规范性工程要求 |
| Machine Contract | 选择 Schema、状态机、不变量、序列化和错误契约等机器可验证表示 |
| Conformance Specification | 定义如何从可观察行为和证据判定符合性 |
| Implementation Design | 决定本项目如何用模块、组件、状态、数据流和错误边界实现这些义务 |
| Implementation Plan | 将已确定的设计拆成依赖正确、范围受限的工作 |
| Plan Graph | Plan 的可重建机器投影，不是新的 Human Authority |
| Task Contract | 单个 Coding work item 的需求、范围、输出和验收闭环 |
| Code / Review | 产生实现和独立审查证据 |
| Final Conformance | 用批准的 Conformance Spec、Machine Contract 和实现证据给出最终判定 |

### 7.2 Artifact 生命周期

```text
MISSING
→ PENDING
→ GENERATING
→ CANDIDATE
→ REVIEW_REQUIRED
→ REQUIRES_PATCH
→ APPROVED
→ PROMOTION_READY
→ ACCEPTED
```

候选物和 accepted artifact 始终分离。上游候选变化会使固定其旧 hash 的下游产物过期；
下游不能继续伪装成基于最新上游产生。`AUTO`、`HUMAN_GATE` 和 `EXTERNAL` 决定接受动作
由谁完成，但都不能跳过 hash、依赖、Validator、Review 和候选存在性检查。

### 7.3 质量治理闭环

每个长期 Artifact 应逐步形成三份可检查的边界：

```text
Definition of Input
Definition of Done
Definition of Handoff
```

完整质量路径是：

```text
Input Readiness
→ Generate
→ Layer Calibration
→ Deterministic Validation
→ Independent Semantic Review
→ Patch / Resolve
→ Handoff Readiness
→ Promotion
```

Layer Calibration 检查 Artifact 是否处于正确抽象层、颗粒度是否合适、是否混入下层细节，
以及本层必须决定的问题是否缺失。Artifact Review 则检查在正确层级内内容是否正确、闭合
和自洽。二者不能混为一次“看起来没问题”的审查。

当前实现已经提供部分通用生命周期、Validator hook、Review stage 和 promotion primitive；
各项目的完整 `Definition of Input/Done/Handoff` 仍是需要逐步建设的质量治理目标。

## 8. Authority 演化与候选版本

当前 PAIS 集成采用以下边界：

```text
Local working tree
  = Human Draft Workspace

GitHub main submitted revision
  = Human Authority submission source

CWO external ledger / snapshots
  = accepted, candidate, processed revision state
```

本地保存、重命名或继续编辑不会自动触发 Authority Change。Remote Watcher 读取配置的远端
分支和精确 authority blob；无关 commit 不构成 authority change。提交的 revision 被保存为
不可变 snapshot 后，分析不依赖正在变化的本地 Draft。

一次尚未接受的 `AuthorityChange` 可以有多个候选 revision：

```text
AuthorityChange
├─ accepted base
├─ candidate revision 1 → SUPERSEDED
└─ candidate revision 2 → ACTIVE
```

如果当前 candidate 尚未 accepted，新的同源提交可以在同一 Change Record 内 rollover，并保
留旧 candidate、旧 review 和旧 Decision 历史。如果 candidate 已经 accepted，新的提交必须
成为新的 AuthorityChange；不可中途替换已经作为下游地基使用的 accepted revision。

Authority Change Analyzer 只声明直接影响的 artifact、需求和锚点；CWO 根据配置的 Artifact
DAG 计算下游闭包和执行顺序。自然语言的 `required_propagation` 不能在 typed workflow 中
取代依赖图。

对于多成员集合，远端提交首先产生集合级 immutable candidate。成员变更被明确分类为
`UNCHANGED`、`ADDED`、`MODIFIED` 或 `REMOVED`；hash 相同且 role 相同的成员可以复用其
member-level review evidence，但集合变化仍必须重新完成 cross-authority consistency 和
handoff readiness review。Human promotion gate 只批准集合基线，不批准下游工程产物。

## 9. RunWorkspace 与并行人类工作

Agent 不以真实项目目录作为执行工作区。每次 bounded invocation 按如下路径运行：

```text
real project snapshot
        ↓
external RunWorkspace
        ↓
Agent invocation
        ↓
workspace diff
        ↓
deterministic scope / authority / drift validation
        ↓
transactional commit-back or discard
```

Snapshot 包含 invocation 开始时的 tracked、modified、deleted 和相关 untracked 文件视图，
但不包含 `.git` 内部瞬态文件、缓存和 CWO external state。Agent 的有效 cwd 必须是该
RunWorkspace；`danger-full-access` 只是进程沙箱降级，不得改变这一 cwd 绑定。

严格只读阶段的 workspace 变化会被丢弃并作为策略违例记录。候选生成阶段只提取声明且验
证过的候选 Artifact。Task execution/patch 只能在满足 outcome、scope、authority、baseline、
Git audit 和无 drift 等条件时，以 validated changed-file set 原子写回真实项目。

Invocation 期间的真实项目变化按责任分类，而不是一律视为同一错误：

```text
AUTHORITY_DRIFT
ACCEPTED_UPSTREAM_DRIFT
CURRENT_TARGET_DRIFT
LOCAL_DRAFT_DRIFT
UNRELATED_CONCURRENT_DRIFT
```

Authority、accepted upstream 和当前写入目标 drift 会阻塞或拒绝写回；Local Draft 和真正无
关的 concurrent drift 被记录、保留并允许安全继续。安全性不是靠 Prompt 自律，而是靠
workspace、baseline、diff 和 commit-back 前置条件共同实现。当前 Linux 环境若无法提供内核
命名空间隔离，absolute path 访问风险仍存在，因此 origin monitoring 与 transactional
commit-back 仍是必要防线。

## 10. Scheduling、Gate 与 Recovery

CWO 使用依赖感知 scheduler 维护 work item 状态。直接受影响的 work 可进入
`BLOCKED_BY_AUTHORITY_CHANGE` 或 `BLOCKED_BY_HUMAN_DECISION`，后继 work 进入
`WAITING_DEPENDENCY`，独立 work 保持 `READY` 并可顺序执行。一个 Decision 不应把整个工程
变成全局停工，只有不存在可运行的 READY work 时工作流才进入 `WAITING_FOR_HUMAN`。

HumanDecision 是一个带问题、选项、理由、来源、影响范围和证据的持久化请求。它复用已有
Decision / ADR / `cwo decide` 机制，不为 authority promotion、artifact promotion 和普通
缺陷分别创建审批系统。批准一个 Human Guide 只能批准该 Human Guide；下游 Artifact 仍须独
立生成、验证、审查并按照自己的 policy promotion。

恢复只依赖持久化 state、run artifacts、当前 workflow、Git 和项目证据，不依赖上一次对话记
忆。未完成或 side effect 不明的 invocation 不自动提交；只有 outcome、workspace、baseline
和目标状态全部能确定地重建时，才允许幂等恢复。

## 11. Reference Governance

External Reference 是证据或设计启发，不自动成为 Authority。可使用以下角色表达用途：

```text
MUST_ALIGN
PREFERRED_PATTERN
INFORMATIVE_REFERENCE
NEGATIVE_REFERENCE
```

推荐的 precedence 是：

```text
Human Authority
>
Accepted Project Engineering Artifacts
>
Explicit Project ADR / Decisions
>
External References
>
Existing Implementation Evidence
```

该顺序是 CWO 的治理方向，具体项目仍需定义冲突处理规则。Reference Suitability 应至少
检查问题域、抽象层、系统边界、runtime 假设、状态模型、安全假设、生命周期、扩展模型、
成熟度以及与 Human Authority 的冲突。

`REFERENCE_POLICY` 属于 Human Authority；`PREFERRED_PATTERN`、`MUST_ALIGN`、
`INFORMATIVE_REFERENCE` 和 `NEGATIVE_REFERENCE` 是用途角色，不是把外部项目提升为项目
规范的机制。Engineering Specification 可以记录 `derived from` Human Authority 与
`informed by` Reference 的区别，但 Reference 本身不能直接生成 normative Requirement。

经审计的 Reference Pack 应保存 reference id、repository/standard、commit/tag/version、角
色、用途、非用途、提取的模式和已知不匹配。PAIS 中对 MCP 的使用可以作为 informative example，
但 CWO 不依赖 MCP；协议/schema 治理模式可以借鉴，物理安全、物理状态权威和设备生命周
期不能因为软件协议相似而直接照搬。

## 12. Target Understanding 与未来方向

在进入正式工程传播前，CWO 最终应能回答：

```text
What are we building?
What is the target system?
What authority level is the current input?
What is authoritative, and what is evidence/reference?
Which artifact layers are necessary?
```

目标流程为：

```text
Human Inputs
→ Target Understanding
→ Authority Classification
→ Authority Quality Calibration
→ Reference Suitability
→ Artifact DAG Planning
→ Engineering Propagation
```

目前这套 Target Understanding、通用 Human Authority Set precedence 和完整 quality-contract
分析尚未作为独立运行时能力实现。文档记录它们是设计方向，不把它们写成当前 CWO 已经自
动完成的事实。

## 13. Self-hosting 原则

未来如果 CWO 用自己的 typed pipeline 改进下一版 CWO，应遵循：

```text
Accepted CWO N
→ orchestrates CWO N+1 candidate
```

Candidate CWO 不能自己给自己颁发合格证。其 validation、review、promotion 和 bootstrap
authority 必须由当前 accepted 控制平面或明确的人类权威完成。这是治理原则，不是本版本
新增的 self-hosting workflow。

## 14. 与相邻文档的关系

- [`docs/state-machine.md`](state-machine.md)：stage、outcome 和状态转换参考。
- [`docs/safety-model.md`](safety-model.md)：运行时安全、不变式和 hard stop 边界。
- [`docs/architecture.md`](architecture.md)：实现组件与当前控制流的简要说明。
- [`docs/design-influences.md`](design-influences.md)：外部实现模式的影响范围。
- [`docs/adr/README.md`](adr/README.md)：长期决策及其理由。
- [`docs/CWO_当前架构状态.md`](CWO_当前架构状态.md)：当前版本的事实状态，不代替本文档的原则。

精确的项目契约由项目自身维护。CWO 的长期目标是让这些 artifact 都能提供明确输入、完
成、交接和 provenance，而不是把所有内容复制进 CWO。
