# CWO 当前架构状态

## 1. 状态基线

本页是事实状态记录，不是架构愿景。以下结论以 CWO repository 当前 `main` 工作树、源
码、测试、配置和 Git history 为依据；若与未来提交不一致，应更新本页，而不是把历史描述
当作运行时真相。

```text
Documented version: 0.7.2
Runtime implementation baseline: e5e7944eb098bbaa14812bcfe8481862df20fa6e
Architecture memory baseline: 3902849022da3898c1139e2b4c56fb4481806438
Repository snapshot observed when the previous state baseline was written:
  3902849022da3898c1139e2b4c56fb4481806438
```

这些是可追溯的历史基线，不是对未来 repository `HEAD` 的永久声明；本页随实现状态更新时
应重新观察并记录新的 snapshot，而不应把本页自身的提交误写成永远的当前 HEAD。

当前工作树在形成此状态记录前为 clean。PAIS 是 CWO 的真实集成案例，但不属于 CWO repository
的 authority。

## 2. 当前实现能力

### IMPLEMENTED

- Python 3.11+ package 与 `cwo` CLI。
- Workflow YAML 加载、schema 校验、workflow digest 和持久化 `WorkflowState`。
- bounded Agent stages、结构化 `outcome.json`、独立 stdout/stderr 和 run metadata。
- `READY`、`WAITING_DEPENDENCY`、`BLOCKED_BY_HUMAN_DECISION`、
  `BLOCKED_BY_AUTHORITY_CHANGE`、`SUPERSEDED` 等 work-item 状态及依赖感知 scheduler。
- scoped HumanDecision、Decision Request、ADR 持久化、`cwo decide` 和局部重新调度。
- Remote Authority Watcher：通过外部 bare Git cache 获取配置的 remote/branch，按精确
  authority blob/content hash 判断提交是否真正改变 authority。
- external authority ledger、remote commit/blob/content provenance 和 immutable snapshots。
- Authority Change Intake、C0-C4 分类结果接收、直接影响声明校验、依赖闭包计算和同一
  Change Record 下的 candidate revision rollover。
- typed `EngineeringArtifact` 模型及八种当前支持的 artifact kind；candidate/accepted 分离。
- generic artifact generation、validation、review、patch stage；`AUTO`、`HUMAN_GATE`、
  `EXTERNAL` promotion policy 和 deterministic promotion primitive。
- project deterministic validator invocation seam：按 configured role 执行结构化 JSON
  validator，并校验 exit code、candidate hash、workspace mutation 和 validator evidence。
- Plan Graph 的 deterministic DAG validation、hash provenance 和 incremental reconciliation。
- external disposable `RunWorkspace`、tracked/untracked baseline、workspace diff、authority
  protection、target drift 检查和 validated transactional commit-back。
- Codex CLI adapter 的 workspace cwd binding；配置的 authoritative origin 不作为 Agent cwd。
- recovery 对已完成 outcome、运行中 Agent、失败 runner、workflow digest 和遗留 drift 状态
  的受限处理。
- canonical review evidence registry 位于 `WorkflowState` / 外部 runtime `state.json`；支持
  historical review evidence migration、稳定 finding identity、幂等登记、结构化 provenance、
  unresolved carry-forward 和正常 resolve 生命周期。

### PARTIAL

- typed pipeline 的通用编排、artifact 状态和 promotion 已存在，但各项目仍须提供适合自己
  规范的 Skill、project validator 和 artifact 内容；CWO 不替它们定义语义。
- Remote Authority 的 `watch` 是简单轮询入口，不是 webhook、队列服务或 daemon manager。
- 在 Linux writable sandbox 不可用时，Codex runner 使用 `danger-full-access`，依赖 RunWorkspace
  cwd、origin baseline、scope validation 和 commit-back 防护；内核 namespace 无法阻止 Agent
  通过 absolute path 访问真实项目。
- Plan Graph 可以从 Plan 构建并驱动任务，但通用 Target Understanding、Authority Set
  precedence 和输入层级自动分类尚未成为运行时阶段。
- Human Authority promotion、下游 artifact promotion、项目 Git publishing 是不同边界；
  CWO 不自动 commit/push/tag/release PAIS。

### PLANNED

- 将 Target Understanding、Authority Quality Calibration、Reference Suitability 和
  Human Authority Set precedence 形成明确的分析扩展点。
- 为项目质量契约统一表达 Definition of Input、Definition of Done、Definition of Handoff，
  并将 calibration 与 review 的证据区别记录。
- 在不把内容规则硬编码进 CWO 的前提下，逐步提高 artifact provenance、handoff readiness
  和跨层 traceability 的可见性。

### DEFERRED

- 并行 worker、dashboard、webhook server、distributed queue/service、daemon manager。
- 自动改写 Contract、自动改写 Plan、自动生成全部任务内容。
- PAIS-specific semantic validators、通用 security penetration testing 和自动 Git publishing。
- CWO self-hosting bootstrap 的完整实现。

## 3. 当前 canonical pipeline

启用 typed configuration 时，canonical graph 是：

```text
HUMAN_GUIDE
→ ENGINEERING_SPEC
→ MACHINE_CONTRACT
→ CONFORMANCE_SPEC
→ IMPLEMENTATION_DESIGN
→ IMPLEMENTATION_PLAN
→ PLAN_GRAPH
→ TASK_CONTRACT
→ CODE / REVIEW
→ FINAL CONFORMANCE
```

没有 `artifact_pipeline` 的 workflow 使用 legacy Contract/Plan adapter。typed workflow 不应
从旧的 free-form propagation token 选择 downstream stage，也不应在 typed upstream 缺失时
静默 fallback 到 legacy Plan Graph。

## 4. 当前组件责任

| 组件 | 当前事实责任 |
| --- | --- |
| `config.py` | 读取并规范化 workflow、artifact、Skill 和 validator 配置 |
| `state_machine.py` | 提供确定性的 stage transition，不解释自然语言 |
| `scheduler.py` | 计算依赖、受影响闭包、READY work 和 scoped gate |
| `authority.py` / `remote.py` | 远端 authority 发现、快照、Change Record 和 revision 入口 |
| `artifacts.py` | typed artifact 初始化、依赖 hash、candidate/accepted 生命周期和 promotion 前置检查 |
| `project_validator.py` | 安全构造 validator argv、隔离执行和结构化 evidence 解析 |
| `plan_graph.py` | Plan Graph schema、DAG、hash 和 reconciliation |
| `workspace.py` | RunWorkspace snapshot、fingerprint、diff 和 validated apply |
| `git_audit.py` | 以 workflow、authority 和 baseline 为上下文分类 Git 变化 |
| `orchestrator.py` | 连接 preflight、runner、workspace、outcome、artifact 和 scheduler |
| `runners/codex_cli.py` | 将 Agent invocation 绑定到 RunWorkspace cwd |
| `state_store.py` | 外部 runtime state 的原子持久化和恢复证据 |

## 5. 当前 authority 与 promotion 语义

Local project checkout 不是默认 Human Authority trigger。对于配置为 remote authority 的项目，
CWO 只消费 immutable remote snapshot。一个未接受 Change Record 可以保留旧 candidate、旧
Decision、旧 review 和新 candidate 的完整 lineage；新 candidate 不覆盖旧记录。

Artifact promotion 的当前原则是：

1. candidate 内容存在且 hash 正确；
2. upstream hash 未漂移；
3. review 和 deterministic validator evidence 有效；
4. 必要依赖处于可消费状态；
5. 没有 superseding candidate、未解决 HumanDecision 或 target drift；
6. 由 CWO deterministic code 执行 acceptance，不由 Agent shell 直接覆盖 accepted artifact。

`EXTERNAL` artifact 只能等待外部权威源接受。Human Guide 属于此类时，GitHub main 是提交
入口；CWO 不因本地 Draft 变化而重建本轮 authority。

## 6. 当前 safety / recovery 事实

每个 Agent invocation 都有 external RunWorkspace。只读 stage 的 workspace mutation 被丢弃；
candidate stage 只 externalize 通过验证的候选；Task mutation 只有在 real project baseline
没有变化、写入路径允许、authority 未变、Git audit 通过时才可 commit-back。

当前 drift 分类区分：

```text
AUTHORITY_DRIFT              → hard stop
ACCEPTED_UPSTREAM_DRIFT      → hard stop
CURRENT_TARGET_DRIFT         → reject commit-back / hard stop
LOCAL_DRAFT_DRIFT            → preserve and continue for remote-authority runs
UNRELATED_CONCURRENT_DRIFT   → record, preserve, continue if outside target
```

恢复不会对不明副作用的未完成 Agent invocation 自动 commit。现有 state、run metadata、
workspace evidence 和真实项目 baseline 必须足以证明可安全重建；否则进入 hard stop。

## 7. Skills 与 Validator 路由

当前已知 Skill 角色如下：

| Artifact / stage | Skill 或机制 |
| --- | --- |
| Human-facing architecture guide | `technical-specification-guide` |
| Engineering Specification | `engineering-contract-spec` |
| Machine Contract | `machine-contract-spec` |
| Conformance Specification | `conformance-specification` |
| Implementation Design | `implementation-design-guide` |
| Implementation Plan | `contract-implementation-planner` |
| Task / Coding / Review / Patch | `contract-driven-coding` |
| Markdown → publication（未来薄层） | `technical-document-publisher` |

Skill 负责内容方法，CWO 只负责 identity、依赖、生命周期、routing、validation hook 和证据。
Project validator 负责确定性结构与 traceability，独立 Reviewer 负责语义挑战；二者不能互相
替代。

## 8. 当前明确限制与活动缺口

- PAIS TASK-002 的历史 review finding “capabilities / frames scalar values were incorrectly
  converted to tuple” 已通过 CWO canonical historical evidence migration 持久化；持久化缺口
  已关闭，但 finding 本身仍为 `UNRESOLVED`，直到未来实现与独立 review 证明其解决。
- CWO 本身不提供 PAIS-specific Engineering Spec、Machine Contract、Conformance 或 Design
  内容；这些内容需由项目配置的 Skill 和 validator 产生、审查和固化。
- 当前文档体系没有把 Human Authority Set precedence 自动化；存在多个权威冲突时仍需明确
  的项目治理或 HumanDecision。
- 当运行环境只能使用 `danger-full-access` 时，RunWorkspace 是主要执行视图，但不是内核级
  absolute-path 隔离；这是已知 residual risk。
- 完整 Final Conformance 的证据消费取决于项目提供已批准 Conformance Spec 和相应执行证据；
  CWO 不把普通 test summary 自动升级为 conformance proof。

## 9. PAIS real integration case（简表）

PAIS 曾验证过 remote Human Guide candidate detection、同一 CR candidate rollover、immutable
snapshot 保留和 candidate-specific HumanDecision。当前最新 candidate 的下游 typed propagation
在 Human Guide promotion gate 前保持阻塞；这证明 authority gate 的位置和 candidate/accepted
边界，但不等于 CWO 已完成 PAIS 全部 downstream generation 或 coding。

PAIS 当前本地 Human Guide 是 Draft，不应被本页或 CWO 视为已提交 authority。PAIS 的具体 CR、
requirements、schema 和代码实现属于 PAIS 自己的项目记录。

## 10. 证据来源与维护规则

更新本页时优先检查：

```text
source code → tests → workflow/config → runtime evidence → git history
```

对无法证明的能力标记 `UNVERIFIED` 或降级为 `PLANNED`。不要仅凭聊天记录、旧报告或某次
Agent 输出把目标设计写成当前实现。
