# Thaliris recovery and Host maintenance (retrieve on demand)

These procedures are exceptional. Retrieving them grants no authority; each step
still needs the Controller position and, where noted, an actual user decision.

## Reading the situation first

`<runner> status` reports the task, evidence continuity, recent Claude dispatches
and the authority anchor (`status`, mode, constraint, `sha256`). Distinguish:

- **UNKNOWN** — a Host field or event is missing (no `agent_id`, no SubagentStart,
  an opaque tool result). Keep it UNKNOWN; it is not a violation and never a PASS.
- **Identity conflict** — two native ids claim one dispatch, or one id claims two.
  The lifecycle records `identity-conflict`; the affected child gets no managed
  privileges. Dispose the dispatch and start fresh work.
- **Task unreadable** — SessionStart or `status` names the task and a code
  (`STATE_SCHEMA_INCOMPATIBLE`, `TASK_STATE_INVALID`, ...) instead of
  `NO_ACTIVE_TASK_FOR_SESSION`. The task exists; this adapter cannot read it. That is
  not "no task" and not safe: see *Incompatible or invalid task state*.
- **Lifecycle unreadable** — `CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED` or
  `CLAUDE_LIFECYCLE_INVALID`: see *Unreadable lifecycle*.
- **Authority conflict** — `TASK_AUTHORITY_STATE_CHANGED` or
  `TASK_AUTHORITY_SECURITY_CHANGED`: the ledger or Thaliris' own
  `.context/config.json` changed outside an anchored operation. User-owned Claude
  settings are deliberately not anchored, so a person editing them never trips
  this, and recovery never reverts their bytes.

Damaged authority blocks only the controls that depend on it (admission, dispatch
reservation, judgments, closure). Diagnosis and independent work continue inside
role boundaries; children's duties never move onto you because management broke.

## Child that never returns, or a stale reservation

A reservation can stay `reserved` when the user declined the Agent call, or
`launched`/`running` when a background child was lost. Native termination and your
disposition are different facts:

```
<runner> dispose --handoff-id <id> --reason "<your actual decision>"
```

(use `--tool-use-id` for dispatches without a handoff). This ends management and
fences the child's writes and observations; it does not claim the child stopped.
Isolate or coordinate anything it might still write before new work. A late result
cannot reactivate a disposed handoff.

## Authority conflict recovery

Only after you understand what changed (and, if a human edited a protected file on
purpose, after the user confirms how to proceed):

```
<runner> recover-authority --expected-authority-sha256 <exact sha256 from status> --reason "<reason>"
```

It archives the current bytes outside the repository, restores the last anchored
ledger and the original protected-file baseline, and fences every known child.
Native termination stays UNKNOWN. It cannot bless changed security bytes, remove
fences, or change goal, scope, acceptance or mode. If the user intends a new
security baseline or a different contract, that is a new task, not a recovery.

## Incompatible or invalid task state

If Core reports a schema diagnostic or `invalid task state`, never delete the state
or treat it as absent. Preserve the evidence, report managed assurance as UNKNOWN,
and ask the user whether to continue diagnosis or start an independent new task
(which establishes its own current baseline).

The adapter treats a session whose task state cannot be read as having an unreadable
task, not as having none: SessionStart names the task id and code, `status` (and every
other runner command) reports the real diagnosis (for a schema mismatch Core's
`from_version`, `to_version`, `recoverable`, `recovery_action`), and a managed
`thaliris:*` dispatch is refused with no reservation instead of silently running
unmanaged. Generic agents and your own reads and writes are unaffected. A schema
mismatch usually means the plugin or Core was upgraded or downgraded between the
session that wrote the task and this one: running the version that wrote it (or
following Core's `recovery_action` where it applies, on the user's decision) makes the
task readable again. Child bookkeeping (SubagentStart/Stop) is not recorded while the
state is unreadable, so a child's finished status is UNKNOWN until it is. `task-start`
begins an independent task for this session; the unreadable one is left as it is.

## Unreadable lifecycle

`CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED` means a different adapter version wrote
`claude-lifecycle.json` (it is never reinterpreted); `CLAUDE_LIFECYCLE_INVALID` means it
is damaged or an entry lacks required fields. Child identity and handoff status are
UNKNOWN, so Thaliris children's writes and `SendMessage` fail closed and managed
dispatch is refused; the Controller's own work, generic subagents and read-only roles
are unaffected. Do not edit the file. Run the adapter version that wrote it, or ask the
user about an independent new task; `dispose` and `close` need a readable lifecycle.

## A finished child is not reused

A child that returned its final report (`completed`/`stopped`) or was disposed gets no
further `observe`, no writes and no messages. If the Host starts it again (for example a
resumed `SendMessage`), its status stays and the lifecycle records
`resumed-after-terminal` / `started-after-dispose` as an event. New work goes to a fresh
child, with the earlier result as selected evidence.

## After an upgrade, downgrade or Host maintenance

A session keeps using the plugin version it loaded: its hooks, the runner path
SessionStart gave it and the guidance it holds are one version. That is normal, and the
task, authority anchor and lifecycle stay usable; a newer version reads older version-1
lifecycles (including entries without `role_binding`). To move a running session to new
files the user runs `/reload-plugins` (or starts a new session) and uses the runner path
from the new SessionStart notice. A command that names another installed version's
runner is refused with the exact command to use.

If the Host removes the old version's cache directory while a session still has it
loaded, that session's hooks fail to start (the shell cannot find the script, typically
exit 127, which the Host treats as a non-blocking error). The adapter cannot handle this
itself, and its guards (child write fences, dispatch reservation, runner identity) are
not running meanwhile: treat managed assurance as UNKNOWN. It is not task damage: the
project's task ledger and the authority record are untouched. The user reloads the plugin
or opens a new session, then `attach --task-id <id> --reason "<why>"` reconnects the
original ACTIVE task with its authority and evidence.

## Changing sessions

`/clear` or a new conversation gets a new session id, and an old ACTIVE record
never selects the new session's task. To continue deliberately:
`<runner> attach --task-id <id> --reason "<why>"`. To step away without closing:
`<runner> detach --reason "<why>"` (the task stays ACTIVE and anchored).

## Host maintenance

Installing, upgrading or removing this plugin, editing user-level
`~/.claude/settings.json`, global CLAUDE.md, marketplaces or hooks is live Host
maintenance. It needs the user's explicit, separate instruction naming the exact
operation; project task authority and project acceptance do not grant it. Preserve
user-owned bytes you do not recognize. After any Host change, report that loaded
configuration is UNKNOWN until a fresh session shows it active (for example via
`/hooks`, `/agents` or the SessionStart notice): changed files on disk do not prove
activation.
