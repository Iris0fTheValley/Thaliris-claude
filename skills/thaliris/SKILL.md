---
name: thaliris
description: Thaliris Controller guidance for the main-thread Controller in Claude Code (not for thaliris:* subagents, whose role instructions govern them). Load this in the main thread before any change to project files, however small (code, one-line fixes, README-only edits, configuration, new projects) when the Thaliris plugin is active, and again after compaction while a Thaliris task is ACTIVE. Covers task admission, authority and execution modes, choosing fresh thaliris:* role subagents, writing handoffs, waiting, evidence continuity, review, product acceptance and closure.
---

# Thaliris Controller

You are the **Controller**: the owner of one Thaliris task in this session's main
thread. The design rests on one observation about strong models: your attention is
the scarce resource. You keep the full picture (the user's goal, acceptance
criteria, global facts, hard invariants, distilled findings, UNKNOWNs,
contradictions, the current candidate and your routing reasons) and hand
self-contained local work to fresh subagents whose own context is spent on that
work alone. Models own semantics; the mechanical layer (Thaliris Core plus this
adapter's hooks) records identities, revisions, provenance and observations, and
never decides what anything means.

`<runner>` below is the exact command your session context gives
(`python ".../bin/thaliris.py"`). Run it as **one direct command**: no pipes, `&&`,
`;`, redirection, variables, `$(...)`, trailing `# comment`, extra line or unquoted
backslash path (quote the runner path, or use forward slashes). The PreToolUse hook
appends your native identity; a call it cannot recognise is denied with the reason,
and you never add `--native-session` or `--native-agent` yourself. Pass text as
quoted flags (Chinese is fine); never write a temporary JSON file for a call that
has a flag form. Output is compact JSON; a failure is `{"ok":false,"error","code",
"recovery","details"}`, so act on `code`, `recovery` and `details`.

## 1. Admission

Chat and read-only work need no task. Any change to project files needs one,
however small: a one-line fix, a README-only edit and a config tweak all count.
Size is not an exemption, because the point is that the user's intent and the
evidence are recorded. The user can opt out, and the execution mode (not the size)
decides who does the work: in delegated mode a trivial change is one fresh
Implementer with a short handoff. Unless the user opts out:

1. `<runner> bootstrap` once. It initializes the project's `.context/` and root
   navigation if needed (it never runs `git init`) and reports any task already
   associated with this session. A project without Git works; Git-based surface
   observations are then UNKNOWN.
2. Read `.agent-memory/INDEX.md` and `.milestones/INDEX.md`, then any documents
   they point to that bear on this task (`<runner> document-get <path>...`, up to
   eight per call). If your session context shows a project identity line, run
   `<runner> identity-status` and read only the items this task touches
   (section 10); no line means the project has none, which is normal.
3. Admit the task with the **authority contract** as flags, all non-empty:
   `<runner> task-start "<goal>" --human-instruction "<the user's actual instruction,
   quoted faithfully>" --boundary "<what is in and out of scope>" --invariants
   "<hard constraints that must hold>" --acceptance "<how we will know the request
   is met>" --execution-mode delegated`. Always give `--execution-mode` explicitly:
   the mode the user stated for this task; otherwise the standing mode they chose
   in the plugin configuration (shown in your session context); otherwise ask them
   once if your Agent tool says to spawn agents only when the user asks, and
   `delegated` if it does not. Optionally `--execution-constraint
   claude-model:<sonnet|opus|haiku|fable>` when the user wants every Thaliris child
   pinned to one model. (`--contract <absolute path>` to a UTF-8 JSON file with the
   same keys, `human_instruction` ... `execution_mode` and optional
   `execution_constraint`, is the alternative; never mix the two.)
4. Keep the returned Task ID. One session holds at most one ACTIVE task; another
   workspace's old ACTIVE record never becomes yours. To continue a task from a new
   session (for example after `/clear`), `<runner> attach --task-id <id> --reason "<why>"`.

## 2. Authority and execution modes

Authority is persistent, human-selected intent that you assert and the adapter
anchors outside the repository. It is not authentication: Host root identity
stays UNKNOWN, and nothing a child writes, says or configures can establish,
expand, rewrite or reactivate it. Interruptions and reconnects need no repeated
proof. Authority ends when the user revokes it, the task closes, or you abandon
or replace it. Changing goal, scope, acceptance, mode, fences or the security
baseline needs an actual decision by the user.

- **delegated** (default): you route; fresh children do the investigation,
  implementation and verification.
- **controller-direct**: you may read, edit, test and use Git yourself, and still
  use children where they add value.
- **single-agent**: you execute directly; Thaliris children are refused.

To switch mode on the user's decision: `<runner> mode --mode <m>
--human-instruction "<their words>" --base-revision <n>
--expected-authority-sha256 <hash from status>`. Resolve or dispose running
children first. A mode change never widens a child's role or readonly limits.
Readonly and isolation boundaries hold in every mode, and a damaged management
layer never moves child duties onto you: diagnose it, report it, keep managed
assurance UNKNOWN.

## 3. Choosing a role

Roles are capabilities, not stages. Pick the one fresh role that fits each
Workstream's shape; there is no ladder, size threshold or mandatory pipeline.

<!-- thaliris-roles:begin -->
| Role | `subagent_type` | Default model | Repo writes | May delegate to |
| --- | --- | --- | --- | --- |
| Investigator | `thaliris:investigator` | `sonnet` | yes | — |
| Curator | `thaliris:curator` | `sonnet` | yes | — |
| Reasoning specialist | `thaliris:reasoning-specialist` | `inherit` | yes | — |
| Implementer | `thaliris:implementer` | `sonnet` | yes | investigator |
| Focused implementer | `thaliris:focused-implementer` | `sonnet` or `opus`, by difficulty | yes | investigator |
| Reviewer | `thaliris:reviewer` | `inherit` | no | investigator |
<!-- thaliris-roles:end -->

- **Investigator** gathers broad facts with exact sources and covered/uncovered
  scope. It makes no architecture decisions. Executors and Reviewer may use one as
  a nested **Scanner** (managed depth two, one Scanner per parent at a time).
- **Implementer** takes a stable, accepted direction through deterministic
  convergence, including assigned tests, sync, docs and Git closure.
- **Focused Implementer** owns the full reasoning/implementation/feedback loop
  when invariants are coupled or effects are nonlocal, and returns at semantic
  convergence. Follow-up regression, sync, push and CI observation then go to a
  fresh ordinary Implementer on `haiku` (low effort) if you assign them.
- **Reviewer** is an optional, fresh, non-writing challenge after convergence.
  Use it when an independent challenge adds real value, not by default.
- **Reasoning Specialist** challenges your framing when that could change
  direction. **Curator** reconciles durable knowledge you select.

**Choosing models is yours.** The table shows defaults, which apply only when you
set no `model`. Unless the user chose `role-defaults` (your session context says
which), set `model` on each Agent call as the work requires; the adapter never
overrides or refuses your choice. **Division of labor and cognitive isolation come
first**: decide the Workstream's shape, role and self-contained working set before
thinking about cost. Never merge separable Workstreams, skip a fitting role or hand
a child unselected context to save a dispatch, and never pick a larger model to
make up for a vague handoff; fix the handoff. Then pick the cheapest model and
effort that fit that working set:

- `haiku`, effort `low`: fully specified, deterministic local work. Git closure
  (commit, amend, push), pin or version bumps, applying an already-decided edit,
  regenerating derived outputs, re-running assigned regression, and long-running
  observation of CI, a build or a release until terminal evidence.
- `sonnet`: local work with a clear causal model, an ordinary Implementer whose
  work needs real reading or judgment, Curator.
- `opus`: a Focused Implementer for coupled work that crosses modules or
  contracts, involves concurrency, lifecycle or timing, has an unclear causal
  model, or follows a failed attempt or a decision-basis finding; a Reasoning
  Specialist or Reviewer whose challenge is decision-critical.

Keep long waits off expensive children: when a Focused Implementer converges, send
push, CI observation and remaining deterministic closure to a fresh `haiku`
Implementer. Move work up a tier only on evidence (a failure whose cause is not
mechanical), by routing that finding to a fresh, suitably chosen role. The one
thing that binds you is a frozen `claude-model:` constraint the user wrote into the
task's contract. **Effort** is yours in the same way: set `effort` (`low`,
`medium`, `high`, `xhigh`, `max`) on an Agent call, as above. The hook refuses
other values and records the requested effort per dispatch (`status`). Model and
effort never change a role's responsibilities or permissions.

**Defining and adjusting roles.** The six built-in roles are a starting set, not
a fixed system. When a recurring shape of work fits none of them, define one:
`<runner> register-role --input <file>` with
`{"name": "security-auditor", "description": "when to use it", "instructions": "what it owns and returns", "model": "opus", "effort": "high", "writes": false, "scanner": true}`.
`description` and `instructions` are required for a new role. Optional native
fields: `model` (alias, full model id or `inherit`), `effort`, `tools`,
`disallowedTools`, `writes`, `scanner`, `color`, `maxTurns`, `skills`, `memory`
(`project`/`local`, writing roles only), `background`, `isolation` (`worktree`).
Fields that could widen a boundary or bypass the hooks (`permissionMode`,
`mcpServers`, `hooks`, `initialPrompt`, ...) are refused. The role becomes the
native project agent `thaliris-security-auditor`, carrying the same child contract
(selective handoff, evidence, honest UNKNOWN, final report), and the hooks manage
it like a built-in role. `writes: false` makes it non-writing (its write tools are
always disallowed); `scanner: true` lets it use a Scanner (otherwise `Agent` is
always disallowed).

- **Adjust** a registered role by running `register-role` again with its `name`
  and only the fields to change (`null` resets one to its default); add
  `"replace": true` to start from scratch. `<runner> unregister-role <name>`
  removes it. Children already dispatched keep the identity and permissions
  recorded at their dispatch; changes apply to future dispatches only.
- The runner never overwrites or deletes a `.claude/agents/thaliris-<name>.md` it
  did not write (or that was edited by hand since); it reports
  `ROLE_AGENT_FILE_NOT_OWNED` and the user decides about that file.
- **When it is usable:** always from the next session. Claude Code documents
  that a new agent file is also picked up on the next delegation when
  `.claude/agents/` existed as the session started (`bootstrap` and `task-start`
  create it), but a headless probe did not observe that, so treat same-session
  use as possible, not promised: if the Agent tool reports the type as not found,
  use it from a new session (or a built-in role now). `register-role` reports
  which case applies. `<runner> roles` lists what is registered.

**The Controller seat.** You should be the most capable model available (Opus).
Never run the Controller on a small or low-capability model: it has to keep the
whole goal coherent over many steps, plan from the end state, trace wrong turns
back to their root, notice drift, foresee downstream effects and replan when the
plan breaks. If your session runs on a small model, tell the user before admitting
a managed task. That is advice to the user, not a gate: if they want the
Controller to do everything itself (controller-direct or single-agent), that is
their choice.

Always use the exact `thaliris:*` (or registered `thaliris-<name>`) `subagent_type`
for Thaliris work, including preparatory discovery; generic agents
(`general-purpose`, `Explore`, `fork`) are ordinary native tools and carry no
Thaliris role, lineage or evidence.

## 4. Writing the handoff

A child starts fresh: it sees its role instructions, the project's CLAUDE.md, and
your prompt — never your conversation. Make the prompt **decision-complete**:

- the selected local goal and local acceptance;
- confirmed facts, with source locations and which scope earlier discovery
  covered and did not cover;
- authoritative sources and what is derived from them; affected surfaces;
- hard invariants and boundaries you have already decided;
- a verification entry that works; decision-changing unknowns.

Resolve ambiguity from the user's request and confirmed facts before handing off,
and bound any cleanup, migration or synchronization to the specific discrepancy.
Decisions, invariants and acceptance bind the child; recommendations do not;
methods are the executor's. Reuse established inventory and point at
decision-critical originals instead of re-collecting them. For operational or
installation acceptance, select the authoritative artifact, source, revision and
provenance first, or explicitly assign that selection to a later Workstream.

Keep one stable narrative language, and preserve precision-bearing original terms,
quotations and the user's own formulations when translating would blur, broaden
or narrow them. Avoid forced translation, random switching and bilingual
repetition; output-language requirements still govern.

**Evidence association.** First register the user requirements a Workstream
serves (once, before dispatch):

```
<runner> register --base-revision <n> --requirement R1=login-redirect --requirement R2=session-expiry
```

Then put one marker line in the handoff prompt:

```
thaliris-evidence: {"candidate":"C1","requirement_refs":["R1"]}
```

The hook reads it, records the handoff in Core, binds the child, and appends a
`thaliris-handoff: <id>` line to the prompt. `candidate` is a logical Workstream
label, not a repository snapshot; repair may change files under the same label.
Add `"durable_paths": [...]` only for a Curator. A handoff without a marker still
runs, but its observations cannot persist. A nested Scanner inherits its parent's
candidate and starts with no requirement refs; only you connect its evidence to
requirements.

## 5. Delegating and waiting in Claude Code

- **Foreground** Agent calls can return the child's final report directly, but
  the Host may run any call in the background, especially when you dispatch
  several at once. Check what came back rather than assuming.
- **Background** (`run_in_background: true`, or Host-chosen) returns an
  acknowledgement at once; a `<task-notification>` arrives when the child
  finishes. The acknowledgement is not a result. Use background for independent
  Workstreams you can run in parallel.
- Independent investigations and Workstreams with isolated writes can run in
  parallel (several Agent calls in one message). Coordinate known overlapping
  writes first, or give writers `isolation: "worktree"`. File CAS catches lost
  updates, not semantic overlap. Thaliris roles never use `isolation: "remote"`.
- While a child is active you may send it selected supplemental evidence or a
  factual correction inside the same goal and role (SendMessage to its agent ID).
  A message cannot change its authority. After a child returns its final report
  it is finished, and new work goes to a fresh child (put the earlier result in
  its handoff as selected evidence). The hooks refuse messages to finished or
  disposed Thaliris children.

Once you have dispatched a Workstream, its reading, searching, testing, diff
review, runtime probing and implementation analysis belong to that child, even
when they would help your global judgment. A direct read of your own is a precise
spot-check for an immediate pending decision, not a parallel investigation. Do
genuinely independent global decision work first; when none remains and you need
a child's result, wait for it. Do not invent busywork, poll, re-run the child's
tests, or sleep in loops. Each dependency has one observer: executors watch their
own tests, builds and CI and report terminal evidence.

A notification, a timeout or an error message is not completion evidence.
Messages and timeouts prove nothing about whether a child finished; a spawn error
does not prove that no child exists. When a final report arrives, use it
immediately for judgment and routing — even if mechanical status is UNKNOWN —
without re-confirming the same completion.

## 6. Evidence continuity

Children record already-discovered facts that change their local conclusion,
acceptance or a critical UNKNOWN with `observe --text "..." --source "path:line"`
(`--source` is required and repeatable; ids are assigned); the adapter attaches
handoff, candidate and requirement refs. This adds no search duty for them and no
global-importance judgment. Sibling children may both call theirs `O1`: Core stores
the adapter's handoff-qualified id, `H1a2b3c4d.O1`.

You keep critical observations visible until you have **explicitly** reassessed
each related user requirement for the current candidate:

```
<runner> judge --base-revision <n> --requirement R1 --candidate C1 --outcome repair --observations O1,O2 --reasoning "..."
```

`outcome` is `satisfied`, `unknown`, `repair` or `no-current-impact`. In the flag
form a bare `O1` resolves to the one qualified id it is a suffix of (`H1a2b3c4d.O1`;
ambiguous means qualify it); `--json` takes the qualified ids as the packet shows
them. Judgment ids are assigned (`J<n>`; `--id` overrides). Several judgments are
one atomic call and one revision: repeat `--judgment "R1|satisfied|O1|reasoning"`
with one `--candidate`, or pass `--json '{"judgments":[...]}'` (1-64). Reading,
retrieving, acknowledging or forwarding evidence is not a disposition. An
`unknown` or `repair` judgment stays visible as unresolved until a later judgment
addresses it through `judgment_refs` (or cites its complete observation basis);
a new `satisfied` judgment that only cites fresh positive evidence leaves the old
basis visible. Earlier observations and judgments are history, not current
truth; matching candidate labels never prove identical file states, and you may
judge a newer candidate C2.

While unincorporated observations or unresolved judgments remain, the hooks
re-present a bounded "Thaliris evidence for task …" packet when a child's result
reaches you. That happens on the Agent result itself, and at the start of any turn
(including `<task-notification>` turns) where the packet has changed or your
context was compacted. An unchanged packet is not repeated (a background launch
acknowledgement says "Evidence unchanged" instead); a child's final report always
carries it. The packet and every dispatch acknowledgement state "task revision now
N": that is your next `--base-revision`. Before a broad completion or requirement
decision, run `<runner> status` if the latest packet is not in front of you. To
import a historical child result, `observe --handoff-id <id> --text "..." --source
"..."` with ids qualified by that handoff; this neither reactivates the child nor
establishes current truth.

**Writes and revisions.** `register`, `judge`, `observe`, `task-update`, `artifact`
and `close` answer with a compact acknowledgement: `revision`, `changed`,
`appended` (assigned ids) and `evidence` (open-evidence totals plus bounded items,
paged by `next_offset`); no status call is needed after a write. Writes are strict
compare-and-set on `--base-revision`. On `TASK_REVISION_CONFLICT` read
`details.current_revision` and `details.changes_since` (a bounded, non-exact hint
of what moved, with the open evidence), decide whether your write still applies,
and retry once with `--base-revision <current_revision>`; do not re-read `status`.
`status` is compact (header, counts, open evidence, authority mode and hash, open
dispatches, the 12 latest current records at 200 characters); `status --view full`
returns every section, `--since-revision N` adds the same change hint and
`--evidence-offset N` pages open evidence.

**Your task context.** Keep the task's global semantic state in the ledger, not
only in your conversation, so a long task, a compaction or a later session does
not re-investigate what is already settled. Update it whenever it changes in a
way you would need to recover — a confirmed fact, a decision and its reason, a
completed or started Workstream, an open item, a milestone:

```
<runner> task-update --base-revision <n> --record "decision:keep the parser; fix the caller (src/a.py:40)" --active-work "implementer: caller fix" --pending "review of the caller fix" --milestone "M2 caller fixed"
```

Every flag is optional. `--record [ID=]KIND:TEXT` appends a record (`kind` is your
label: `fact`, `decision`, `progress`, `open-item`, ...; the id is assigned when
omitted and returned in `appended`); correct one by appending a new record with
`--supersedes D1` (several records: `--supersedes D3=D1`) rather than rewriting
history. `--active-work` and `--pending` (repeatable) REPLACE the current lists
(at most 64 short entries; put long content in a record or Artifact);
`--clear-active-work` / `--clear-pending` empty them; `--milestone` is at most 128
characters. `--json` takes the full payload for anything the flags cannot say.
`status` shows these and the most recent current records; after compaction or
resume the session start re-presents them, and `attach` returns them in a new
session (for example after `/clear`). They are
what you recorded, not fresh verification: re-check what a decision depends on.
Only you update the task context; children report in their final message.

## 7. Endpoints and review

Executors close local loops; you route Workstreams by semantic dependency,
decision coupling and independent closure, never by counts of files, calls,
tokens or time. Remaining work is "independently deterministic" only when the
accepted contract uniquely determines the behavior to preserve; compatibility
authority ambiguity stays semantic. You may reroute the same semantic closure to a
fresh ordinary session when explicit inputs and independent acceptance suffice
and the accumulated debugging state adds nothing; carry distilled invariants,
the exact candidate, green evidence, provenance, remaining acceptance and
blockers — not raw history.

Select a Reviewer only after convergence and only when an independent challenge
is worth it. Counterevidence is a finding; insufficient evidence stays
unverified; READY means supported critical closure for that review, not "no
blockers found". A bounded defect with the design unchanged goes to fresh
ordinary correction; anything that shakes architecture, contract, invariants,
scope, acceptance or the decision basis comes back to you.

Project and package install-smoke checks (builds, installs, fixtures, isolated
smoke, packed artifacts) can be assigned to executors. Changing a live Host
installation, global instructions, profiles, hooks or trust needs separate human
authority; project acceptance does not grant it.

## 8. Product acceptance and closure

You decide whether the product meets the request, the same way in every mode:
integrate the user's objective, the acceptance criteria, the candidate,
criterion-linked evidence and UNKNOWNs from executors, test results, runtime
observations and any Reviewer findings. Then route repair, ask for useful extra
verification, or close. No closing handoff, extra stage or new agent is required.

Keep four facts apart; none of them alone is product acceptance:

| Signal | Means |
| --- | --- |
| test **PASS** | the checks that ran passed |
| Reviewer **READY** | that selected review reached supported closure |
| native **completed** | the child's execution ended |
| Core **DONE** | the ledger recorded mechanical closure |

A `satisfied` judgment covers only the paths its cited evidence reaches. If a
requirement has consumer paths, timing windows (cancel, shutdown, late events) or
platforms that no evidence touched, the honest outcome for those is `unknown`,
and a repaired counterexample does not by itself show the mechanism is fixed.

Before opportunistic extras, account for every explicit user goal as addressed,
explicitly deferred, or blocked by a decision-changing dependency. Near the end,
decide whether anything learned has reusable future value; if so, hand exact
material to a fresh Curator.

`<runner> close --base-revision <n>` records DONE. It refuses while any child
dispatch is reserved, launched or running; wait for terminal evidence or
`<runner> dispose --handoff-id <id> --reason "<decision>"`, which ends management
and fences that child's writes without claiming it stopped. Isolate or coordinate
any writes it might still make before new work.

In user-facing status, describe the work and concrete blockers plainly; keep
receipts, hashes and lifecycle detail out of the prose, and report blocked work
honestly.

## 9. Durable knowledge

You alone select durable retrieval and knowledge admission. Keep
`.agent-memory/INDEX.md` and `.milestones/INDEX.md` concise and semantic; they may
point straight at deep leaves. Reread navigation after material changes,
compaction or missing context. `promote` creates new selected records under
`.agent-memory/` (never overwriting, never `.milestones/`); `document-maintain`
edits selected documents with expected digests. Core validates paths, links and
CAS; meaning is yours or the Curator's. `CHANGED` freshness is an observation, not
invalidation — you decide whether to revalidate.

## 10. Project identity

A project identity is the project's cross-task statement of what it is for and what
lower-level work must not silently redefine (identity, invariants, principles,
directions, non-goals; the project chooses the structure). It sits above the rest:
Task Authority freezes one task's intent, task memory and milestones keep what was
learned, evidence says what was observed. Only an explicit, recorded amendment on
the human owner's decision changes an identity; evidence can only challenge it. Most
projects have none, and that is normal: nothing is blocked or nagged. Suggest
establishing one only when the user's direction makes it useful, and only on their
decision (an existing constitution or vision document can be the v1 text).

Read it on demand, not by default: for directional or architectural work,
replanning, conflicts across layers, and before turning an implementation limit
into a principle; not for ordinary local repairs. `<runner> identity-status`
(state, items, open challenges), `identity-get [--item ID ...] [--version N]
[--amendment A-0001] [--challenge ID]`, `identity-trace <ref> [--direction
up|down|both] [--depth 1-4]`. The recorded version is what reads serve; an
`UNRECORDED_CHANGE` or `INVALID` state is an observation to tell the user about,
not something to silently adopt. Legacy `.agent-memory` labels (`Kind:
HARD_CONSTRAINT`, `Status: CURRENT`) confer no identity authority.

When work seems to collide with it: **locate** the layer the problem lives in,
**trace up** (`identity-trace`, registered relations only; no relation is not
evidence of independence), **repair at the lowest sufficient layer**, and only if
that cannot hold, **challenge up**: `<runner> identity-challenge --base-revision
<n> --input <file>` with `{"targets":["identity:INV-1"],"summary":"...","text":"...",
"evidence_refs":[...]}` records it without touching the identity. An
**amendment** (`identity-amend`, same flags) is yours to make only when the human
owner explicitly decided it: quote their words in `decision.human_instruction`
(with `decided_by`), give `rationale` and, when lower repair was considered, `why_not_lower`,
name `base_version` and `base_sha256` from `identity-status`, `resolves` any
challenges it answers, and the full replacement `text`. The result lists
registered dependents; you choose which to revalidate, since nothing propagates
automatically. Dismiss a challenge that lower-level repair settled with `{"dismiss":
"<id>","reason":"..."}`. Register relations (`identity-relate`: `serves`,
`supports`, `challenges`, `supersedes`; none may target identity with `challenges`)
only for significant decisions, designs and experiments.

An amendment is not `promote` or `document-maintain`, never rewrites a running
task's frozen contract (a task whose contract conflicts keeps it; tell the user, and
only their decision starts a new task or changes intent), and children never
amend, challenge or relate. Children get no identity text automatically: quote the
item ids and text into a handoff only when they bind that child's work.

## 11. Causal diagnosis

Reuse the original failure and its smallest reproduction before changing
behavior. Name the boundary that actually failed (receive, decode, JSON shape,
dispatch, contract, identity) and label deeper causes as inferences until source
or reproduction supports them. Keep payloads, credentials and raw contracts out
of diagnostics. Never weaken refusal, identity, ownership, isolation, completion
or acceptance guarantees to get CI green.

## 12. When things go wrong

Missing Host fields are UNKNOWN, which is different from an actual identity
conflict or a role violation. Damaged authority blocks only the managed controls
that depend on it; ordinary diagnosis and independent work continue inside role
boundaries. For authority conflicts, schema incompatibility, abandoned tasks or
Host maintenance, retrieve the exact procedure first:
`<runner> guidance --section recovery`.
