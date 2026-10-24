# Thaliris-claude 适配层说明

面向维护者的技术细节。项目介绍与安装见 [README](../README.md)。

Claude Code 的 Thaliris Host 适配层。它直接复用共享的 [Thaliris Core](../../core/Thaliris)
（任务账本、Evidence Continuity、Authority 锚点），用 Claude Code 原生机制
（Plugin、Skill、`agents/*.md` 子代理、Hooks）实现薄适配，而不是移植 Codex 的整套
生命周期 / 安装 / Pinned Runtime 系统。

> 原则不变：**Claude Code 负责执行，Thaliris Core 负责机械事实，Controller 负责语义判断。**
> 适配层不维护第二份任务事实库，也不重建 Agent Runtime。

## 组成

```
Thaliris-claude/
├── .claude-plugin/plugin.json     插件清单（插件名 thaliris → 子代理类型 thaliris:<role>）
├── agents/*.md                    6 个角色子代理（由 roles.py 生成，勿手改）
├── skills/thaliris/SKILL.md       Controller 常驻指导（Claude 风格重写）
├── skills/thaliris/recovery.md    例外流程（按需检索：恢复、处置、Host 维护）
├── hooks/hooks.json               SessionStart / UserPromptSubmit / PreToolUse / PostToolUse(+Failure) / SubagentStart / SubagentStop
├── bin/thaliris-hook             Hook 入口（sh，与官方插件同一约定；命中缓存时只启动一个解释器）
├── bin/thaliris.py                Runner 启动器（兼容旧 Python，自动发现并缓存 ≥3.11 运行时）
├── vendor/                        固定提交的 Thaliris Core（e29c837，Core 0.5.1）与 core.lock.json
├── scripts/vendor_core.py         内置/校验 Core
└── src/thaliris_claude/
    ├── roles.py    RoleSpec + ClaudeExecutionBinding；角色提示词唯一权威来源
    ├── store.py    会话↔任务关联、原生身份↔handoff 绑定、Core 加锁事务
    ├── guards.py   Shell/文件写入的机械分类（obvious-write guard，不是沙箱）
    ├── hooks.py    Hook 处理器与全局提示（GLOBAL_GUIDANCE）
    └── cli.py      Runner 子命令
```

## 提示词分层（重写后）

| 层 | 载体 | 内容 |
| --- | --- | --- |
| 全局 | SessionStart `additionalContext`（`hooks.GLOBAL_GUIDANCE`） | 主线程即 Controller；何时加载 skill；Authority/只读/隔离边界；Runner 路径；INDEX 导航 |
| Controller | `skills/thaliris/SKILL.md` | 准入、Authority 与模式、角色选择、决策完备 handoff、Claude 原生的派发与等待、证据连续性、端点与评审、产品验收与关闭、持久知识、因果诊断 |
| 例外流程 | `skills/thaliris/recovery.md` | 处置、Authority 恢复、会话切换、Host 维护——按需 `guidance --section recovery` |
| 子角色 | `agents/<role>.md` | 角色职责 + 共享子代理边界 + （执行者）执行方法 + Scanner 规则 + 等待原则 + 最终报告格式 |
| 子代理运行期 | SubagentStart `additionalContext` | 精确 Runner 命令、所绑定 handoff / candidate / requirement refs，或"尚未绑定"提示 |

"Claude 特色"的具体做法：

- 第二人称、先给角色身份与**理由**（例如为什么子代理看不到父对话），而不是关键词堆叠；
- 用标题与短列表组织，正向陈述要做什么，必要的禁止项附原因；
- 直接使用 Claude 原生机制描述行为：Agent 工具与 `subagent_type`、前台/后台
  （`run_in_background`）与完成通知、`SendMessage` 补充证据、`isolation: "worktree"`、
  子代理"只有最终消息返回父级"；
- 把 Codex 的 `wait_agent` / `fork_turns` / heartbeat 规则收敛为一句可执行原则：先做独立决策工作，
  靠原生通知等待，不轮询、不 sleep，超时/消息不是完成；
- 强调校准的诚实：未验证即 UNKNOWN，PASS / READY / completed / DONE 四者分离（SKILL 中以表格呈现）。

## 保留的约束（对照 Codex）

| 约束 | Claude 实现 |
| --- | --- |
| Fresh child、无父对话历史 | Claude 普通子代理默认独立上下文；Thaliris 角色禁止 `fork` / 通用代理充当角色 |
| Authority 为持久的人类选定意图，子代理不可建立/扩展 | `task-start` 需合同文件；Controller-only 子命令在 PreToolUse 对子代理拒绝；身份参数由 Hook 注入，模型自带即拒绝 |
| 执行模式 delegated / controller-direct / single-agent | 存于 Core Authority；single-agent 拒绝 Thaliris 子代理；`mode` 需人类指令 + CAS |
| 冻结执行约束 | `execution_constraint: "claude-model:<alias>"`，PreToolUse 强制并拒绝冲突覆盖 |
| Reviewer 只读 | frontmatter `disallowedTools` + PreToolUse 拒绝文件写工具与明显写入的 Shell 命令（插件 Agent 的 `permissionMode`/`hooks` 字段会被 Claude 丢弃，已从二进制确认） |
| 子代理不能改 Controller/安全状态 | `.context/`、`.project-identity/`、`.claude/settings*.json`、`~/.thaliris` 写入拒绝；子代理禁止直接调用 `thaliris-core`、禁止加载 Controller skill；Authority 只锚定 Thaliris 自有的 `.context/config.json`（用户的 Claude 设置不锚定，避免合法编辑被当篡改、恢复时被回滚） |
| 委派矩阵与最大深度 2、每父级 1 个 Scanner | PreToolUse 依据 `agent_type`（Host 断言）与 lifecycle 校验 |
| 已派发 child 的身份与权限不随配置漂移 | 预约时快照角色绑定；Hook 对已派发 child 按快照解释，角色文件改动或重新登记只影响之后的派发 |
| Evidence Continuity | Core `evidence_continuity` 原样复用；子代理 `observe` 由适配层补全 handoff/candidate/refs 并以 handoff 前缀命名空间化 ID；Agent 结果送达时（PostToolUse，以及后台完成通知触发的 UserPromptSubmit）重新呈现未处置证据 |
| 原生完成 ≠ 验收；DONE 只是机械关闭 | lifecycle 记录 `completed/stopped`；`close` 只检查执行残留风险（reserved/launched/running 未处置即拒绝）；已结束的 child 不再获得 `observe` 权限，也不能被 `SendMessage` 复用于新工作 |
| 处置不等于终止 | `dispose` 只结束管理并 fence 写入，`native_termination: UNKNOWN` |
| UNKNOWN 优先于猜测 | 只用精确来源绑定：Host 自写的 `subagents/agent-<id>.meta.json`（含 `toolUseId`）或 Agent 结果的 `agentId`；从不按“同角色唯一待定预约”猜身份 |
| 优雅降级 | Hook 异常或运行时缺失/损坏时普通工作透明放行；只拒绝会因此失去守卫的动作（关联任务中的 Thaliris 派发、Reviewer 文件写入）；SessionStart 告知 managed 控制为 UNKNOWN |

不移植的部分：Codex 的 pinned runtime / release manager / profile 兼容性 / 安装器 / Verifier
（仅为兼容旧 Codex 安装存在，新 Host 没有需要兼容的旧 profile）。

## 安装、配置与更新

唯一前提：本机有任意 Python ≥ 3.11（python.org、`py` 启动器或 `uv python install 3.11` 均可）。
Thaliris Core 已按固定提交内置在 `vendor/`（只用标准库），不需要 venv、pip 或联网安装。

```bash
claude plugin marketplace add Iris0fTheValley/Thaliris-claude
```

```bash
claude plugin install thaliris@thaliris --config default_execution_mode=delegated
```

在交互式 Claude Code 中用 `/plugin install thaliris@thaliris` 时，启用时会询问三个选项（之后可用
`/plugin configure thaliris@thaliris` 修改）：

| 选项 | 默认 | 作用 |
| --- | --- | --- |
| `default_execution_mode` | `delegated` | 受管任务的常驻执行模式。选 `delegated` 即用户对 `thaliris:*` 子代理的常驻请求（见下文 Pro 计划限制） |
| `auto_approve_runner` | 开 | Hook 只批准“单条直接调用本插件 Runner”的命令，避免受管流程每步停下来问权限；plan 模式下只批准只读子命令；串接其他命令一律拒绝 |
| `model_selection` | `controller` | `controller`：Controller 为每个角色按需选择模型：先定分工与认知隔离，再选能胜任的最便宜模型与 effort（`haiku`/low 用于 Git 收尾、CI 观察与确定性本地工作，`sonnet` 用于因果清晰的本地工作，`opus` 用于耦合或不清晰的工作；设计如此，Hook 不覆盖也不拒绝；只有用户写入任务合同的 `claude-model:` 约束会冻结模型）。`role-defaults`：使用各角色默认模型 |

无需其他步骤：

- **首次会话**：SessionStart 自动寻找 Python ≥ 3.11 并缓存到 `~/.thaliris/claude/runtime/python`，
  注入全局 Controller 提示（含已加载的版本与路径、用户的常驻模式）。找不到运行时就说明如何修复，
  普通工作不受影响。
- **项目初始化**：按需。Controller 在第一个受管任务时运行 `bootstrap`；从不 `git init`；在含多个子仓的
  父目录会要求 `--root`。
- **全局提示词注册**：通过插件自身的 SessionStart、`agents/*.md` 和 skill，不改写用户的
  `~/.claude/CLAUDE.md`；卸载插件即全部移除。
- **更新**：`claude plugin update thaliris@thaliris`（或 marketplace 自动更新），在新会话生效。
  GitHub 来源安装到带版本号的缓存目录（已实测：`plugins/cache/thaliris/thaliris/0.2.0`，Hook 脚本为 LF、
  内置 Core 与锁一致、首次 Hook 自动发现系统 Python 3.11），进行中的会话仍用旧版本文件。Windows 上若把
  `CLAUDE_CONFIG_DIR` 放在很深的目录，Git 克隆 marketplace 可能超出 260 字符路径上限；默认的 `~/.claude` 不受影响。
  用户选项与 `~/.thaliris` 数据跨版本保留；
  lifecycle 文件带版本号，读到不认识的版本时报错而不是误读（此时 child 写与消息 fail closed，见“升级与会话连续性”）。内置 Core 随插件版本一起更新，
  `scripts/vendor_core.py --check --source <Core 仓库>` 校验内置字节与固定提交一致。

开发时临时加载（不改用户配置）：

```bash
claude --plugin-dir /path/to/Thaliris-claude
```

改动提示词后重新生成角色文件；更换 Core 固定版本时重新内置：

```bash
python bin/thaliris.py render-agents
```

```bash
python scripts/vendor_core.py --source ../../core/Thaliris --commit <Core 提交>
```

## Claude 官方提示词中与 Thaliris 相关的部分（Claude Code 2.1.302）

| 官方内容 | 与 Thaliris 的关系 | 处置 |
| --- | --- | --- |
| Agent 工具：“Agents run in the background by default… do NOT sleep, poll, or proactively check on its progress” | **一致**：事件驱动等待、不轮询正是 Thaliris 等待原则 | 不改。SKILL 与之对齐（后台确认不是结果，等待通知） |
| 静默回合提醒 “The user hasn't heard from you in a while…”、耗时提醒 | 类似 Codex 的 60 秒沟通要求，但只在主线程**正在工作时**提示给用户更新，不唤醒模型、不作用于子代理；等待后台子代理时不会触发 | 不改（沿用 H108/H150 结论：沟通频率 ≠ 轮询子代理）。官方开关 `CLAUDE_CODE_SILENT_TURN_REMINDER*` 可用但不需要 |
| **仅 Pro 计划**：Agent 工具 “Do not spawn agents unless the user asks… Only use this tool when the user explicitly says to use a subagent” | **冲突**：Thaliris 默认委派 | 无设置可删。按该规则自身的出口处理：插件 `userConfig.default_execution_mode` 由用户在 Claude Code 配置中选择，SessionStart 如实告知“这是用户的常驻请求”；未设置时 Controller 在准入时询问一次，而不是假定委派。实机：Sonnet 在用户未提及子代理时完成了 bootstrap→task-start→派发 Implementer→close |

不采用的做法：修改二进制、伪造“用户同意”的 Hook 文本、强制 output style 覆盖用户自己的风格、写入用户
`~/.claude/CLAUDE.md`（属于 Host 维护，需单独授权）。

## Runner 子命令

### 输入形式、输出与错误（0.4.1）

- **参数形式**：`task-start`（`--human-instruction --boundary --invariants --acceptance --execution-mode [--execution-constraint]`，
  与 `--contract <文件>` 二选一）、`register`（`--requirement ID=REF`，可重复）、`judge`（`--requirement --candidate --outcome
  --observations --reasoning [--id] [--judgment-refs]`；批量用重复的 `--judgment "REQ|OUTCOME|OBS[,OBS]|REASONING"` 配一个
  `--candidate`，原子、一次 revision）、`observe`（`--text` 与必填的 `--source LOCATOR`，可重复；`--id` 可选）、`task-update`
  （`--record [ID=]KIND:TEXT`、`--supersedes`、`--milestone`、`--active-work`、`--pending`、`--clear-*`）。参数形式与 `--json` /
  `--input`（UTF-8、UTF-8 BOM、UTF-16 BOM，由 Core `decode_input_bytes` 解码）走同一条校验路径，不可混用；`--json` 也接受 Core 的批量形式
  `{"judgments":[...]}`、`{"observations":[...]}`（1–64 个，原子，一次 revision）。
- **一条直接命令**：Hook 只识别单条直接调用；管道、`&&`、`;`、重定向、尾随 `#` 注释、多余换行、未加引号的反斜杠路径都会使身份无法追加，
  此时 Hook 以原因和正确命令拒绝，Runner 则报告 `NATIVE_IDENTITY_UNKNOWN`。分类按工具区分方言：PowerShell 没有反斜杠转义，POSIX 读法与它不一致的写法
  一律不算直接命令——引号前的反斜杠、未加引号的 `\;` `\|` 等、会成为引号定界符的弯引号、`--%`——对非 Runner 命令同样按“无法判断，视为写入”处理，
  所以只读子代理不能借此在第二条 PowerShell 语句里写文件。双引号内的 `$name`、`${...}`、`$?`（PowerShell 另含 `$$`、`$^`）会被 shell 悄悄替换，
  因此也被拒绝并提示改用单引号；单引号内的 `$` 保持字面。
- **观察引用**：子代理自选的 id（如 `O1`）由适配层加 handoff 前缀存入 Core（`H1a2b3c4d.O1`），因此并行兄弟都叫 `O1` 也不会冲突；参数形式的
  `judge --observations O1` 只在“同一 candidate、且观察若限定了 requirement 则包含被判定的 requirement”的观察里找唯一匹配的限定 id，零个或多个
  匹配都保持裸 id，由 Core 拒绝并给出建议；解析结果以 `observation_refs`（按 judgment id）回显在响应里；`--json` 必须使用限定 id。
- **输出**：紧凑 JSON（无缩进，UTF-8）。写入类子命令（`task-update`、`register`、`judge`、`observe`、`artifact`、`close`）返回 Core 的紧凑确认：
  `ok task_id revision status changed [appended] evidence`，其中 `evidence` 是开放证据的总数加有界条目（`next_offset` 翻页，Core
  `evidence_summary`），不再回显完整 Task Context / Evidence Continuity；`appended` 带回自动分配的记录 / 判断 / 观察 id。子代理的 `observe`
  只拿到 `evidence` 的总数（条目由 Controller 呈现）。`status` 默认为紧凑视图：Core 紧凑包（任务头、计数、开放证据）加 Authority
  （mode、status、sha256，另有冻结的 `execution_constraint` 时一并显示）、`Dispatches`（按 `native_status` 计数，加每个未终止派发一条紧凑条目）、
  `Records`（总数与最近 12 条、每条 ≤200 字符）；`status --view full` 与此前的完整输出一致；`--evidence-offset` 翻页，`--since-revision N`
  追加 Core 的变化提示（`Changes Since`）。
- **错误**：Runner 对 Core 的 `ThalirisError` 输出 Core 的 `error_envelope`（`ok:false, error, code, recovery, details`）；适配层的
  `CODE: 说明` 错误也带 `code` 字段（`error` 文本不变）。revision 不符一律是 `TASK_REVISION_CONFLICT`（适配层原有的两处自造冲突已改用
  `core.check_revision`），`details` 含 `current_revision` 与 `changes_since`（有界、非精确的变化提示和开放证据：`exact:false`；`recent` 是每类最近 k 条，同一
  revision 里批量写入的条目可能漏掉；被整体替换的字段 Active Work、Pending Results、milestone、status 以及 sources 不被追踪）。恢复流程：查看
  `changes_since`（含其开放证据摘要），确认写入仍适用后用 `details.current_revision` 重试一次；需要 Active Work、Pending Results 或提示未覆盖的内容时再读
  `status`（紧凑视图）。严格 CAS 不变，不做自动 rebase。
- **一次锁内完成**：此前 `task-update`（以及 `promote`、`document-maintain`、`artifact`、身份变更）先在一把锁内检查 Authority，释放后 Core 再在第二把锁内
  写状态，写完才（锁外）重新锚定 Authority。并行派发时，另一个 Hook 可能在“状态已写、锚点未更新”的窗口里读到
  `TASK_AUTHORITY_STATE_CHANGED`；检查与写入之间状态被改，也会被随后的重新锚定一并认可。现在由 `store.critical()` 在同一次持锁期间完成
  Authority 检查、Core 写入与重新锚定（Core 的锁对同一根目录变为无操作，Core 自己的校验与 CAS 不变）；`mode` 的“无未决派发”检查也移入同一把锁
  （`test_authority_checkpoint_happens_inside_the_lock`）。
- **证据包去重**：证据包摘要与 `UserPromptSubmit` 共用不含 revision 的摘要。后台启动确认（`async_launched`）若摘要未变，只给一行
  “Evidence unchanged: N unincorporated, M unresolved, task revision now R.”；子代理最终报告（前台完成）总是带完整包
  （`test_parallel_background_launches_present_the_packet_once`）。

### 动态 Task Context（`task-update`）

Controller 用 `task-update --base-revision <n>`（参数形式，或 `--json`）维护任务的全局语义状态，直接映射 Core
`task_update`（actor 为 `controller`，revision CAS，写后重新锚定 Authority）：`records` 追加事实、决策、进展、
未决事项（`kind` 自定；更正用 `supersedes` 追加新记录而不改写历史；`producer`/`revision` 由 Runner 提供，
载荷自带即拒绝），`active_work` / `pending_results` 整体替换（Core 限 64 条、每条 ≤4096 字符及总字节上限），
`current_milestone` ≤128 字符，`evidence_refs` 追加来源。子代理调用在 PreToolUse 与 Runner 两层均被拒绝。

读出：`status`（紧凑）给出 Active Work / Pending Results / milestone 以及最近 12 条未被取代的记录（`Records`，每条 ≤200 字符）与总数；
`status --view full` 的 `Current Records` / `Current Record Count` 保持原样（文本 ≤600 字符）；SessionStart（startup/resume/compact/clear 且会话已关联任务）注入有界摘要
（≤6000 字符：里程碑、Active Work、Pending Results、最近记录）；新会话 `attach` 返回同一摘要。摘要是
Controller 记录的历史，不是新的验证。

### Controller 定义的角色（`register-role` / `unregister-role`）

`register-role` 写入项目原生 agent `.claude/agents/thaliris-<name>.md`（带与内置角色相同的子代理契约），并在受保护的
`.context/claude-roles.json` 记录其摘要。可配置字段：`description`、`instructions`（新角色必填）、`model`（别名、
完整模型 ID 或 `inherit`）、`effort`、`tools`、`disallowedTools`、`writes`、`scanner`、`color`、`maxTurns`、`skills`
（不可预载 `thaliris`）、`memory`（仅 `project`/`local`，且须 `writes: true`）、`background`、`isolation`（仅
`worktree`）。`permissionMode`、`mcpServers`、`hooks`、`initialPrompt` 等可能扩大边界或绕过 Hook 的字段一律拒绝。
`writes: false` 时写工具、`scanner: false` 时 `Agent` 始终进入 `disallowedTools`，Controller 自己的列表只能追加。
每个值都校验为单个安全的 frontmatter 记号或 JSON 字符串，不能借值注入额外字段。

- **修改**：同名再次 `register-role` 时只给要改的字段即与已登记记录合并（`null` 重置为默认）；`"replace": true`
  则从头定义。`unregister-role <name>` 移除登记。
- **保留用户字节**：目标文件已存在且不是登记表以当前摘要写下的字节（用户文件或被手改）时，`register-role`
  拒绝（`ROLE_AGENT_FILE_NOT_OWNED`）且不改动文件；`unregister-role` 只删除摘要一致的文件，否则保留并说明。
- **何时可用**：新会话一定可用。Host 文档（Claude Code sub-agents）说明 agent 目录被监视，新增或修改的 agent 文件在
  下一次委派即生效，但会话启动时不存在的目录除外。实测（2.1.302，Windows，`claude -p`，haiku，临时仓库，单次调用以
  `--settings` 禁用本插件）：启动前已放好的 agent 文件可派发；启动时 `.claude/agents/` 已存在、会话中新建 agent 文件，
  即使等待 12 秒后派发仍报 “Agent type not found”；目录不存在时同样找不到。交互式会话是否热加载仍未验证。因此
  `bootstrap` 与 `task-start` 仍预先创建该目录（满足文档条件、无副作用），SessionStart（startup/resume）记录本进程
  启动时目录是否存在，`register-role` 回报 `next session`（确定）以及本会话是“可能”（文档条件满足但未实测成立）、
  不可用还是 UNKNOWN，不承诺同会话可用。
- Hook 只把摘要一致的注册角色当作**新派发**的 Thaliris 角色（只读、委派、证据绑定同内置角色）；文件被改动即对新派发
  失效为普通代理，子代理写文件不能自造受管角色。

### 项目身份（`identity-*`）

Core 0.5.0 的 Project Identity 平面（`src/thaliris/identity.py`，存于仓库内、随 Git 走的 `.project-identity/`）。身份是
跨任务的项目级声明，不是 Task Authority、记忆或里程碑；分层只是 Controller 的推理模型，不是文件、权限或阶段。

- **只读**（`identity-status`、`identity-get [--version N] [--item ID ...] [--amendment A-000N] [--challenge ID]`、
  `identity-trace REF [--direction up|down|both] [--depth 1..4]`）：归入 `READ_ONLY_COMMANDS`（plan 模式也批准、子代理可用），
  不需要任务，与 `catalog` 一样按会话关联根或 `--root`/cwd 解析项目。身份不存在时 `identity-status` 返回 `state: ABSENT`，
  不报错。`UNRECORDED_CHANGE`（`IDENTITY.md` 与记录版本不一致）与 `INVALID`（链/快照不一致）是观察结果；读取仍服务记录版本，
  `INVALID` 时读取拒绝。
- **变更**（`identity-amend`、`identity-challenge`、`identity-relate`，`--base-revision <n>` 与 `--input <文件>`，亦接受 `--json`）：
  归入 `CONTROLLER_COMMANDS`；PreToolUse 对子代理拒绝，Runner 再次以 `CONTROLLER_ONLY` 拒绝。要求会话已关联 ACTIVE 任务，
  Core 在任务锁内以 `--base-revision` 做 CAS、记录 task_id 与 actor（`claude:controller`）、**不**递增任务 revision，成功后与
  `task-update` 一样重新锚定 Authority。人类决定（`decision.decided_by`、`decision.human_instruction`）由 Controller 在载荷中断言，
  Core 无法验证真人，与 Task Authority 的 `human_instruction` 同构。它不是 `promote` / `document-maintain`。
- **子代理写保护**：`guards._PROTECTED_MARKERS` 与 `protected_target(controller=False)` 把 `.project-identity/` 加入子代理的写拒绝列表。
  该列表与 Task Authority 的 `protected_paths`（`store.PROTECTED_PATHS`，只有 Thaliris 自有配置）是两份独立的列表：身份**没有**加入后者，
  因此任务内的合法 amendment 不会触发 `TASK_AUTHORITY_SECURITY_CHANGED`。Controller 不被拦截（它通过 Runner 修改；绕过 Runner 的手改会被
  Core 观察为 `UNRECORDED_CHANGE`/`INVALID`，再加 Git diff）。这与其他 obvious-write guard 一样不是沙箱：解释器等未知写入不在声明范围内。
- **投递**：SessionStart 对所有来源（startup/resume/compact/clear），无论是否关联任务，仅在身份存在时追加 Core `identity_summary` 的一行
  （不存在则什么都不加）；`attach` 在存在时于 `Project Identity` 键下返回同一行。`GLOBAL_GUIDANCE` 的导航项提示按需用 `identity-status` /
  `identity-get` 读取。身份文本从不注入子代理（SubagentStart 不变）；Controller 只在 handoff 中摘录真正约束该子代理工作的条目。
- **自然形成（0.4.2，Core 0.5.2 `identity_basis`）**：`identity-note`（`--quote ... [--kind purpose|direction|constraint|non-goal|decision|rejection|material|other]
  [--context ...] [--ref ...]`；`--retract N-0001 --reason ...`；`--list [--include-retracted]`）记录用户明确陈述的原话（存于 `THALIRIS_HOME`
  或 `~/.thaliris`，仓库外，不新增被跟踪文件）；`identity-suggest --on|--off|--clear --scope session|workspace|project|global` 设置收尾提议偏好。
  二者归入 `CONTROLLER_COMMANDS`（子代理被 PreToolUse 与 Runner 拒绝），**不需要** ACTIVE 任务（只需 Controller 身份）。会话键即 Hook 追加的
  `--native-session`，传给 Core 作 `session_key`（session 范围偏好与 `identity-status` 的 `suggestions`）。`identity-status` 另含 `basis` 与
  `suggestions`。`close` 成功提交后调用 `identity_offer`，有则在结果中加 `identity_offer` 字段（同一批笔记只出现一次；偏好关闭、身份 INVALID/
  UNRECORDED_CHANGE 或失败时字段缺省）。SessionStart/bootstrap 不输出任何提议。
- **指导**：`SKILL.md` 第 10 节“Project identity”（准入第 2 步一句话指向它）。

### 输出编码（Windows）

重定向或管道下的 Windows `sys.stdout` 默认是 cp1252，写中文会抛 `UnicodeEncodeError`。Hook 在写出前崩溃时，进程非零退出且没有输出，
`--native-session` 不会被追加，Runner 随后报告 `NATIVE_IDENTITY_UNKNOWN`；`PYTHONUTF8=1` 可规避但不能依赖。当前处理：Hook 输出改为纯 ASCII JSON
（`ensure_ascii=True`，任何解码器都能读）；Runner 输出保持可读的中文，以 UTF-8 字节写入 `sys.stdout.buffer`。回归测试在
`PYTHONIOENCODING=cp1252` 的子进程里分别跑 Hook 与 Runner（`tests/test_install.py`）。

### 执行参数：effort

受管任务中，Thaliris 派发的 `effort` 只接受 `low|medium|high|xhigh|max`（其他值 PreToolUse 拒绝），lifecycle 记录
`requested_effort`，角色默认值（注册角色的 frontmatter `effort`）记在派发快照的 `default_effort`，`status` 均可见。
Host 的有效 effort（环境变量 `CLAUDE_CODE_EFFORT_LEVEL` 优先于两者）不在 Host 记录中，保持 UNKNOWN。内置角色不设默认
effort。effort 不改变职责或权限。

### 升级与会话连续性（0.4.3）

目标：合法的升级、Host 维护、会话恢复和执行中的合法操作，不使既有会话、原 Controller 或原 Task 失效；局部故障不扩大成全局停摆；
原有拒绝（身份、turn、授权、终态）不放宽，UNKNOWN 不当作完成或安全。

| 情形 | 行为 | 测试 |
| --- | --- | --- |
| 会话关联的任务状态不可读（schema 版本不兼容、JSON 损坏等；状态文件缺失或任务已 DONE 仍是“无任务”） | `store.inspect_session` 区分“无任务”与“任务存在但不可读”：SessionStart 报告任务 ID、code 与 recovery；`status` 等 Runner 命令给出真实诊断（`STATE_SCHEMA_INCOMPATIBLE` 带 Core 的 `from_version`/`to_version`/`recovery_action`），不再是 `NO_ACTIVE_TASK_FOR_SESSION`；受管 `thaliris:*` 派发 fail closed（拒绝、不预约、不静默非受管）；通用代理与 Controller 自己的读写不受影响。不可读期间 child 的 SubagentStart/Stop 不记录（状态 UNKNOWN） | `test_incompatible_state_schema_is_reported_and_dispatch_fails_closed`、`test_damaged_state_json_is_unreadable_not_absent`、`test_missing_or_closed_state_is_still_no_task` |
| 无任务的普通路径 | 只读会话关联文件即决定；不读状态、lifecycle，不起子进程；读文件与子进程次数与 0.4.2 逐项相同（对 0.4.2 做了计数对照） | `test_no_task_path_reads_no_task_state_and_starts_no_process` |
| 已结束（`completed`/`stopped`）或已处置的 child 再次 SubagentStart（如 `SendMessage` 恢复） | 状态不复位为 `running`，只记事件 `resumed-after-terminal` / `started-after-dispose`（连续重复不重复记录）；仍无 `observe`；给该 child 的 SubagentStart 上下文说明不再有授权。新工作给新 child | `test_second_start_does_not_reactivate_a_finished_child`、`test_completed_tool_result_is_terminal_too`、`test_start_after_dispose_keeps_the_fence` |
| 写权限与终态一致 | 终态 child 的 Write/Edit 与明显写入的 Shell 命令像已处置一样被围栏（“已交付最终报告；新工作需新派发”）；运行中（含 `launched` 的后台 child）不受影响，Controller 主线程不受 child 状态影响 | `test_running_child_writes_until_it_finishes_then_is_fenced`、`test_background_launch_acknowledgement_does_not_fence_a_running_child` |
| lifecycle 不可读（`CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED`、`CLAUDE_LIFECYCLE_INVALID`） | Thaliris 类型 child 的写与所有 `SendMessage` fail closed，拒绝文案带 code 与 recovery；受管派发照旧拒绝；Controller 主线程、通用子代理、只读角色不受影响；child 的 Runner 调用仍带身份到达 Runner，Runner 报告真实 code，而不是 `NATIVE_IDENTITY_UNKNOWN` | `test_unreadable_lifecycle_keeps_a_disposed_child_fenced`、`test_unreadable_lifecycle_does_not_stop_readonly_roles_or_the_controller` |
| lifecycle 条目缺必填键或字段不可读 | 读取时即报带 code 的 `CLAUDE_LIFECYCLE_INVALID`（含 recovery），不再是裸 `KeyError`；SessionStart 报告而不崩溃。必填键是 0.1 起就有的八个键，`role_binding`/`requested_effort` 等后加字段仍是可选 | `test_damaged_lifecycle_entry_is_coded_and_session_start_survives`、`test_version_01_lifecycle_entries_read_and_dispatch` |
| 局部 child 冲突 | 已处置、已结束、身份冲突的 child 不阻塞 Controller 的下一次派发，也不使整任务拒绝 | `test_local_child_conflicts_do_not_widen_into_a_task_wide_refusal` |

旧会话继续使用其已加载的版本：Hook 由 `hooks.json` 经 `${CLAUDE_PLUGIN_ROOT}` 指向该版本目录，Runner 路径与提示词中给模型的路径同属一版；
`guards.runner_misses` 对“另一已安装版本的 Runner 路径”给出准确拒绝与应使用的命令。`/reload-plugins` 或新会话后使用新路径。
Host 限制：若 Host 清理了旧版本缓存目录，仍加载它的会话里 `sh` 找不到 Hook 脚本（通常退出码 127，Host 视为非阻塞错误），
插件无法自行处理，此时守卫不在运行，managed 保证为 UNKNOWN；这不是任务损坏，重载插件或新开会话后用 `attach --task-id` 接回原 Task
（Task 与 Authority 保留）。处置步骤见 `skills/thaliris/recovery.md`。

### 已派发角色的身份连续性

PreToolUse 预约时在 lifecycle 条目写入 `role_binding` 快照：角色、`subagent_type`、来源（builtin/registered）、
`writes`、可委派目标、默认 model/effort、渲染出的 `tools`/`disallowedTools`、注册角色的 agent 文件摘要。此后 Hook
按快照而非当前登记表解释该 child：已绑定（Host `meta.json` 或 Agent 结果）的 child 用自己条目的快照；尚未绑定但类型与
未结束预约一致的 child 只获得这些快照中**最严格**的权限（不是身份认定，不绑定证据）。因此修改、重新登记或移除角色
只影响之后的派发；`writes: false` 的 child 在文件被改后仍不能写；SubagentStart/SubagentStop 仍识别它，`stopped`
照常记录，`close` 不被残留预约阻塞。新派发仍要求登记摘要一致。快照是 lifecycle 条目的可选新字段，
`LIFECYCLE_VERSION` 保持 1：旧条目没有快照时回退到按登记表解析（旧行为），旧版适配层读新文件时忽略未知字段。

| 谁 | 子命令 |
| --- | --- |
| 任何人（只读） | `guidance [--section recovery]` `status` `get` `catalog` `document-get` `version` `roles` `identity-status` `identity-get` `identity-trace` |
| Controller | `bootstrap` `task-start` `attach` `detach` `task-update` `register` `judge` `mode` `dispose` `artifact` `close` `recover-authority` `register-role` `unregister-role` `identity-amend` `identity-challenge` `identity-relate` |
| 子代理 | `observe`（Controller 用 `--handoff-id` 导入历史结果） |
| Controller / Curator | `promote` `document-maintain` |

## 真实 Host 探针结果（Claude Code 2.1.302，Windows，2026-10-22）

方法：一次性 Git 仓库、`claude -p --plugin-dir`、主线程和全部子代理均为 haiku（由任务的
`claude-model:haiku` 约束经 `updatedInput` 强制），用 `THALIRIS_CLAUDE_PROBE_LOG` 记录原始 Hook
载荷。三轮共约 $0.04。

| # | 问题 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | 插件子代理 `agent_type` 是否带命名空间 | **已确认**：`thaliris:investigator` 等 | 所有子代理事件的 `agent_type` |
| 2 | 子代理 Hook 的 `session_id` 是否同主线程 | **已确认**：一致；另有 `prompt_id`、`scratchpad_dir` | 22 条事件同一 `session_id` |
| 3 | 无 `permissionDecision` 时 `updatedInput` 是否生效 | **已确认**（Agent 与 Bash） | 子代理收到 `thaliris-handoff:` 行；`resolvedModel` 为 haiku（investigator 默认 sonnet）；子代理 `status`/`observe` 带身份成功 |
| 4 | 事件顺序 | **已观察**，见下 | — |
| 5 | Windows `python` 解析与开销 | **可用**。Bash/Edit/Write 约占一次会话工具调用的 84%，因此 `sh` 入口先做无 Python 的预筛：与 Thaliris 无关的调用约 0.16 s，涉及 Thaliris 的约 0.42 s，无任务时每回合 Hook 约 0.12 s | 预筛只放过 Python 守卫不会作出决定的调用，由真实 `sh` 脚本的等价测试保证 |
| 6 | 后台完成是否经过 PostToolUse | **不经过**；以 `<task-notification>` 新回合到达，**会触发 UserPromptSubmit** | 已改为在 UserPromptSubmit 重新呈现证据包，第二轮探针中 Controller 逐字收到 |

观察到的顺序：

- 主线程派发：PreToolUse → SubagentStart →（若被后台化）PostToolUse `async_launched` → … →
  SubagentStop → 主线程新回合 UserPromptSubmit（`<task-notification>`）。
- 前台嵌套 Scanner：PreToolUse → SubagentStart → SubagentStop → PostToolUse `completed`。
- `SendMessage` 恢复：同一 `agent_id` 再次 SubagentStart → SubagentStop；lifecycle 记为
  `launched → running → stopped → running → stopped`（0.4.3 起，已结束的 child 再次 SubagentStart 不再复位为 `running`，
  只记 `resumed-after-terminal` 事件）。

探针带来的修正：

- **Host 会自动后台化未请求后台的 Agent 调用**（并行派发时观察到）。PostToolUse 现在依据
  `async_launched` 记录真实的 `background`；SKILL 不再承诺前台调用必定直接返回结果。
- **新增 UserPromptSubmit Hook**：未处置证据包变化时（或压缩/恢复后）在 Controller 回合开头注入，
  覆盖后台完成通知；未变化的包不重复注入。
- **SendMessage 守卫**：已结束或已 `dispose` 的 Thaliris child 不能被消息复活（第三轮探针实测拒绝）。
- **精确绑定**：SubagentStart 时 Host 尚未写 `meta.json`；child 第一次调用 Runner 时已存在，
  第三轮探针在此精确绑定并成功 `observe`。实际工具集 `prefix.json` 在 SubagentStop 之后才写，
  因此在 Agent 结果或下一个 Controller 回合采集。
- 新增 `THALIRIS_CLAUDE_PROBE_LOG`（可选，记录原始载荷，含 prompt，仅用于一次性仓库）。

仍未验证：交互式（非 `-p`）会话与 Desktop 下的同样行为；`isolation: "worktree"` 子代理的 `cwd`
与会话关联；PowerShell 工具下的 Runner 识别；Reviewer 若拥有 Write 工具时文件守卫（本次
`disallowedTools` 已使其不可见，Hook 层仅有单元测试覆盖）。

## 避免重演的历史问题

依据《Thaliris：来时路——问题与决策全史》（2026-10-22）逐条对照。下列是当前实现若不处理就会
重演的条目及处置；括号内为回归测试或实机证据。

| 历史 | 若不处理会怎样重演 | 当前处置 |
| --- | --- | --- |
| H144、H030 | SubagentStart 按“同角色唯一待定预约”绑定，正是被否定的猜身份 | 删除；只用 Host `meta.json` 的 `toolUseId` 或 Agent 结果 `agentId`；类型不符记 `conflict`（`test_same_role_children_are_never_bound_by_guessing`、`test_host_record_type_mismatch_is_a_conflict`；探针 3） |
| H023 | argparse 缩写 `--native-s` 与重复参数“后值覆盖”可移动身份 | `allow_abbrev=False`，身份参数重复即拒绝（`test_runner_rejects_abbreviated_or_repeated_identity`） |
| H092、H113、H145 | `SendMessage` 可恢复已交付 FINAL 的 child，继续获得 `observe` 权限做新工作 | 已结束或已处置的 child 拒绝消息；终态 dispatch 拒绝 `observe`（`test_finished_child_is_not_reused`；探针 3 实测拒绝） |
| H143、H178 | child 自动加载 Controller skill，接触全局路由；Focused 提示词指定后继角色 | child 加载 `thaliris` skill 被拒，skill 描述限定主线程；Focused 只列 remaining work（`test_children_cannot_load_the_controller_skill`、`test_children_do_not_choose_successors`） |
| H138、H139 | 在含多个子仓的父目录 `bootstrap` 会把父目录当受管项目、造假 marker | 非仓库目录下发现嵌套仓库即 `AMBIGUOUS_PROJECT_ROOT`，要求 `--root`；Hook 依赖显式会话关联而非 cwd（`test_nested_repository_root_is_never_guessed`） |
| H094、H098 | 锚定用户的 `.claude/settings.json`：合法编辑被判篡改，恢复会回滚用户字节 | 只锚定 `.context/config.json`；child 写设置仍被拒（`test_user_settings_edit_does_not_trip_authority`） |
| H055、H057、H097 | 运行时损坏时 Hook 崩溃，受管派发静默放行，或反过来锁死一切 | 启动器捕获失败：普通工作透明；仅拒绝受管派发与 Reviewer 写（`test_broken_runtime_refuses_only_unguardable_actions`） |
| H132、证据噪声 | 每回合重复注入同一证据包，或压缩后看不到 | 只在变化/压缩/恢复后注入；子代理最终报告送达时总是呈现，后台启动确认摘要未变则只给一行；写入与 `close` 的输出附带紧凑证据摘要（`test_evidence_reaches_notification_turns_without_repeating`、`test_parallel_background_launches_present_the_packet_once`） |
| H027、H111 | 只记录请求的模型，把配置当运行事实 | 记录 `requested_model`/`requested_effort` 与 Host 观测的 `observed_model`/`observed_tools`（`test_actual_tool_set_is_collected_after_the_child_stops`、`test_effort_is_validated_and_recorded`）；有效 effort 无 Host 记录，保持 UNKNOWN |
| H059 | `--contract` 被传入 JSON 文本时报错难懂 | 明确 `CONTRACT_FILE_PATH_REQUIRED` |
| H084 | 测试断言提示词措辞，改文案就炸 CI | 测试只断言协议性标记（命令、marker、报告标题、agent 类型）与生成同步 |
| H096、H012 | 源码已改，但不知道 Host 实际加载的是哪份 | SessionStart 与 `version` 报告加载的适配层版本、插件路径、Python、Core 版本 |
| H124、H156、H160 | 共享 helper 正确 ≠ 所有消费者路径；修一个反例 ≠ 修好机制；过宽的 `satisfied` | 提示词层面：执行者报告已覆盖/未覆盖的生产者与消费者路径；Reviewer 在 READY 前检查机制与迟到事件；Controller 未触及的路径判 `unknown`。不新增机制（H152） |

已对照、当前实现不会重演的：H019/H087（无任务时透明、UNKNOWN 不是违规）、H024/H103（Thaliris 角色不可 fork）、
H029/H180（Stop 不当完成）、H045（无进程内状态）、H085（任务按会话关联，旧 ACTIVE 不锁新任务）、
H086/H089（Controller 可读与诊断，`task-status` 等只读调用放行）、H088（Authority 损坏只限受管控制，有测试）、
H090（独立 Workstream 可并行）、H091（`dispose`）、H112（spawn 报错只记录，不推定无子代理）、
H133（嵌套 Scanner 不继承需求引用）、H134/H135（`observed_state` 与 `judgment_refs` 由 Core 保留）、
H136/H137（Curator 不做实时证据、Reviewer 非必经关卡）、H147（事件保留最近 16 条而非最早）。

仍开放：Desktop 与交互式会话下同样行为；worktree 隔离子代理的会话关联；Host 记录格式
（`meta.json`/`prefix.json`）未文档化，版本变化时退化为 UNKNOWN 而不会误绑。
