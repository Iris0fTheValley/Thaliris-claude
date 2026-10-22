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
