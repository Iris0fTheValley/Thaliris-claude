# Thaliris

[English](README.en.md)

**注意力才是最贵的资源。**

Thaliris 是一个面向 AI 编程 Agent 的**认知编排层**。它不负责把上下文压短，它要解决的是长程工作里的三个问题：

- **强模型的注意力被噪声稀释。** 探索、调试和日志不应留在负责关键判断的那条推理轨迹里。
- **原始需求和反证在交接中消失。** 一个局部“完成”的报告，不应悄悄覆盖掉此前已经发现的问题。
- **项目身份在长期工作中漂移。** 项目为什么存在、什么不能变，不应被一次次局部任务、实验或实现限制悄悄改写。

三者是一条因果链，起点是注意力稀释。为了保护关键推理，工作必须拆给上下文全新的 Agent，并跨越多个会话；拆分保住了注意力，也制造了交接边界。在一个任务之内，反证容易在边界上丢失；在任务之间，如果没有谁持有“项目是为了什么”，每次局部决定都可能把一个实现限制当成新原则，项目身份就这样一点点漂移。反过来，丢失的证据和漂移的身份又会把旧问题和噪声带回关键推理里。所以 Thaliris 不只隔离噪声，还明确规定隔离之后什么必须跨越边界、由谁持有。

> **Protect the reasoning. Isolate the noise. Isolation without amnesia.**

Thaliris 不另造 Agent Runtime，而是建在 Codex、DeepSeek Harness、Claude Code 这些 Host 的原生 Agent 能力之上，补上隔离、交接、证据连续性和项目身份这一层。

> **状态：Beta。** 在真实的复杂重构任务上，已经观察到实现者自主派发调查、Controller 发起独立审查、反证推动修复的完整链路。样本量还小，不足以证明普遍的质量或成本优势。见[实测观察](#实测观察)。
>
> **版本：** 本文描述各仓库 `main` 分支上的源码版本：Core 0.5.2、Codex adapter 0.5.1、Claude Code plugin 0.4.2、DSH plugin 0.3.1。版本号描述源码，不描述某台机器上的安装；仓库或磁盘上的文件更新，也不证明正在运行的 Host 已经加载新版本。

## 为什么不止于上下文管理

常见做法是在同一个 Agent 里管理更多上下文：压缩、检索、记忆。Thaliris 认为更大的问题不是 token 多，而是**同一条推理轨迹里互相竞争的目标太多**：既要调查又要设计、实现、调试、自审，还得记住此前的失败。

所以 Thaliris 管的是**谁来推理、带着什么推理**：

> **为每个推理上下文提供它需要的信息，不因为别的信息存在，就把它也塞进去。**

名字取自 Thalamus（丘脑，筛选并路由信息）与 Iris（虹膜，调节进入的光量）。

## 三根支柱

### 1. 认知隔离：把强模型的注意力用在刀刃上

一个长期的 **Controller** 持有用户目标、硬约束和验收标准。短期角色在全新的上下文里完成局部工作，不继承父级对话，只交回结论、来源、验证结果和会改变决策的未知项。

| 角色 | 做什么 |
| --- | --- |
| Investigator | 在选定范围内调查事实，返回来源、矛盾与未知项 |
| Implementer | 实现已确定的方向并完成验证 |
| Focused Implementer | 处理设计与实现高度耦合的问题，直到核心语义收敛 |
| Reviewer | 可选的独立只读挑战，用证据检验候选方案 |

角色按需选用，不是固定流水线。单 Agent、Controller 直接执行或委派都合法。不同角色可以配不同能力的模型：强模型只放在真正需要它的关键节点上。

### 2. Evidence Continuity：隔离，但不遗忘

隔离最大的风险是：审查者发现的反例，经过几轮交接和修复后，被一句“测试通过”悄悄淹没。Thaliris 用这几条规则防止这件事：

1. **反证会被登记。** 子角色发现的事实如果改变了局部结论、局部验收或关键未知项，就记录为一条 observation，附带来源、交接链路和它关联的用户需求。
2. **反证保持可见，直到被明确处置。** 在 Controller 针对当前候选，把每条相关需求明确判定为 *已满足 / 未知 / 需修复 / 已不再相关（并说明理由）* 之前，这条 observation 会在后续交接中持续出现。读过、转发过、确认收到都不算处置。
3. **新的好消息不能顶掉旧的坏消息。** 之后出现测试 `PASS`、Reviewer `READY`、子代理 `Completed` 或 Core `DONE`，都不能让此前的“需修复 / 未知”自动退场；Controller 必须显式引用并回应那条旧依据。
4. **在下判断之前重新呈现。** Adapter 在把子代理结果交给 Controller 时，同时附上仍未处置的关键证据，赶在它做下一次“是否完成”的判断之前。这一步不需要额外的 Agent、固定阶段或轮询。
5. **历史只追加，不改写。** 旧的观察和判断保留为历史，但不会自动被当作当前事实。

Core 只负责保存这些链接和只追加的历史，不判断什么重要；“重要不重要、是否满足需求”始终由 Controller 判断。

```text
Reviewer 发现反例 ──► observation（来源 + 关联需求）
                          │
     后续修复、测试 PASS、子代理 Completed …   ← 都不能让它消失
                          │
Controller 明确处置：已满足 / 未知 / 需修复 / 已不再相关
```

### 3. 语义归模型，机械归 Thaliris

机械层负责原生 Agent 身份、来源、版本、连续性和有界检索；语义判断交给模型。结论都带来源（文件 hash、Git blob、测试观察）；freshness 只说明这些来源没变，不说明所有依赖都没变。证据不足时保持 `UNKNOWN`，不把推测升级为确认。

## 核心设计：四类状态，各负其责

隔离之后，跨越 Agent 和会话边界的不是对话历史，而是四类显式状态。它们回答不同的问题，变更方式不同，也互不替代：

| 状态 | 回答什么 | 谁能改变 | 怎样跨 Agent、跨会话传递 |
| --- | --- | --- | --- |
| **项目身份**（`.project-identity/`，可选） | 项目为什么存在；哪些身份、不变量、原则、方向与非目标不能被低层工作悄悄改写 | 只能是人类所有者明确决定、由 Controller 记录的 amendment；证据只能对它提出 challenge | 随仓库进入 Git，跨所有任务与 Host；Controller 只看到一行摘要并按需读取，子角色只有在 Controller 把选定条目写进 handoff 时才看到 |
| **单次任务授权**（Task Authority） | 这一次任务里用户选定的意图：目标、范围、硬约束、验收标准与执行模式 | 任务开始时由 Controller 依据用户指令确立并冻结；改目标、范围、验收或模式都需要用户的新决定；子角色不能确立、扩大、改写或重新激活它 | 由 Adapter 锚定在仓库之外，中断与重连后仍然有效；它是 Controller 记录的用户意图，不是对 Host 所有者的认证，也不包含维护 live Host 的权限 |
| **证据**（任务 ledger） | 这一次任务里发现了什么、依据什么来源、关联哪条需求、Controller 如何处置 | 子角色登记 observation，只有 Controller 判断；历史只追加 | 绑定 Task ID 与 handoff 链；未处置的关键证据在 Controller 下一次判断前重新呈现，压缩、恢复或换会话接续任务后依然存在 |
| **长期知识**（`.agent-memory/`、`.milestones/`） | 已经学到、值得以后复用的项目知识与长期进度 | Controller 选择后写入，或交给 Curator 依据选定材料整理；带来源与 freshness | 随仓库保存，经 INDEX 按需检索；记录下来不等于自动塞给每个角色 |

自上而下，项目身份约束一个个任务，任务授权约束这一次工作，证据检验当前候选，长期知识保存学到的东西。下层可以质疑上层，但不能悄悄改写它：证据推不翻任务授权，任务里的结论不会自动升级为项目身份，修改项目身份也不会改写正在运行的任务已经冻结的授权。没有项目身份的项目照常运行，不会被阻塞或催促。

## 组成

| 部分 | 职责 |
| --- | --- |
| [Thaliris Core](https://github.com/Iris0fTheValley/Thaliris/blob/main/README.md)（共享 Core） | Task ID 与任务记录、任务授权、observation / judgment 链接、证据与 freshness、项目记忆、里程碑、项目身份、CAS 写入与回滚 |
| [Thaliris-codex](https://github.com/Iris0fTheValley/Thaliris-codex) | Codex 原生集成：Fresh Child、角色 profiles、原生 Agent 身份与生命周期、证据重新呈现 |
| [Thaliris-dsh](https://github.com/Iris0fTheValley/Thaliris-dsh/blob/main/README.md) | DeepSeek Harness 插件：Workspace/Session/Agent 集成、可选 Memory |
| [Thaliris-claude](https://github.com/Iris0fTheValley/Thaliris-claude/blob/main/README.md) | Claude Code 插件：Controller skill、角色子代理、Hooks 原生身份绑定与证据重新呈现 |

只有一套共享 Core。Core 不运行模型、不调度 Agent、不认证调用者；原生执行由各 Adapter 负责。

## 角色能力与推荐模型

> **绝对不要用小模型或低能力模型做控制层（Controller）。** 省钱应当来自把窄任务交给更便宜的子角色，而不是降级 Controller。Controller 的失误不会被子代理纠正，只会被放大：实测中，弱 Controller 会跳过受管流程、在隔离拒绝后退回单代理，或在审查反证仍未处置时就判定完成。

Controller 必须同时具备以下能力，每一项都很重要：

| 能力 | Controller 要回答的问题 |
| --- | --- |
| Global Coherence 全局一致性 | 做了几十上百步以后，是否还记得为什么要做这件事？ |
| Strategic Planning 战略统筹 | 能不能从最终目标反推路径、识别依赖、分配资源，而不是只看下一步？ |
| Retrospective Reasoning 回溯推理 | 能不能从历史决策中定位根本错误，而不是反复修补表象？ |
| Metacognitive Monitoring 元认知监控 | 能不能发现自己正在跑偏、重复劳动，或者陷入不可能完成的任务？ |
| Prospective Reasoning 前瞻推理 | 能不能预测当前决定对后续阶段的影响，主动避免未来的结构性问题？ |
| Adaptive Replanning 动态重规划 | 当原计划失效、条件变化时，能否重新组织全局工作，而不是机械延续旧计划？ |

各角色需要的能力与推荐模型：

| 角色 | 需要的能力 | Claude（Claude Code） |
| --- | --- | --- |
| Controller | 上述六项；整合原始目标、验收标准与证据，判定产品验收 | Opus |
| Investigator | 在选定范围内广泛、准确地收集事实与来源，说明已覆盖与未覆盖范围 | Sonnet |
| Implementer | 在已确定的方向内实现、验证并修复普通缺陷，直到确定性收敛 | Sonnet |
| Focused Implementer | 对耦合的不变量持续推理，吸收运行反馈直到语义收敛 | Sonnet 或 Opus，由 Controller 按任务难度选择 |
| Reviewer | 独立挑战已收敛的候选，跨边界寻找反证，不被作者的框架带偏 | Opus |
| Reasoning Specialist | 质疑问题框架、因果模型和决策依据 | Opus |
| Curator | 依据选定材料精确整理长期知识与 INDEX | Sonnet |

推荐值是起点，不是质量保证。角色与执行模型相互独立；实际运行的模型以 Host 的观测为准，而不是配置文件。

这些只是推荐。为每个角色选择模型是 Controller 的职责，在用户写入任务的约束之内可以自由选择；Controller 也可以用 `register-role` 注册新的项目角色。用户也可以让 Controller 独自完成全部工作：是否委派、委派多少，由用户决定。

## 实测观察

**这项基准测什么：** 我们有意挑了一个很难的单轮重构任务：桌面与 WebUI 共享 Live2D 行为，涉及模型选择、语音/动作队列和退出生命周期。比较的不是候选还剩几个 bug，而是原始需求能否持续约束调查、方案收敛、实现和验证，尤其是检查是否覆盖真实消费者路径、反证是否推动方案修正。A/B 是单代理样本，C/D 提供协作执行对照。

**结果要这样读：** 冻结候选接受独立产品后审；表中把原运行内的独立审查/修复与冻结后的后审分开。A/B 的原运行没有独立 Reviewer；它们后来接受 GPT-6.1 Sol/high 独立后审，不能算作组内自审或修复。A/B 保留原报告的高/中严重度标签和 V3 覆盖不足结论，不换算为本表的 P1/P2。各样本都有未闭合的需求，其中一些在起始代码里已经存在；缺陷数量不是质量总分。

| 样本（原执行日期） | 执行方式 | 原运行内独立审查/修复 | 冻结后独立后审结果 | P1 | P2 | 用时 | API 等价费 |
|---|---|---|---|---:|---:|---:|---:|
| A · 10/03 | 单代理 Sol/medium | 无 | 10/14–15：NOT_READY；1 高、2 中，另有 V3 renderer 覆盖不足 | — | — | 21.6 分 | $0.88 |
| B · 10/03 | 单代理 Luna/xhigh | 无 | 10/14–15：NOT_READY；2 高、3 中 | — | — | 54.1 分 | $0.23 |
| C · 10/14 | Luna 多代理 | 有，局部 | NOT_READY | 2 | 3 | 57.1 分 | $0.46 |
| D · 10/14 | Sol + Luna 多代理 | 有 | NOT_READY | 1 | 2 | 40.4 分 | $2.62 |
| C · 10/19 | Luna（派发被拒，回退单代理） | 无 | NOT_READY | 3 | 3 | 19.8 分 | $0.22 |
| **D · 10/19** | **Sol + Luna 原生多代理** | **有，两个反证触发修复** | **PARTIALLY SUPPORTED / NOT_READY** | **0** | **3** | **34.4 分** | **$2.91** |

表中费用是 API 等价参考值，用于比较，不是实际账单。A/B 的原执行日期是 2026-10-03，冻结后才于 10/14–15 接受 GPT-6.1 Sol/high 独立后审；表中的高/中标签沿用原报告，没有换算为 P1/P2。

A 在 16:46 UTC 已形成共享行为策略加渲染器适配的跨端方向，随后把桌面音频等待和 WebUI 片段调度问题纳入实现；其最终相关 Python 套件报告 92 passed/1 deselected，前端 7 files/20 tests，build 和 lint 均退出成功。后审仍发现桌面转换入口会用 Sakiko costume 覆盖显式选择的自定义模型，Web 无音频呈现只保留最新动作。这说明较早形成整体方向、完成局部验证，仍可能遗漏实际消费者路径。B 花了更长时间做广泛调查，16:59 UTC 才明确列出缺口和共享目标/renderer 方案；之后修正测试夹具与 lint，并完成 84 个 Python、16 个前端测试和 build/lint。后审仍复现角色状态与取消 ingress 未完整消费、bye 未停止正在播放的音频，显示更多投入和通过的局部测试也没有闭合事件链与生命周期一致性。两次后审都是独立评估，不是 A/B 原运行中的 Reviewer。

因此，A/B 与 C/D 对照的是调查、方案收敛、原始需求保持、验证方案和反证后的修正；不是比较冻结后剩下多少个 bug。

**D · 10/19 发生了什么：**

```text
Controller 把耦合的设计与实现交给 Sol
  → Sol 自主派 Luna 调查运行时支持与测试边界
  → Sol 交回局部候选
  → Controller 没有把“局部完成”当作结束，发起独立审查
  → Reviewer 找到两个推翻局部结论的反例：
      · 用户显式选择的模型会被桌面旧规则覆盖
      · 退出告别指令可能在事件通道关闭后才发出
  → Controller 采纳反证，派修复与回归
```

D · 10/19 的独立外部后审没有发现 P0/P1，但报告了三类 P2，结论为 PARTIALLY SUPPORTED / NOT_READY，不构成完整产品验收。三类反例是：取消后到达的预入队片段仍可恢复播放；shutdown bye 后在途 producer 仍能发布，独立前端复现也会接受之后的片段；Web 按版本选择嘴型参数名，无法驱动拼写与版本默认值不同但实际受支持的别名，桌面按实际 parameter IDs 选择。过程中的原生代理状态冲突按 Codex 原生生命周期异常处理。

这正是 Thaliris 要验证的能力：**局部成功不等于全局结束，反证会实际改变后续工作。**

**C · 10/19 不是一次成功的多代理运行：** Luna 请求把整段对话历史传给子代理，被 Fresh Context 规则正确拒绝；它没有改用独立上下文重新派发，后续退回单代理完成。这个结果说明隔离保护在起作用，也显示关键调度节点需要足够强的模型。

**目前还不能证明的：** 每行都是单次运行，各批次的 Host 与模型 effort 不完全一致，不是单变量实验；混合配置总成本更高；D · 10/19 中审查与修复阶段的反证虽然实际推动了修复，但没有全部登记为正式 observation，Evidence Continuity 的持久化链路仍在完善中。详见 [ABCD 基准](https://github.com/Iris0fTheValley/Thaliris/tree/main/benchmarks/abcd) 与[基准协议](https://github.com/Iris0fTheValley/Thaliris/blob/main/docs/thaliris-benchmark-protocol.md)。

## 快速开始

需要 Python 3.11+。Git 可选；使用下方 Git URL 安装需要 Git。

```bash
uv tool install git+https://github.com/Iris0fTheValley/Thaliris
cd your-repository
thaliris-core --root . init
TASK_ID=$(thaliris-core --root . task-start "example task" | python -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')
thaliris-core --root . --task-id "$TASK_ID" task-status
```

每个任务有独立的 Task ID，后续操作显式传入 `--task-id`。只安装 Core 不会启用多代理；需要原生集成时，按 [Codex](https://github.com/Iris0fTheValley/Thaliris-codex/blob/main/README.md)、[DSH](https://github.com/Iris0fTheValley/Thaliris-dsh) 或 [Claude Code](https://github.com/Iris0fTheValley/Thaliris-claude/blob/main/README.md) 指南安装。

初始化后的仓库：

```text
.agent-memory/   # 经选择、可跨任务复用的项目知识（可纳入版本控制）
.milestones/     # 长期进度：scope / decisions / progress / verification
.context/tasks/<task-id>/state.json   # 单个任务的状态与证据（默认被 Git 忽略）
```

任务状态保存结构化的事实、observation 和判断，不保存对话 transcript。持久化和交接是两个独立选择：记录下来的东西不会自动塞给每个角色。

项目可以选择记录一份**项目身份**（`.project-identity/`，随仓库进入 Git）：跨任务的“项目是为了什么、什么不能被低层工作悄悄改写”。没有身份是正常状态；有身份时，Controller 在会话开头只看到一行摘要，按需读取。只有在人类所有者明确决定后才能通过 amendment 修改，证据只能提出 challenge。

## 不是什么

不是 Agent runtime、工作流引擎、固定多代理流水线、向量数据库或 RAG 系统、全仓自动总结器，也不能替代源码、测试、编译器和运行时行为。

删除 Thaliris，底层开发流程也不应因此变得不正确。

## 延伸阅读

- [DESIGN.md](https://github.com/Iris0fTheValley/Thaliris/blob/main/DESIGN.md) — Core 架构与契约
- [路由协议](https://github.com/Iris0fTheValley/Thaliris/blob/main/docs/thaliris-routing-protocol.md) — 路由、交接、Evidence Continuity、等待与验收
- [任务授权契约](https://github.com/Iris0fTheValley/Thaliris/blob/main/docs/thaliris-task-authority.md)
- [提示词归属与研究动机](https://github.com/Iris0fTheValley/Thaliris/blob/main/docs/thaliris-prompt-design.md) — 含相关文献
- [角色说明](https://github.com/Iris0fTheValley/Thaliris/blob/main/docs/thaliris-role-packs.md) · [分包与 API 边界](https://github.com/Iris0fTheValley/Thaliris/blob/main/docs/host-neutral-packaging.md)

## Claude Code 原生集成

Thaliris-claude 是一个 Claude Code 插件，把共享语义接入 Claude Code 原生的子代理、Skill、Hooks 与插件配置。它内置一份按固定提交锁定的 Thaliris Core（当前为 Core 0.5.2，只用标准库），由 Core 提供任务记录、证据和任务授权（authority）API；适配层负责会话与任务关联、原生身份与 handoff 绑定、角色边界守卫和证据重新呈现，不另建 Agent Runtime。

主线程即 Controller，按 thaliris skill 准入任务、选择角色、写选择性 Handoff 并判定验收。角色是 `thaliris:*` 子代理；六个默认角色不是固定体系，Controller 可以用 `register-role` 按任务创建、修改项目角色（`thaliris-<name>`，可选职责、模型、effort 与原生能力，不能放宽权限或绕过 Hook），用 `unregister-role` 移除；已派发的子代理始终按派发时的角色身份与权限运行。Controller 用 `identity-status` / `identity-get` / `identity-trace` 按需读取项目身份，用 `identity-amend` / `identity-challenge` / `identity-relate` 记录修改，用 `identity-note`（记录用户明确陈述的原话，无需任务）/ `identity-suggest`（关闭或恢复收尾提议）自然形成身份（均仅 Controller）。Controller 用 `task-update` 记录事实、决策、进展、未决事项和里程碑，压缩、恢复或新会话 `attach` 时重新呈现。Runner 的 `task-start`、`register`、`judge`、`observe`、`task-update` 都有参数形式（不必写临时 JSON 文件），输出为紧凑 JSON，失败返回 `code`/`recovery`/`details`；写入是严格的 revision CAS，冲突时带回当前 revision 与近似的变化提示（不含 Active Work、Pending Results 等被替换字段）：确认写入仍适用就用新 revision 重试一次，需要时再读 `status`。`status` 默认紧凑，`--view full` 看全部。默认模型如下，只在 Controller 未指定时使用；显式选择保留（`effort` 同样按派发选择并记录），任务的 `claude-model:<alias>` 约束会冻结所有子代理的模型：

| 角色 | 默认模型 |
| --- | --- |
| Investigator、Implementer、Curator | sonnet |
| Focused Implementer | sonnet 或 opus，由 Controller 按任务难度选择 |
| Reasoning Specialist、Reviewer | inherit（与 Controller 相同，应为 Opus） |

Claude 子代理本身就不继承父级对话；Thaliris 角色不能由 fork 或通用代理充当。原生身份只按 Host 的精确记录绑定（Agent 结果的 agentId、Host 写入的子代理元数据），不按“唯一待定预约”猜测，缺失或矛盾时保留 UNKNOWN。子代理停止、Agent completed、Core DONE 与产品验收彼此独立；后台子代理完成时，未处置的证据会在 Controller 下一回合开头重新呈现。Reviewer 只读由工具白名单和 PreToolUse 守卫共同保证。详见[适配层说明](docs/claude-adapter.md)。

## 安装与配置

需要 Python 3.11+；不需要 venv、pip 或单独安装 Core。

~~~sh
claude plugin marketplace add Iris0fTheValley/Thaliris-claude
claude plugin install thaliris@thaliris
~~~

启用时会询问三个选项，之后可用 `/plugin configure thaliris@thaliris` 修改：`default_execution_mode`（delegated、controller-direct 或 single-agent；选 delegated 即对 `thaliris:*` 子代理的常驻请求）、`model_selection`（controller 时由 Controller 先定分工，再为每个角色选能胜任的最便宜模型：haiku 用于 Git 收尾与 CI 观察，sonnet 用于清晰的本地工作，opus 用于耦合或不清晰的工作；role-defaults 时使用各角色默认模型）和 `auto_approve_runner`（只批准本插件 Runner 的单条直接调用）。首次会话自动寻找并缓存 Python 运行时。全局 Controller 提示由插件的 SessionStart 注入，不改写用户的 CLAUDE.md，卸载插件即移除。项目在第一个受管任务时由 Controller 运行 `bootstrap` 初始化，不执行 `git init`。

更新用 `claude plugin update thaliris@thaliris`，在新会话生效；用户选项和 `~/.thaliris` 数据跨版本保留。安装、更新或修改用户级配置属于 Host 维护，需要用户另行明确决定，任务授权不包含这项权限；磁盘上的文件变化不代表正在运行的会话已经加载。更多约束、真实 Host 探针结果和历史问题对照见[适配层说明](docs/claude-adapter.md)。
