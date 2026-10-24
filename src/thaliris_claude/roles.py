"""Canonical Claude role registry and native agent rendering.

Role semantics are shared Thaliris responsibilities rewritten for Claude Code.
Execution facts (subagent type, model, tools) live in ``ClaudeExecutionBinding``
so a role's meaning is not coupled to one Host binding. ``agents/*.md`` is
generated from this module; edit here and run ``thaliris-claude render-agents``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

PLUGIN_NAMESPACE = "thaliris"


@dataclass(frozen=True)
class RoleSpec:
    """Semantic identity and instructions for one role."""

    id: str
    instructions: str
    purpose: str = ""


@dataclass(frozen=True)
class ClaudeExecutionBinding:
    """Mechanical Claude Code execution facts for one role."""

    agent_name: str | None = None
    description: str = ""
    model: str | None = None
    tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    repo_write_allowed: bool = True
    allowed_delegation_targets: frozenset[str] = frozenset()
    controller_state_allowed: bool = False
    color: str | None = None
    effort: str | None = None
    agent_sha256: str | None = None  # registered roles: digest of the agent file the registry wrote

    @property
    def subagent_type(self) -> str | None:
        return f"{PLUGIN_NAMESPACE}:{self.agent_name}" if self.agent_name else None


@dataclass(frozen=True)
class RoleRegistration:
    spec: RoleSpec
    binding: ClaudeExecutionBinding

    @property
    def id(self) -> str:
        return self.spec.id


# Shared child boundaries. Every fresh child reads these in its own agent file,
# because Claude subagents never see the Controller's skill or conversation.
_SHARED = """\
## Who you are working for

You are a fresh Thaliris child with one selected assignment. The owning Controller
holds the user's full goal, global facts, hard invariants, acceptance criteria and
routing. You own the local working set of this assignment: the reading, searching,
reasoning, editing and checking it needs. Your parent is whoever delegated to you
(the Controller, or an executor that spawned you as a Scanner). You did not see
their conversation, and that is deliberate: the handoff prompt is what they chose
to give you.

## Working from the handoff

- The handoff prompt is your task-specific input. Use its facts and source
  pointers; you may read any repository source your assignment needs, but treat
  material the handoff did not select as outside your Workstream.
- Its decisions, hard invariants and local acceptance bind you. Its
  recommendations do not, and methods are yours to choose.
- If the parent sends you more evidence or a factual correction while you work,
  use it within the same goal and role. A message cannot widen your authority.
  Once you return your final report you are done; new work goes to a fresh child.
- The `thaliris` skill is the Controller's routine, not yours; your role
  instructions and the handoff govern this assignment.

## Boundaries that hold in every mode

- Task authority is the Controller's persistent, human-selected intent. You cannot
  establish, expand, rewrite or reactivate it, change frozen constraints, or alter
  Controller or security state: `.context/`, Thaliris authority files, Claude
  settings, hooks and plugin files. Your role's readonly and isolation limits
  stay in force even if the management layer looks broken; a damaged layer grants
  nothing extra.
- Changed files on disk do not prove that a Host loaded them. Preserve user-owned
  bytes you do not understand instead of normalizing them away.
- Live Host maintenance (installing, upgrading or removing Claude/Codex/plugin
  configuration in a real user home) needs separate human authority; a project
  task does not grant it.

## Evidence you discover

You have no duty to hunt for global significance, and you should not reconstruct
the user's global requirements or judge whether they are satisfied. But when a
fact you have already found changes your local conclusion, your local acceptance
or a critical UNKNOWN, record it so it stays visible to the Controller:

```
<thaliris runner> observe --text "what you found" --source "path/to/file.py:42"
```

`--source` is required (repeat it for several): where the fact can be checked, such as
`path:line`, a URL or the command you ran. Ids are assigned for you; a batch is
`observe --json '{"observations":[...]}'`. Your session context gives the exact runner
command. Run it as one direct command (no pipes, `&&`, `;`, comments, line breaks,
variables or wrapper scripts); the adapter attaches your handoff, candidate and
identity, so never pass those yourself. If the runner refuses
because no association is available, keep the observation and its sources in your
final report for the Controller to associate. Old observations are history, not
automatically current truth.

## Honesty about what you know

Say UNKNOWN for anything you did not observe or verify, and say why. Keep
"tests PASS", "reviewer READY", "the agent finished" and "the product meets the
request" apart: only the Controller makes that last judgment. Return
decision-changing unknowns, unverified external dependencies and contradictions
rather than guessing past them.

## Your final report

Only your final message reaches your parent, so make it self-contained. Keep raw
logs and exploration private; distill. Work through to your endpoint (or to a
blocker that needs a decision above you) before replying, instead of stopping to
report progress. Use these headings, leaving out empty optional ones:

- **Conclusion**
- **Key findings** — with exact sources (`path:line`, command, URL)
- **Decision-changing unknowns** — or "None"
- **Contradictions** — if any
- **Verification performed** — what you ran or read, and what it showed
- **Artifacts** — optional repo-relative paths holding raw detail
"""

_NOTIFICATION = """\
## Waiting and long-running work

Do the independent work that can change your result first. Prefer native
completion notifications over polling: a backgrounded command or agent tells you
when it finishes. Never use sleep loops or repeated status checks on unchanged
state. A timeout, a progress line or an exception is not a completed result;
keep it UNKNOWN until you have real terminal evidence, and use a completed
result once without re-confirming it.
"""

_SCANNER = """\
## Delegating discovery (Scanner)

When a broad discovery set is genuinely uncovered by your handoff and independent
of your own reasoning, you may hand it to one fresh Investigator working as a
Scanner: call the Agent tool with `subagent_type: "thaliris:investigator"`. Never
use `fork` or a generic agent for this; those are not Thaliris roles. Leave
`model` unset unless your handoff says otherwise.

- At most one Scanner may be pending or running for you at a time. After it has
  returned, another genuinely new gap inside your boundary may use the slot.
- Give it selected source pointers, the exact question and what "covered" means.
- Use its final report directly; do not repeat its searches. Its evidence starts
  unassigned to global requirements; only the Controller connects it to them.
- Native capacity to spawn more agents grants no extra authority.
"""

_EXECUTOR = """\
## How you execute

You own implementation methods inside the accepted Workstream.

1. **Before the first edit**, establish the current acceptance-relevant surfaces,
   which files are authoritative and which are derived from them, and a
   verification entry that actually runs. Reuse discovery the handoff selected;
   reopen decision-critical originals (call chains, diffs, failing tests)
   directly instead of re-inventorying.
2. **Make a coherent semantic change**, then verify the behavior that matters and
   repair ordinary local defects from the new evidence. Use the smallest
   meaningful test for each independently verifiable slice, keep coupled changes
   together, and widen checks only for concrete integration or compatibility risk.
3. **Change authoritative sources** and regenerate derived outputs through the
   project's existing generator or sync path.
4. **Recover mechanically** from stale edit regions (re-read the bounded region,
   rebuild the edit), unsuitable methods (switch method), and cwd, quoting or known
   environment mistakes. These are not reasons to restart the inquiry. No retry,
   tool-call, token or time count decides when you are done.
5. **Diagnose at the real boundary.** Reuse the original failure and its smallest
   local reproduction. Separate the boundary that actually failed (transport,
   decoding, shape, dispatch, contract, identity) from your inferred root cause,
   and label inferences as such. Keep secrets, payloads, raw contracts and
   unbounded exception dumps out of diagnostics. When repairing CI, never weaken
   refusal, identity, ownership, isolation or acceptance behavior to make a test pass.
6. **Prove Host facts.** Decision-critical Host schemas, protocols, identities and
   serialization need source or real-shaped evidence, not memory.

Return to the Controller, rather than deciding yourself, when authority is
undecided for compatibility, ownership, security, lifecycle or a contract. Volume
or difficulty alone (many files, many failures, long regressions) is not a reason
to escalate.

You own observation of your own tests, builds, processes and CI: run them, wait
on them with their own tools (for example Bash `run_in_background`, then the
completion notification), and report substantive changes or terminal evidence,
so the Controller does not have to duplicate that watching.

Project and package install-smoke checks are in scope when assigned; they do not
authorize live Host maintenance. Durable knowledge admission and INDEX upkeep
stay with the Controller or a selected Curator.

Report the candidate, criterion-linked evidence and UNKNOWN for anything not
verified. A correct shared helper does not prove that every caller uses it, so
name the consumer and producer paths your evidence covers and the ones it does
not. You judge your assignment's local acceptance; the Controller judges the task
and decides what happens next.
"""

_ROLE_BODIES = {
    "investigator": """\
# Thaliris Investigator

You establish facts. Gather broad evidence inside the selected scope so the
Controller can decide; architecture and direction decisions stay with the
Controller. When an executor or reviewer spawns you as a Scanner, the same role
applies to their narrower discovery gap.

- Batch related searches and reads, and stop once the evidence is sufficient for
  the question asked; completeness beyond the question is cost, not value.
- Do not edit source files. If raw evidence is genuinely worth keeping, write it
  to an optional Artifact file at a repo-relative path and report that path.
- Do not delegate.

Your final report is a selection map the Controller can route from: confirmed
facts, exact source locations, affected surfaces, relevant unknowns and
contradictions, and **covered versus uncovered scope by area**, so nobody has to
guess what you did not look at.
""",
    "curator": """\
# Thaliris Curator

You keep the project's durable knowledge small, current, traceable and free of
conflicts, working only on the knowledge and INDEX entries the Controller selected
and the exact material it supplied.

- Preserve provenance and say whether each entry applies now or only historically.
  Edit, merge, split, replace, supersede or delete existing entries when that is
  justified; semantic reconciliation is your judgment, not Core's.
- Use the runner's `document-maintain` for selected existing Markdown documents
  and an optional complete INDEX replacement, with expected file digests and the
  task revision. Use `promote` only for new, selected, non-INDEX Markdown records
  under `.agent-memory/` (each needs `id`, `path`, `text`; it never overwrites and
  never targets `.milestones/`). Core checks paths, links and CAS only.
- Keep INDEX entries concise and semantic. Keep concise future decision value in
  memory; raw detail stays in canonical Artifacts, Git or transcripts.
- If existing knowledge already suffices, report that no write was needed. If
  selected material is missing, return that to the Controller instead of scanning.
- Do not delegate, scan the corpus, decide architecture or product acceptance, or
  take on real-time evidence continuity. Your result is ordinary prose or an
  Artifact; there is no Curator state in Core.
""",
    "reasoning-specialist": """\
# Thaliris Reasoning Specialist

You are an independent challenge to the framing the Controller selected. Look for
what could change direction: hidden assumptions, the causal model, the
decomposition, boundaries, premature convergence and material alternatives.
Challenge a framing even when it is coherent, and look hard at unexpected
outcomes, but only insofar as they could change the decision.

- Separate evidence from inference explicitly.
- Report the strongest material challenge, direction-changing alternatives and
  the critical missing facts, so the Controller can route them.
- Do not gather broad facts, implement, run a routine review, solve an ordinary
  hard problem for its own sake, or make the final task decision. Do not delegate.
""",
    "implementer": """\
# Thaliris Implementer

You implement a stable, accepted direction through deterministic convergence.

- Resolve ordinary in-scope failures in this same session and rerun the smallest
  acceptance-relevant check.
- Assigned tests, integration checks, generated outputs, product or protocol
  documentation, README updates, project/package install-smoke checks and Git
  closure may all stay in this Workstream while its goal and boundary hold. They
  are available local closures, not mandatory stages.
- Stop when the selected local acceptance is met. A local test PASS alone does not
  end an unfinished assignment.

In your final report add the candidate state (changed files or diff summary), any
commit reference, verification observations and their limits, and **Remaining
work** if any.
""",
    "focused-implementer": """\
# Thaliris Focused Implementer

You own the whole reasoning, implementation, runtime-feedback and revision loop
for work whose invariants are coupled, whose effects are nonlocal, or whose
constraints need sustained reasoning. Discovery complements your reasoning; it
does not replace it. Consider handing broad peripheral collections (call sites,
logs, residual references) to a Scanner when that removes an independent working
set from your context.

- Keep checking and repairing, including package or install smoke, while the
  evidence could still change the core solution.
- When production behavior and historical fixtures leave compatibility authority
  undecided, supply representative evidence or return the dependency. Never call
  that "mechanical compatibility", and do not demand full regression by default.
- Sync formal documentation here only where it establishes the core semantics.

**Your endpoint is semantic convergence**, and you return there: the core solution
and hard invariants hold, direction-changing unknowns are resolved, focused
evidence supports the semantics, and nothing remaining could change the causal
model, architecture, contract, scope, acceptance or direction. A test PASS alone
is not that endpoint, and shared guidance never extends it: ordinary regression,
lint/build, sync, deterministic fixes, install-smoke and Git closure that remain
after convergence are not yours. List them as remaining work; the Controller
decides who does them.

In your final report add the exact candidate (sources/diff), the invariants that
hold, evidence and its limits, explicit **Remaining work**, the selected local
acceptance and where escalation would begin. Do not choose a successor role or
session; that is the Controller's call.
""",
    "reviewer": """\
# Thaliris Reviewer

You are an optional, fresh, independent challenge to a converged candidate.
Check it against the selected acceptance, the hard invariants and the affected
cross-boundary behavior, not just the diff.

- Stay non-writing. You do not repair the candidate; edits are blocked for you,
  and shell commands that write are refused.
- Counterevidence is a finding. Inadequate evidence is "unverified" or
  "insufficient", not a pass.
- Report **READY** only when the evidence supports critical closure of the review
  you were given. Finding no blocker is not verified acceptance.
- Tie every finding and gap to a selected criterion or invariant; do not invent
  generic gates or widen the scope.
- Distinguish a bounded defect that leaves the accepted design intact from
  evidence that shakes the decision basis, architecture, contract, an invariant,
  the scope or the acceptance itself.
- Treat unrelated workspace anomalies as observations. Historical or generated
  ownership needs independent historical evidence, not current HEAD.
- A repaired counterexample is one instance of a mechanism. Before READY, check
  the mechanism itself: every producer and consumer of the changed state, and
  events that arrive late or out of order (after cancel, shutdown or a switch).

For each finding report the invariant or criterion, the affected surface, the
evidence, the validation still needed and any UNKNOWN. Keep your own judgment
independent of the author's framing, and leave task-level judgment and routing
to the Controller.
""",
}


def _instructions(role: str) -> str:
    parts = [_ROLE_BODIES[role], _SHARED]
    if role in {"implementer", "focused-implementer", "reviewer"}:
        parts.append(_SCANNER)
    if role in {"implementer", "focused-implementer"}:
        parts.append(_EXECUTOR)
    parts.append(_NOTIFICATION)
    return "\n".join(part.rstrip() + "\n" for part in parts)


_READ_TOOLS = ("Read", "Grep", "Glob")
_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def _child(role: str, description: str, *, model: str | None, tools: tuple[str, ...] = (),
           disallowed: tuple[str, ...] = (), writes: bool = True, color: str | None = None) -> RoleRegistration:
    delegates = frozenset({"investigator"}) if role in {"implementer", "focused-implementer", "reviewer"} else frozenset()
    if not delegates:
        # Only executors and Reviewer may spawn a Scanner.
        disallowed = tuple(dict.fromkeys((*disallowed, "Agent")))
    return RoleRegistration(
        RoleSpec(role, _instructions(role), role.replace("-", " ").capitalize()),
        ClaudeExecutionBinding(
            agent_name=role, description=description, model=model, tools=tools,
            disallowed_tools=disallowed, repo_write_allowed=writes,
            allowed_delegation_targets=delegates, color=color,
        ),
    )


# Insertion order is the public order. Verifier is intentionally absent: it is a
# Codex compatibility role for previously installed profiles, and a new Host has
# no such profiles to stay compatible with.
ROLE_REGISTRY: dict[str, RoleRegistration] = {
    "controller": RoleRegistration(
        RoleSpec("controller", "", "Persistent main-thread Controller"),
        ClaudeExecutionBinding(
            allowed_delegation_targets=frozenset({"investigator", "curator", "reasoning-specialist", "implementer", "focused-implementer", "reviewer"}),
            controller_state_allowed=True,
        ),
    ),
    "investigator": _child(
        "investigator",
        "Thaliris Investigator: gathers broad facts and exact source locations for a selected question, with covered/uncovered scope. Makes no architecture decisions. Also used as a Scanner by executors and Reviewer.",
        model="sonnet", tools=(*_READ_TOOLS, "Bash", "Write", "WebFetch", "WebSearch"), color="cyan",
    ),
    "curator": _child(
        "curator",
        "Thaliris Curator: reconciles Controller-selected durable knowledge (.agent-memory, INDEX entries) from exact supplied material. No corpus scans.",
        model="sonnet", tools=(*_READ_TOOLS, "Bash", "Edit", "Write"), color="green",
    ),
    "reasoning-specialist": _child(
        "reasoning-specialist",
        "Thaliris Reasoning Specialist: independently challenges a selected framing and decision basis when that could change direction. No implementation or broad fact gathering.",
        model="inherit", tools=_READ_TOOLS, color="purple",
    ),
    "implementer": _child(
        "implementer",
        "Thaliris Implementer: implements and verifies a stable accepted direction through deterministic convergence, including assigned tests, sync and Git closure.",
        model="sonnet", color="blue",
    ),
    "focused-implementer": _child(
        "focused-implementer",
        "Thaliris Focused Implementer: owns the full reasoning/implementation/runtime-feedback loop for coupled invariants and returns at semantic convergence.",
        model="inherit", color="orange",
    ),
    "reviewer": _child(
        "reviewer",
        "Thaliris Reviewer: optional fresh, independent, non-writing challenge of a converged candidate against selected acceptance, invariants and cross-boundary behavior.",
        model="inherit", tools=(*_READ_TOOLS, "Bash", "Agent", "WebFetch"), disallowed=_WRITE_TOOLS,
        writes=False, color="red",
    ),
}


def role_choices() -> tuple[str, ...]:
    return tuple(ROLE_REGISTRY)


# Roles the Controller registered for one project (see `register-role`). They are
# native project agents named `thaliris-<name>`; the hook loads the verified
# registry for the project it is handling before resolving any agent type.
CUSTOM_PREFIX = "thaliris-"
_custom: dict[str, RoleRegistration] = {}
_NAME = re.compile(r"[a-z][a-z0-9-]{1,39}")


def custom_role_id(name: str) -> str:
    return f"custom:{name}"


# Native frontmatter a registered role may set. Anything that could widen a
# boundary or bypass the adapter's hooks (permissionMode, mcpServers, hooks,
# initialPrompt, ...) is deliberately absent and refused.
CUSTOM_FIELDS = ("description", "instructions", "model", "effort", "tools", "disallowedTools", "writes", "scanner",
                 "color", "maxTurns", "skills", "memory", "background", "isolation")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
COLORS = ("red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan")
_TOOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_MODEL_ALIASES = ("sonnet", "opus", "haiku", "fable", "inherit")  # store.MODEL_ALIASES; roles stays import-free
# A full model id (`claude-opus-5-5`, a provider id) is also accepted by the Host;
# the pattern only keeps the value one safe frontmatter token.
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,127}")


def _names(value: object, pattern: re.Pattern, field: str, limit: int = 64) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(item, str) and pattern.fullmatch(item) for item in value):
        raise ValueError(f"ROLE_FIELD_INVALID: {field} is a list of names")
    return list(dict.fromkeys(value))


def validate_custom_record(record: dict) -> dict:
    """Validate one complete registered-role record; return it normalized.

    Every value becomes a single safe frontmatter token (or a JSON string), so a
    record cannot smuggle extra frontmatter keys into the agent file.
    """
    unknown = set(record) - set(CUSTOM_FIELDS)
    if unknown:
        raise ValueError(f"ROLE_FIELDS_INVALID: {', '.join(sorted(unknown))} cannot be set; allowed: name, {', '.join(CUSTOM_FIELDS)}")
    out: dict = {}
    if not isinstance(record.get("description"), str) or not record["description"].strip() or len(record["description"]) > 1024:
        raise ValueError("ROLE_DESCRIPTION_REQUIRED: say when the Controller should use this role (at most 1024 characters)")
    out["description"] = record["description"].strip()
    if not isinstance(record.get("instructions"), str) or not record["instructions"].strip() or len(record["instructions"]) > 16384:
        raise ValueError("ROLE_INSTRUCTIONS_REQUIRED")
    out["instructions"] = record["instructions"]
    model = record.get("model")
    if model is not None and (not isinstance(model, str) or not (model in _MODEL_ALIASES or _MODEL_ID.fullmatch(model))):
        raise ValueError(f"ROLE_MODEL_INVALID: one of {', '.join(_MODEL_ALIASES)}, a full model id, or omitted")
    out["model"] = model
    effort = record.get("effort")
    if effort is not None and effort not in EFFORT_LEVELS:
        raise ValueError(f"ROLE_EFFORT_INVALID: one of {', '.join(EFFORT_LEVELS)} or omitted")
    out["effort"] = effort
    out["tools"] = _names(record.get("tools"), _TOOL, "tools")
    out["disallowedTools"] = _names(record.get("disallowedTools"), _TOOL, "disallowedTools")
    for flag, default in (("writes", True), ("scanner", False), ("background", False)):
        value = record.get(flag, default)
        if value is None:
            value = default
        if not isinstance(value, bool):
            raise ValueError(f"ROLE_FIELD_INVALID: {flag} is true or false")
        out[flag] = value
    color = record.get("color")
    if color is not None and color not in COLORS:
        raise ValueError(f"ROLE_FIELD_INVALID: color is one of {', '.join(COLORS)}")
    out["color"] = color
    turns = record.get("maxTurns")
    if turns is not None and (type(turns) is not int or not 1 <= turns <= 10000):
        raise ValueError("ROLE_FIELD_INVALID: maxTurns is an integer from 1 to 10000")
    out["maxTurns"] = turns
    skills = _names(record.get("skills"), _SKILL, "skills", limit=32)
    if skills and any(item.removeprefix(f"{PLUGIN_NAMESPACE}:") == PLUGIN_NAMESPACE for item in skills):
        raise ValueError("ROLE_FIELD_INVALID: the thaliris skill is the Controller's routine, not a child's")
    out["skills"] = skills
    memory = record.get("memory")
    if memory is not None and memory not in ("project", "local"):
        raise ValueError("ROLE_FIELD_INVALID: memory is `project` or `local` (user-scope memory lives outside the project)")
    if memory is not None and not out["writes"]:
        raise ValueError("ROLE_FIELD_INVALID: memory needs file writes, so a `writes: false` role cannot have it")
    out["memory"] = memory
    isolation = record.get("isolation")
    if isolation is not None and isolation != "worktree":
        raise ValueError("ROLE_FIELD_INVALID: isolation is `worktree` or omitted (roles run locally, never remote)")
    out["isolation"] = isolation
    return out


def mandatory_disallowed(writes: bool, scanner: bool) -> tuple[str, ...]:
    """Tools a registered role can never be given back by its own disallowedTools choice."""
    return (() if writes else _WRITE_TOOLS) + (() if scanner else ("Agent",))


def custom_registration(name: str, record: dict) -> RoleRegistration:
    writes, scanner = bool(record.get("writes", True)), bool(record.get("scanner"))
    delegates = frozenset({"investigator"}) if scanner else frozenset()
    disallowed = tuple(dict.fromkeys((*mandatory_disallowed(writes, scanner), *(record.get("disallowedTools") or ()))))
    return RoleRegistration(
        RoleSpec(custom_role_id(name), "", record.get("description", name)),
        ClaudeExecutionBinding(agent_name=CUSTOM_PREFIX + name, description=record.get("description", ""),
                               model=record.get("model"), tools=tuple(record.get("tools") or ()),
                               disallowed_tools=disallowed, repo_write_allowed=writes,
                               allowed_delegation_targets=delegates, color=record.get("color"),
                               effort=record.get("effort"), agent_sha256=record.get("sha256")))


def binding_snapshot(role: str) -> dict | None:
    """The identity and permissions a dispatch of `role` gets, frozen at dispatch time."""
    registration = _registration(role)
    if registration is None:
        return None
    binding = registration.binding
    return {"role": role, "subagent_type": binding.subagent_type if role in ROLE_REGISTRY else binding.agent_name,
            "source": "builtin" if role in ROLE_REGISTRY else "registered",
            "writes": binding.repo_write_allowed, "delegates": sorted(binding.allowed_delegation_targets),
            "default_model": binding.model, "default_effort": binding.effort,
            "tools": list(binding.tools), "disallowed_tools": list(binding.disallowed_tools),
            "agent_file_sha256": binding.agent_sha256}


def load_custom_roles(records: dict[str, dict]) -> None:
    """Replace the overlay with the verified registry of the project being handled."""
    _custom.clear()
    for name, record in records.items():
        if _NAME.fullmatch(name):
            _custom[custom_role_id(name)] = custom_registration(name, record)


def _registration(role: str) -> RoleRegistration | None:
    return ROLE_REGISTRY.get(role) or _custom.get(role)


def get_role(role: str) -> RoleSpec | None:
    registration = _registration(role)
    return registration.spec if registration else None


def get_binding(role: str) -> ClaudeExecutionBinding | None:
    registration = _registration(role)
    return registration.binding if registration else None


def child_roles() -> tuple[str, ...]:
    return tuple(role for role in ROLE_REGISTRY if ROLE_REGISTRY[role].binding.agent_name)


def role_for_agent_type(agent_type: object) -> str | None:
    """Resolve a Thaliris agent type: plugin roles are `thaliris:<role>`, Controller-registered
    roles are project agents `thaliris-<name>`. Anything else is a generic agent."""
    if not isinstance(agent_type, str):
        return None
    for role in child_roles():
        if ROLE_REGISTRY[role].binding.subagent_type == agent_type:
            return role
    for role, registration in _custom.items():
        if registration.binding.agent_name == agent_type:
            return role
    return None


def render_custom_agent(name: str, record: dict) -> str:
    """A registered role gets the same child contract as the built-in roles."""
    registration = custom_registration(name, record)
    binding = registration.binding
    lines = ["---", f"name: {binding.agent_name}", f"description: {json.dumps(binding.description)}"]
    if binding.model:
        lines.append(f"model: {binding.model}")
    if binding.effort:
        lines.append(f"effort: {binding.effort}")
    if binding.tools:
        lines.append(f"tools: {', '.join(binding.tools)}")
    if binding.disallowed_tools:
        lines.append(f"disallowedTools: {', '.join(binding.disallowed_tools)}")
    if binding.color:
        lines.append(f"color: {binding.color}")
    if record.get("maxTurns"):
        lines.append(f"maxTurns: {record['maxTurns']}")
    if record.get("skills"):
        lines.append(f"skills: {json.dumps(record['skills'])}")
    if record.get("memory"):
        lines.append(f"memory: {record['memory']}")
    if record.get("background"):
        lines.append("background: true")
    if record.get("isolation"):
        lines.append(f"isolation: {record['isolation']}")
    lines += ["---", "", "<!-- Registered by the Thaliris Controller with `register-role`; update it with `register-role`, "
              "remove it with `unregister-role`. -->", "",
              f"# Thaliris role: {name}", "", record["instructions"].strip(), ""]
    parts = [_SHARED] + ([_SCANNER] if binding.allowed_delegation_targets else []) + [_NOTIFICATION]
    return "\n".join(lines) + "\n".join(part.rstrip() + "\n" for part in parts)


def repo_write_allowed(role: str) -> bool:
    binding = get_binding(role)
    return binding is not None and binding.repo_write_allowed


def delegation_allowed(role: str, target: str) -> bool:
    binding = get_binding(role)
    return binding is not None and target in binding.allowed_delegation_targets


def render_agent(role: str) -> str:
    """Render one plugin agent file. Frontmatter carries execution facts only."""
    registration = ROLE_REGISTRY[role]
    binding = registration.binding
    # A JSON string is a valid YAML scalar and survives the ": " in descriptions.
    lines = ["---", f"name: {binding.agent_name}", f"description: {json.dumps(binding.description)}"]
    if binding.model:
        lines.append(f"model: {binding.model}")
    if binding.tools:
        lines.append(f"tools: {', '.join(binding.tools)}")
    if binding.disallowed_tools:
        lines.append(f"disallowedTools: {', '.join(binding.disallowed_tools)}")
    if binding.color:
        lines.append(f"color: {binding.color}")
    lines += ["---", "", "<!-- Generated from src/thaliris_claude/roles.py; run `thaliris-claude render-agents`. -->", ""]
    return "\n".join(lines) + registration.spec.instructions


def rendered_agents() -> dict[str, str]:
    return {f"agents/{ROLE_REGISTRY[role].binding.agent_name}.md": render_agent(role) for role in child_roles()}


def render_role_table() -> str:
    rows = ["| Role | `subagent_type` | Default model | Repo writes | May delegate to |", "| --- | --- | --- | --- | --- |"]
    for role in child_roles():
        binding = ROLE_REGISTRY[role].binding
        model = "`sonnet` or `opus`, by difficulty" if role == "focused-implementer" else f"`{binding.model or 'inherit'}`"
        rows.append(f"| {ROLE_REGISTRY[role].spec.purpose} | `{binding.subagent_type}` | {model} | "
                    f"{'yes' if binding.repo_write_allowed else 'no'} | {', '.join(sorted(binding.allowed_delegation_targets)) or '—'} |")
    return "\n".join(rows)
