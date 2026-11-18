"""Claude Code hook handlers.

Hooks bind mechanical identity (session, native agent, handoff), enforce role
and readonly boundaries, and re-present unresolved evidence to the Controller.
Host fields that are missing stay UNKNOWN; hook events never decide acceptance.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
import time

from thaliris import core

from . import __version__, guards, roles, store

AGENT_TOOLS = frozenset({"Agent", "Task"})
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})
FILE_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
# Controller tool results the adaptive lane counter adds up (registered in hooks.json with the launcher's `--lane` fast path).
LANE_TOOLS = frozenset({"Read", "Bash", "PowerShell", "Grep", "Glob", "WebFetch"})
_MARKER = re.compile(r"^[ \t]*thaliris-evidence:[ \t]*(\{.*\})[ \t]*$", re.MULTILINE)
_READ_ONLY_CORE = ("task-status", "task-get", "task-show", "catalog", "document-get", "version", "stale", "milestone-check",
                  "identity-status", "identity-get", "identity-trace")


class Refusal(Exception):
    pass


def plugin_root() -> Path:
    return Path(os.environ.get("THALIRIS_CLAUDE_PLUGIN_ROOT") or Path(__file__).resolve().parents[2])


def runner_path() -> str:
    return (plugin_root() / "bin" / "thaliris.py").as_posix()


def runner_command() -> str:
    # The interpreter running this hook is the discovered Python >= 3.11; naming it
    # avoids depending on whichever `python` the Bash tool resolves.
    return f'"{Path(sys.executable).as_posix()}" "{runner_path()}"'


def _deny(reason: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


def _context(event: str, text: str, **extra) -> dict:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text, **extra}}


def _shape_gaps(event: str, payload: object, needed: dict[str, type]) -> str:
    """Hook-shape self-check: name the Host fields this event needs that are missing or of another type.

    Compatibility is by hook shape, never by Claude Code version. A gap is reported as UNKNOWN and changes nothing
    else: the caller keeps its existing fail-closed or skip behaviour, and an unverified shape is never treated as bound.
    Dotted names look into nested objects. Returns "" when the shape is as expected; the text is short and bounded.
    """
    missing = []
    for name, kind in needed.items():
        value = payload
        for part in name.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if not isinstance(value, kind) or (kind is str and not value):
            missing.append(name)
    if not missing:
        return ""
    return f"host_hook_shape: UNKNOWN ({event} payload lacks or mistypes: {', '.join(missing[:6])}); binding for this event is not assumed."


def _caller(payload: dict) -> dict:
    """Return the caller's identity: child?, Thaliris role or None, native agent id, permissions.

    `agent_id` is present only inside a subagent; the main thread is the
    Controller position, unless the process environment shows another agent
    spawned this session (`store.foreign_signal`): then it is UNKNOWN. A child already dispatched by a managed task keeps the
    role binding snapshotted at its dispatch, so editing or re-registering its
    role file later never reinterprets it. Other children resolve from the
    verified registry; a child with a generic agent type has no Thaliris role.
    """
    # The environment may only lower authority: a session spawned by another agent is UNKNOWN, never the Controller.
    foreign = store.foreign_signal()
    if payload.get("agent_id") is None:
        if foreign:
            return {"child": False, "role": "unknown", "agent_id": None, "writes": True, "delegates": frozenset(),
                    "foreign": foreign}
        controller = roles.get_binding("controller")
        return {"child": False, "role": "controller", "agent_id": None, "writes": True,
                "delegates": controller.allowed_delegation_targets, "foreign": None}
    agent_id, agent_type = store.native_id(payload.get("agent_id")), payload.get("agent_type")
    snapshot = _dispatched_binding(payload, agent_id, agent_type)
    if snapshot is not None:
        return {"child": True, "role": snapshot["role"], "agent_id": agent_id, "writes": snapshot.get("writes") is True,
                "delegates": frozenset(snapshot.get("delegates") or ()), "foreign": foreign}
    role = roles.role_for_agent_type(agent_type)
    binding = roles.get_binding(role) if role else None
    writes = binding.repo_write_allowed if binding else (False if foreign else None)  # a generic subagent of an UNKNOWN session is read-only
    return {"child": True, "role": role, "agent_id": agent_id, "writes": writes,
            "delegates": binding.allowed_delegation_targets if binding else frozenset(), "foreign": foreign}


def _dispatched_types(lifecycle: dict) -> set[str]:
    return {item["subagent_type"] for item in lifecycle["dispatches"] if isinstance(item.get("subagent_type"), str)}


def _dispatched_binding(payload: dict, agent_id: str | None, agent_type: object) -> dict | None:
    """The dispatch-time role snapshot for a child of this session's managed task, if any."""
    association = store.resolve_session(payload.get("session_id"))
    if association is None or not isinstance(agent_type, str):
        return None
    try:
        lifecycle = store.read_lifecycle(*association)
        if agent_type not in _dispatched_types(lifecycle):
            return None
        if agent_id is not None and store.dispatch_for_agent(lifecycle, agent_id) is None:
            # The Host writes its exact record shortly after SubagentStart; bind as soon as it exists.
            with store.transaction(*association, observe_only=True) as ctx:
                store.bind_from_host(ctx, agent_id, agent_type, payload.get("transcript_path"))
                lifecycle = ctx["lifecycle"]
        return store.snapshot_identity(lifecycle, agent_id, agent_type)
    except (ValueError, OSError):
        return None


PACKET_SECTION_LINES = 8


def _task_contract(root: Path, task_id: str) -> dict | None:
    try:
        record = store.authority(root, task_id).read()
    except (OSError, ValueError):
        return None
    return record.get("contract") if isinstance(record, dict) and isinstance(record.get("contract"), dict) else None


def _bounded(lines: list[str], more: str) -> list[str]:
    return lines if len(lines) <= PACKET_SECTION_LINES else lines[:PACKET_SECTION_LINES] + [f"- … {len(lines) - PACKET_SECTION_LINES} more {more}"]


def _verification_lines(section: dict | None) -> list[str]:
    """Checks that withhold (red, stale, unrun), tampered check files and compiled/partial requirements without a check."""
    if not section:
        return []
    lines = []
    for entry in section.get("checks", []):
        if entry.get("status") == "green":
            continue
        detail = str(entry.get("status")) + (f" ({entry['reason']})" if entry.get("reason") else "")
        if entry.get("result_id"):
            detail += f", latest {entry.get('outcome')} {entry['result_id']}"
        lines.append(f"- check {entry['id']}: {detail}; withholds satisfied for {', '.join(entry.get('requirement_refs') or []) or '(none)'}")
    for item in section.get("tamper", []):
        state = f"recorded as observation {item['observation_id']}" if item.get("recorded") else "not yet recorded"
        lines.append(f"- check {item['check_id']}: declared files changed ({', '.join(item.get('paths') or [])}); {state}. "
                     "Accept the content with `check --supersede`, or restore it")
    if section.get("uncovered_requirements"):
        lines.append("- compiled/partial requirement(s) without a declared check: " + ", ".join(section["uncovered_requirements"][:16]))
    lines = _bounded(lines, "in `status`")
    if lines:
        lines.append("A satisfied judgment or close on these needs the checks green on the current tree (`verify --check <id>`), "
                     "or `--override-reason` (recorded).")
    return lines


def _claims_lines(section: dict | None) -> list[str]:
    without = (section or {}).get("handoffs_without_typed_claims") or {}
    if not without.get("total"):
        return []
    return [f"- {without['total']} handoff(s) returned without a readable `thaliris-claims` block (missing {without.get('missing', 0)}, "
            f"malformed {without.get('malformed', 0)}): {', '.join(without.get('ids') or [])}. Nothing was ingested from them; "
            "read their final reports yourself."]


def _advisory_detail(item: dict) -> str:
    for key in ("requirements", "paths"):
        if item.get(key):
            return ", ".join(map(str, item[key][:6])) + (" …" if len(item[key]) > 6 else "")
    if item.get("code") == "CHECK_FAILED_TWICE":
        return f"check {item.get('check_id')}"
    if item.get("code") == "CHANGE_SIZE_EXCEEDED":
        return f"{item.get('files')} file(s), {item.get('changed_lines')} changed line(s)"
    if item.get("code") == "CONTROLLER_TOOL_OUTPUT":
        return f"{item.get('tokens')} of {item.get('threshold')} tokens"
    return ""


def _advisory_lines(section: dict | None) -> list[str]:
    items = (section or {}).get("items") or []
    if not items:
        return []
    lines = _bounded([f"- advisory {item.get('code')}: {_advisory_detail(item)}".rstrip(": ") for item in items], "in `status`")
    lines.append("Adaptive lane advisories never block. When one fires, record why and escalate to delegated "
                 "(`mode --mode delegated`, on the user's instruction) or continue with a reason.")
    return lines


def evidence_state(root: Path, task_id: str) -> dict | None:
    """What needs the Controller's attention: the packet text and a digest of its content.

    Open observations and unresolved judgments, plus (Core 0.6.0) checks that withhold, handoffs without typed claims
    and adaptive lane advisories. The digest leaves out the task revision (every dispatch bumps it), so "nothing new to
    judge" keeps the same digest however many dispatches were reserved in between. UserPromptSubmit and PostToolUse
    compare the same digest.
    """
    core.select_task(root, task_id)
    status = core.task_status(root, view="compact", contract=_task_contract(root, task_id))
    evidence = status["Evidence"]
    verification = _verification_lines(status.get("Verification"))
    claims = _claims_lines(status.get("Claims"))
    advisories = _advisory_lines(status.get("Advisories"))
    if not (evidence["unincorporated_total"] or evidence["unresolved_judgments_total"] or verification or claims or advisories):
        return None
    counts = (f"{evidence['unincorporated_total']} observation(s) not yet incorporated into a requirement judgment, "
              f"{evidence['unresolved_judgments_total']} unresolved repair/unknown judgment(s)")
    lines = []
    for item in evidence["unincorporated"]:
        lines.append(f"- observation {item['observation_id']} → requirement {item['requirement_ref'] or '(unassigned)'} "
                     f"(candidate {item['candidate']}, handoff {item['handoff_id']})")
    for item in evidence["unresolved_judgments"]:
        lines.append(f"- judgment {item['id']}: {item['outcome']} for {item['requirement_ref']} (candidate {item['candidate']})")
    if evidence["next_offset"] is not None:
        lines.append(f"- … more with `status --evidence-offset {evidence['next_offset']}`")
    if evidence["unincorporated_total"] or evidence["unresolved_judgments_total"]:
        lines.append("Before you call a requirement satisfied or close the task, reassess each item for the current candidate "
                     "(satisfied / unknown / repair / no-current-impact) with `judge`. Reading or forwarding it is not a disposition.")
    for title, section in (("Verification", verification), ("Claims", claims), ("Advisories", advisories)):
        if section:
            lines += [f"{title}:"] + section
    revision = status["Task"]["revision"]
    body = "\n".join(lines)
    return {"revision": revision, "counts": counts, "unincorporated": evidence["unincorporated_total"],
            "unresolved": evidence["unresolved_judgments_total"], "digest": counts + "\n" + body,
            "text": f"Thaliris evidence for task {task_id} (task revision now {revision}): {counts}.\n" + body}


def evidence_packet(root: Path, task_id: str) -> str | None:
    evidence = evidence_state(root, task_id)
    return evidence["text"] if evidence else None


# ---------------------------------------------------------------- PreToolUse

def _marker(prompt: str) -> dict | None:
    found = _MARKER.findall(prompt)
    if not found:
        return None
    if len(found) > 1:
        raise Refusal("A handoff may carry at most one `thaliris-evidence:` marker line.")
    try:
        value = json.loads(found[0])
    except ValueError as exc:
        raise Refusal("The `thaliris-evidence:` marker must be one line of JSON.") from exc
    if (not isinstance(value, dict) or set(value) - {"candidate", "requirement_refs", "durable_paths"}
            or not isinstance(value.get("candidate"), str) or not value["candidate"]
            or not isinstance(value.get("requirement_refs", []), list)
            or not all(isinstance(ref, str) for ref in value.get("requirement_refs", []))
            or not isinstance(value.get("durable_paths", []), list)):
        raise Refusal('Marker shape: thaliris-evidence: {"candidate":"C1","requirement_refs":["R1"]} (durable_paths for Curator only).')
    return value


def _dispatch(payload: dict, tool_input: dict) -> dict | None:
    caller = _caller(payload)
    is_child, caller_role, agent_id = caller["child"], caller["role"], caller["agent_id"]
    # New dispatches resolve only from the verified registry: writing an agent file mints no role.
    target_role = roles.role_for_agent_type(tool_input.get("subagent_type"))
    if target_role is None:
        if is_child and caller_role is not None:
            return _deny("Thaliris children delegate discovery only to a fresh `thaliris:investigator` Scanner; "
                         "generic agents and forks are not Thaliris roles.")
        return None
    if is_child and target_role not in caller["delegates"]:
        return _deny(f"Role {caller_role or 'generic agent'} may not delegate to {target_role}. "
                     "Return the uncovered need to your parent in your final report.")
    if tool_input.get("isolation") == "remote":
        return _deny("Thaliris roles run locally so their identity can be observed; omit `isolation: remote`.")
    association, problem = store.inspect_session(payload.get("session_id"))
    if association is None:
        if problem is not None:
            # The task exists but cannot be read: that is not "no task". Never fall through to ordinary routing.
            return _deny(f"Thaliris managed dispatch refused: {store.problem_text(problem)} No reservation was made, and the "
                         "dispatch was not treated as unmanaged.")
        return None  # No managed task: ordinary native routing, no reservation.
    if caller["foreign"]:
        return _deny(f"This session is an unmanaged foreign session ({caller['foreign']}), not the Controller of the task it "
                     "resolves to; Thaliris managed dispatch is refused and no reservation was made.")
    effort = tool_input.get("effort")
    if effort is not None and effort not in roles.EFFORT_LEVELS:
        return _deny(f"`effort` is one of {', '.join(roles.EFFORT_LEVELS)} (or omitted for the role default). "
                     "Effort sets reasoning depth only; it never changes a role's responsibilities or permissions.")
    root, task_id = association
    prompt = tool_input.get("prompt") if isinstance(tool_input.get("prompt"), str) else ""
    updated = dict(tool_input)
    try:
        marker = _marker(prompt)
        with store.transaction(root, task_id) as ctx:
            contract = ctx["authority"]["contract"]
            if contract.get("execution_mode", "delegated") == "single-agent":  # an older anchor may omit the mode
                raise Refusal("This task's execution mode is single-agent: do the work directly, without Thaliris children.")
            parent = "controller"
            if is_child:
                parent_entry = store.dispatch_for_agent(ctx["lifecycle"], agent_id) if agent_id else None
                if parent_entry is None:
                    raise Refusal("Your native identity is not bound to a Thaliris handoff, so a managed Scanner cannot be reserved. "
                                  "Report the discovery gap in your final report.")
                if parent_entry["parent"] != "controller":
                    raise Refusal("Maximum managed depth is two: a Scanner cannot delegate.")
                if parent_entry["disposed"]:
                    raise Refusal("Your handoff was disposed by the Controller.")
                if store.active_scanner(ctx["lifecycle"], agent_id):
                    raise Refusal("You already have a Scanner pending or running. Use its result first.")
                if marker and marker.get("requirement_refs"):
                    raise Refusal("A nested Scanner cannot select global requirement refs; its evidence starts unassigned.")
                parent = agent_id
                marker = None
            elif marker is not None:
                if not marker.get("requirement_refs"):
                    raise Refusal("A top-level evidence marker needs at least one registered requirement ref.")
                if marker.get("durable_paths") and target_role != "curator":
                    raise Refusal("`durable_paths` is selected only for Curator handoffs.")
            try:
                alias = store.validate_constraint(contract)
            except ValueError:
                raise Refusal(
                    f"UNSUPPORTED_EXECUTION_CONSTRAINT: the task's frozen execution constraint "
                    f"`{contract.get('execution_constraint')}` names a model alias this plugin version cannot resolve, so the "
                    "frozen intent is unknown and managed dispatch is refused. No reservation was made. `close`, `abandon` and "
                    "`dispose` still work. Update the plugin if the alias was added later, or close this task and start a new "
                    "one with a supported `claude-model:` constraint.") from None
            requested = tool_input.get("model")
            if alias is not None:
                if requested not in (None, alias) and store.resolve_alias(requested) != alias:
                    raise Refusal(f"The task's frozen execution constraint selects model `{alias}`; per-spawn overrides are refused.")
                updated["model"] = alias
            # Otherwise the model is the Controller's choice; the adapter never overrides or refuses it.
            entry = store.reserve(ctx, role=target_role, subagent_type=tool_input["subagent_type"],
                                  tool_use_id=str(payload.get("tool_use_id") or ""), parent=parent, prompt=prompt,
                                  model=updated.get("model"), background=bool(tool_input.get("run_in_background")),
                                  association=marker, effort=effort, role_binding=roles.binding_snapshot(target_role))
    except Refusal as exc:
        return _deny(str(exc))
    except ValueError as exc:
        return _deny(f"Thaliris managed dispatch refused ({exc}). No reservation was made.")
    if entry["handoff_id"]:
        updated["prompt"] = prompt.rstrip() + f"\n\nthaliris-handoff: {entry['handoff_id']}"
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}


APPROVE_SETTING = "CLAUDE_PLUGIN_OPTION_AUTO_APPROVE_RUNNER"


def _approve_runner(payload: dict, subcommand: str | None) -> bool:
    """Approve the plugin's own runner so managed work does not stall on prompts.

    Only a verified direct invocation reaches here. The user can turn this off in
    the plugin's configuration, and plan mode keeps its promise: only read-only
    runner commands are approved there.
    """
    if os.environ.get(APPROVE_SETTING, "true").strip().lower() in {"false", "0", "no", "off"}:
        return False
    if payload.get("permission_mode") == "plan":
        return subcommand in guards.READ_ONLY_COMMANDS
    return True


def _thaliris_typed(agent_type: object) -> bool:
    """A Thaliris agent type, even when its registration can no longer be verified."""
    return isinstance(agent_type, str) and (roles.role_for_agent_type(agent_type) is not None
                                            or agent_type.startswith((f"{roles.PLUGIN_NAMESPACE}:", roles.CUSTOM_PREFIX)))


def _error_code(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else type(exc).__name__


def _lifecycle_unreadable(exc: BaseException) -> str:
    return (f"This session's task lifecycle cannot be read ({_error_code(exc)}), so the child's handoff status is UNKNOWN. "
            "Recovery: the Controller runs `status` for the diagnosis; the user decides whether to run the adapter version that "
            "wrote it or start an independent new task (`guidance --section recovery`).")


def _child_fence(payload: dict) -> str | None:
    """Why a child's write is refused because its handoff no longer grants one, or None when it is not fenced.

    A disposed handoff and a finished one (the child returned its final report) grant no writes: new work needs a fresh
    dispatch, and a child resumed after its final report is not that. When the lifecycle cannot be read, a Thaliris child's
    handoff status is UNKNOWN, so its write fails closed; a generic subagent has no handoff to consult.
    """
    target = store.child_task(payload.get("session_id"))
    agent_id = store.native_id(payload.get("agent_id"))
    if target is None or agent_id is None:
        return None
    try:
        entry = store.dispatch_for_agent(store.read_lifecycle(*target), agent_id)
    except (ValueError, OSError) as exc:
        if _thaliris_typed(payload.get("agent_type")):
            return _lifecycle_unreadable(exc) + " Writes are refused until it is readable."
        return None
    if entry is None:
        return None
    if entry["disposed"]:
        return "Your handoff was disposed by the Controller; further writes are fenced."
    if entry["native_status"] in store.TERMINAL_STATUSES:
        return ("This Thaliris child already returned its final report; its handoff grants no further writes. New work needs a "
                "fresh dispatch from the Controller (the earlier result can be selected evidence).")
    return None


_INDIRECT_CAUSES = {
    "comment": "a `#` comment (the adapter appends your identity to the end of the command, which would land inside the comment)",
    "newline": "more than one line (a line break outside quotes starts a separate command)",
    "operator": "a pipe, `&&`, `;`, `&` or redirection",
    "expansion": "command substitution or backticks outside single quotes",
    "variable": "a `$name`, `${...}` or `$?` inside double quotes, which the shell silently replaces (put prose containing `$` "
                "in single quotes)",
    "escape": "a backslash before a quote or shell character: PowerShell does not escape with a backslash, so PowerShell would "
              "read the command differently from the adapter (use single quotes; double a literal apostrophe as `''`)",
    "smart_quote": "a curly quote, which PowerShell treats as a quote delimiter (use ASCII quotes, or 「」 in the text)",
    "stop_parsing": "the PowerShell `--%` stop-parsing token",
    "prefix": "a wrapper or other words before the interpreter and runner path",
    "repeated": "the runner path appearing more than once",
    "unparseable": "unbalanced quotes",
}


def _indirect_reason(problems: list[str]) -> str:
    found = [_INDIRECT_CAUSES[item] for item in problems if item in _INDIRECT_CAUSES]
    cause = "; ".join(found) if found else "something other than a plain interpreter, runner path and arguments"
    return (f"Run the Thaliris runner as one direct command. This one is not direct: it has {cause}. Use exactly "
            f"`{runner_command()} <command> <arguments>` with no pipes, `&&`, redirection, comments, line breaks, variables "
            "or command substitution. Put prose in a single-quoted argument (`$`, backslashes and backticks are literal "
            "there), or use the runner's flag forms (`--text`, `--reasoning`, ...), instead of writing JSON files.")


def _runner_miss_reason(misses: list[dict]) -> str:
    first = misses[0]
    cause = {
        "backslash": f"the runner path `{first['path']}` is written with unquoted backslashes, which the shell strips; quote it "
                     "or use forward slashes",
        "version": f"`{first['path']}` is another installed version or location of the runner than this session's hook "
                   f"(`{runner_path()}`), so the adapter cannot add your identity",
        "path": f"`{first['path']}` is not the exact runner path this session's hook expects (`{runner_path()}`)",
    }[first["kind"]]
    return (f"This command runs a Thaliris runner that the adapter does not recognise as this session's runner: {cause}. "
            f"Without recognition your identity is not added and the runner fails NATIVE_IDENTITY_UNKNOWN. Use exactly "
            f"`{runner_command()} <command> <arguments>` as one direct command.")


def _shell(payload: dict, tool_input: dict) -> dict | None:
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    caller = _caller(payload)
    is_child, caller_role, agent_id = caller["child"], caller["role"], caller["agent_id"]
    powershell = payload.get("tool_name") == "PowerShell"
    invocation = guards.runner_invocation(command, runner_path(), powershell=powershell)
    if invocation is None:
        misses = guards.runner_misses(command, runner_path())
        if misses:
            return _deny(_runner_miss_reason(misses))
    else:
        if not invocation["direct"]:
            return _deny(_indirect_reason(invocation.get("problems") or []))
        if invocation["forged_identity"]:
            return _deny("`--native-session` and `--native-agent` are supplied by the adapter hook; remove them.")
        subcommand = invocation["subcommand"]
        if caller["foreign"] and subcommand not in guards.READ_ONLY_COMMANDS:
            return _deny(f"`{subcommand}` establishes or uses Thaliris authority, and this session is an unmanaged foreign "
                         f"session ({caller['foreign']}): it was spawned by another agent and is UNKNOWN, not the Controller. "
                         "Only read-only views (status, guidance, catalog, roles, version) are available.")
        if is_child and subcommand in guards.CONTROLLER_COMMANDS:
            return _deny(f"`{subcommand}` belongs to the owning Controller. Return the need in your final report.")
        if is_child and subcommand in guards.CURATOR_COMMANDS and caller_role != "curator":
            return _deny(f"`{subcommand}` is reserved for the Controller or a selected Curator.")
        session = store.native_id(payload.get("session_id"))
        if session is None or (is_child and agent_id is None):
            return None  # The runner reports NATIVE_IDENTITY_UNKNOWN itself.
        association = store.resolve_session(session) if is_child else None
        if association is not None:
            # By a child's first runner call the Host has written its exact record.
            try:
                with store.transaction(*association, observe_only=True) as ctx:
                    entry = store.bind_from_host(ctx, agent_id, payload.get("agent_type"), payload.get("transcript_path"))
                    if entry is not None and entry["native_status"] == "reserved":
                        store.set_status(ctx, entry, "running")  # the Host record is written after SubagentStart
            except (ValueError, OSError):
                pass  # an unreadable lifecycle: the runner reports its real diagnosis (with a code) instead of NATIVE_IDENTITY_UNKNOWN
        suffix = f" --native-session {session}" + (f" --native-agent {agent_id}" if is_child else "")
        output = {"hookEventName": "PreToolUse", "updatedInput": {**tool_input, "command": command.rstrip() + suffix}}
        if _approve_runner(payload, subcommand):
            output.update(permissionDecision="allow",
                          permissionDecisionReason="Thaliris runner, verified as one direct invocation of this plugin's own CLI")
        return {"hookSpecificOutput": output}
    if is_child:
        if guards.direct_core_call(command, powershell):
            return _deny("Children use the Thaliris runner, not the raw Core CLI or adapter module.")
        writes = guards.obvious_write(command, powershell)
        if writes and caller["writes"] is False:
            return _deny(f"The {caller_role} role is non-writing; this command looks like a write. Report what should change instead.")
        if writes and guards.mentions_protected(command):
            return _deny("Children cannot change Controller or security state (.context/, .project-identity/, Thaliris authority, Claude settings).")
        fence = _child_fence(payload) if writes else None
        if fence:
            return _deny(fence)
        return None
    if store.resolve_session(payload.get("session_id")) is not None:
        if guards.direct_core_call(command, powershell) and not any(name in command for name in _READ_ONLY_CORE):
            return _deny("In a managed task, mutate the ledger through the Thaliris runner so authority stays anchored.")
        ledger = (".context/tasks", ".context/state.json", store.LIFECYCLE_NAME, store.ROLES_NAME)
        if guards.obvious_write(command, powershell) and any(name.casefold() in command.replace("\\", "/").casefold() for name in ledger):
            return _deny("Hand edits to the task ledger or lifecycle break authority anchoring; use the runner.")
    return None


def _file(payload: dict, tool_input: dict) -> dict | None:
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(target, str):
        return None
    caller = _caller(payload)
    association = store.child_task(payload.get("session_id"))
    root = association[0] if association else store.project_root(Path(payload.get("cwd") or "."))
    if caller["child"]:
        if caller["writes"] is False:
            return _deny(f"The {caller['role']} role is non-writing. Report the change you would make instead.")
        fence = _child_fence(payload)
        if fence:
            return _deny(fence)
        if store.is_managed_project(root) and guards.protected_target(root, target, controller=False):
            return _deny("Children cannot change Controller or security state (.context/, .project-identity/, Claude settings).")
        return None
    if store.is_managed_project(root) and guards.protected_target(root, target, controller=True):
        return _deny("Hand edits to the task ledger or lifecycle break authority anchoring; use the Thaliris runner.")
    return None


def _message(payload: dict, tool_input: dict) -> dict | None:
    """SendMessage resumes even a finished agent.

    An active child may receive supplemental evidence; a child that returned its
    final report, or was disposed, is not reused for new work (H092/H145).
    """
    association = store.child_task(payload.get("session_id"))
    target = store.native_id(tool_input.get("to"))
    if association is None or target is None:
        return None
    try:
        entry = store.dispatch_for_agent(store.read_lifecycle(*association), target)
    except (ValueError, OSError) as exc:
        # The addressee may be a finished or disposed Thaliris child; without the lifecycle that is UNKNOWN, so fail closed.
        return _deny(_lifecycle_unreadable(exc) + " Messages are refused until it is readable.")
    if entry is not None and entry["disposed"]:
        return _deny("That Thaliris handoff was disposed; a late message cannot reactivate it. Start a fresh child for new work.")
    if entry is not None and entry["native_status"] in store.TERMINAL_STATUSES:
        return _deny("That Thaliris child already returned its final report. Dispatch a fresh child with a new handoff "
                     "(you can include the earlier result as selected evidence).")
    return None


def _skill(payload: dict, tool_input: dict) -> dict | None:
    """The Controller skill is the main thread's routine; children keep their role (H143)."""
    if payload.get("agent_id") is None:
        foreign = store.foreign_signal()
        if foreign and str(tool_input.get("skill") or tool_input.get("name") or "").removeprefix(f"{roles.PLUGIN_NAMESPACE}:") == "thaliris":
            return _deny(f"The thaliris skill is the owning Controller's routine; this session is an unmanaged foreign session "
                         f"({foreign}), UNKNOWN and not a Controller.")
        return None
    if str(tool_input.get("skill") or tool_input.get("name") or "").removeprefix(f"{roles.PLUGIN_NAMESPACE}:") == "thaliris":
        return _deny("The thaliris skill is the owning Controller's routine; your role instructions govern this assignment. "
                     "A specific rule can still be read with the runner's `guidance` command.")
    return None


def _mcp(payload: dict, tool: str) -> dict | None:
    """MCP tools can spawn unmanaged agents or run commands, which no Thaliris child may do.

    Applies to a Thaliris child (a bound or registered role, a Thaliris-typed agent) and to any subagent of an UNKNOWN
    foreign session. The Controller, a foreign main thread and a generic subagent of an ordinary session are unaffected.
    """
    caller = _caller(payload)
    if not caller["child"]:
        return None
    if caller["role"] is None and not caller["foreign"] and not _thaliris_typed(payload.get("agent_type")):
        return None
    reason = guards.mcp_denial(tool, caller["writes"])
    return _deny(reason) if reason else None


def pre_tool_use(payload: dict) -> dict | None:
    tool, tool_input = payload.get("tool_name"), payload.get("tool_input")
    if isinstance(tool, str) and tool.startswith("mcp__"):
        return _mcp(payload, tool)
    if not isinstance(tool_input, dict):
        return None
    if tool == "SendMessage":
        return _message(payload, tool_input)
    if tool == "Skill":
        return _skill(payload, tool_input)
    if tool in AGENT_TOOLS:
        return _dispatch(payload, tool_input)
    if tool in SHELL_TOOLS:
        return _shell(payload, tool_input)
    if tool in FILE_TOOLS:
        return _file(payload, tool_input)
    return None


# ------------------------------------------------------ native lifecycle facts

_COUNT_LIMIT = 10 ** 12


def _count(value: object) -> int | None:
    """A Host-reported non-negative integer, or None (UNKNOWN). Booleans and floats are not counts."""
    if type(value) is int and 0 <= value <= _COUNT_LIMIT:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
        return int(value)
    return None


def _record_usage(association: tuple[Path, str], match, measured: dict, outcome: str) -> dict | None:
    """Record dispatch usage on the matched managed handoff (Core `set_handoff_usage`, one revision).

    Every value the Host did not report stays null (UNKNOWN). Usage is a mechanical record for the Controller's routing;
    it decides nothing. A failure never fails the hook: a bounded diagnostic is noted in the lifecycle instead.
    Returns {"handoff_id", "revision"} when something was recorded.
    """
    try:
        with store.transaction(*association) as ctx:
            entry = match(ctx["lifecycle"])
            if entry is None or not entry.get("handoff_id") or store.handoff(ctx["state"], entry["handoff_id"]) is None:
                return None
            usage = {"model": entry.get("observed_model") or entry.get("requested_model"), "effort": entry.get("requested_effort"),
                     **measured, "outcome": "disposed" if entry["disposed"] else outcome}
            usage = {key: value for key, value in usage.items() if value is not None}
            if entry.get("usage_reported") == usage:
                return None  # the same report again changes nothing (and costs no revision)
            core.set_handoff_usage(ctx["state"], entry["handoff_id"], usage)
            entry["usage_reported"] = usage
            ctx["state_changed"] = ctx["lifecycle_changed"] = True
            return {"handoff_id": entry["handoff_id"], "revision": ctx["state"]["revision"]}
    except Exception as exc:  # noqa: BLE001 - a hook degrades; the Controller can read the lifecycle diagnostic
        diagnostic = f"{type(exc).__name__}: {str(exc)[:200]}"
    try:
        with store.transaction(*association, observe_only=True) as ctx:
            entry = match(ctx["lifecycle"])
            if entry is not None:
                store.note(entry, "usage-not-recorded", error=diagnostic)
    except (ValueError, OSError):
        pass
    return None


def _foreground_usage(response: dict) -> dict:
    """Best effort: the Agent tool result's usage fields are undocumented, so only plainly named counts are read."""
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    measured = {"input_tokens": _count(usage.get("input_tokens")), "output_tokens": _count(usage.get("output_tokens")),
                "total_tokens": _count(response.get("totalTokens")), "duration_ms": _count(response.get("totalDurationMs")),
                "tool_uses": _count(response.get("totalToolUseCount"))}
    return {key: value for key, value in measured.items() if value is not None}


_NOTIFICATION = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
_NOTIFICATION_OUTCOMES = {"completed": "completed", "failed": "failed", "error": "failed"}
NOTIFICATIONS_MAX = 16


def _tag(body: str, name: str) -> str | None:
    found = re.findall(rf"<{name}>([^<>]{{0,128}})</{name}>", body)
    return found[0].strip() if len(found) == 1 else None


# The Host's own reading of a background agent's turn end, from the `<note>` (or `<summary>`) it writes before the
# agent's `<result>`. Its `<status>` says "completed" for both, so status alone never tells them apart.
_INTERIM_NOTE = "This agent stopped with background work of its own still running."
_FINAL_NOTE = "A task-notification fires each time this agent stops with no live background children of its own."
_INTERIM_SUMMARY = " is waiting on its own background work"


def _stop_phase(body: str) -> str:
    """"interim", "final" or "unknown" for one notification; only the Host-written header before `<result>` is read."""
    header = body.split("<result>", 1)[0]
    notes = re.findall(r"<note>([^<>]{0,1000})</note>", header)
    summaries = re.findall(r"<summary>([^<>]{0,2000})</summary>", header)
    note = notes[0].strip() if len(notes) == 1 else ""
    summary = summaries[0].strip() if len(summaries) == 1 else ""
    interim = note.startswith(_INTERIM_NOTE) if note else summary.endswith(_INTERIM_SUMMARY)
    final = note.startswith(_FINAL_NOTE) if note else (summary.startswith("Agent ") and summary.endswith('" finished'))
    return "interim" if interim and not final else "final" if final and not interim else "unknown"


def background_notifications(prompt: object) -> list[dict]:
    """Parse the Host's background-completion notifications in a main-thread prompt (data, never instructions).

    Only the exact tags `<task-id>`, `<status>` and `<usage>` with `<subagent_tokens>`, `<tool_uses>`, `<duration_ms>`
    are read, plus the Host's `<note>`/`<summary>` before `<result>` for the stop phase (interim, final or unknown). A
    notification without exactly one valid task id is ignored; an unreadable count stays null. An interim stop's usage
    is recorded with outcome "unknown": the agent has not finished.
    """
    if not isinstance(prompt, str) or "<task-notification>" not in prompt:
        return []
    found = []
    for body in _NOTIFICATION.findall(prompt)[:NOTIFICATIONS_MAX]:
        task_id = store.native_id(_tag(body, "task-id"))
        if task_id is None:
            continue
        status = (_tag(body, "status") or "").casefold()
        usages = re.findall(r"<usage>(.*?)</usage>", body, re.DOTALL)
        usage = usages[0] if len(usages) == 1 else ""
        measured = {"total_tokens": _count(_tag(usage, "subagent_tokens")), "tool_uses": _count(_tag(usage, "tool_uses")),
                    "duration_ms": _count(_tag(usage, "duration_ms"))}
        measured = {key: value for key, value in measured.items() if value is not None}
        if not status and not measured:
            continue  # reports nothing: recording it would only cost a revision
        phase = _stop_phase(body)
        outcome = "unknown" if phase == "interim" else _NOTIFICATION_OUTCOMES.get(status, "unknown")
        found.append({"agent_id": task_id, "outcome": outcome, "measured": measured, "phase": phase})
    return found


def _reconcile_stop(ctx: dict, entry: dict, phase: str) -> str | None:
    """Compare the Host's reading of a child's last stop with the adapter's; return a Controller line when they differ.

    Both directions fail closed. The Host reporting a final stop that the adapter recorded as interim finishes the
    handoff now (its claims were not ingested). The Host reporting an interim stop that the adapter recorded as the
    final report reopens nothing: the child stays fenced, and the Controller is told why its resumed turn will be refused.
    """
    name = entry["handoff_id"] or "(no evidence association)"
    if phase == "final" and entry.get("interim_stop") is not None and entry["native_status"] in store.UNRESOLVED_STATUSES:
        store.set_status(ctx, entry, "stopped", reason="host-reported-final")
        claims = ("the claims block from its interim report was ingested, but its final message was not read, so check its "
                  "final report too" if (entry.get("claims_source") or {}).get("stop") == "interim"
                  else "its typed claims were not ingested, so read its final report yourself")
        return (f"Thaliris {entry['role']} dispatch {name}: the Host reports it finished with no live background work of its "
                f"own, while the adapter had recorded its last stop as interim. It is now finished and fenced; {claims}.")
    # Markers are entry fields, not events: the event history keeps only the last 16.
    if (phase == "interim" and entry["native_status"] in store.TERMINAL_STATUSES and not entry["disposed"]
            and not entry.get("was_interim") and not entry.get("host_interim_notified")):
        store.note(entry, "host-interim-while-fenced")
        entry["host_interim_notified"] = True
        ctx["lifecycle_changed"] = True
        why = f" ({entry['final_stop_reason']})" if entry.get("final_stop_reason") else ""
        return (f"Thaliris {entry['role']} dispatch {name}: the Host reports this child is waiting on its own background work "
                f"(its result may be interim), but the adapter recorded that stop as its final report{why}. Lifecycle "
                "faults fail closed, so it stays finished: when it resumes, its writes, `observe` and commits are refused. "
                "Its next notification carries its report; dispatch a fresh child for any work it could not finish.")
    return None


def notification_gaps(prompt: object) -> str:
    """Shape check for background-completion notifications: a notification the parser cannot read is UNKNOWN, not absent."""
    if not isinstance(prompt, str) or "<task-notification>" not in prompt:
        return ""
    missing = set()
    for body in _NOTIFICATION.findall(prompt)[:NOTIFICATIONS_MAX]:
        for tag in ("task-id",):
            if store.native_id(_tag(body, tag)) is None:
                missing.add(f"<{tag}>")
    if not missing:
        return ""
    return f"host_hook_shape: UNKNOWN (task-notification lacks or mistypes: {', '.join(sorted(missing))}); its completion and usage are not recorded."


def _output_chars(value: object, depth: int = 0) -> int:
    """Characters of the text a tool result carries (string leaves; keys and structure are not model-visible output)."""
    if isinstance(value, str):
        return len(value)
    if depth >= 8:
        return 0
    if isinstance(value, dict):
        return sum(_output_chars(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return sum(_output_chars(item, depth + 1) for item in value[:10_000])
    return 0


def _lane_count(payload: dict) -> None:
    """Adaptive lane counter: add ceil(chars / 4) of a Controller tool result to the task's sidecar counter.

    The launcher only starts Python when this session's lane marker exists; here the association and the anchored
    mode are re-checked (a stale marker is removed and counts nothing). The counter is advisory input, never a gate,
    and lives outside the ledger (no revision, no authority bytes).
    """
    if payload.get("agent_id") is not None:
        return  # a child's tool output is its own working set, not the Controller's
    session = store.native_id(payload.get("session_id"))
    association = store.resolve_session(session)
    if not store.sync_lane_marker(session, association) or association is None:
        return
    chars = _output_chars(payload.get("tool_response"))
    if chars:
        core.select_task(*association)
        core.task_lane_counter(association[0], add=min(math.ceil(chars / 4), core.LANE_COUNTER_MAX))


def post_tool_use(payload: dict) -> dict | None:
    if payload.get("tool_name") in LANE_TOOLS:
        _lane_count(payload)
        return None
    if payload.get("tool_name") not in AGENT_TOOLS:
        return None
    association = store.resolve_session(payload.get("session_id"))
    if association is None:
        return None
    root, task_id = association
    response = payload.get("tool_response")
    gaps = _shape_gaps("PostToolUse(Agent)", payload, {"tool_use_id": str, "tool_response.status": str})
    response = response if isinstance(response, dict) else {}
    agent_id, status = store.native_id(response.get("agentId")), response.get("status")
    if status == "completed":
        # Before the observe-only pass, so the revision reported below already includes this write.
        measured = _foreground_usage(response)
        if measured:
            _record_usage(association, lambda lifecycle: next((item for item in lifecycle["dispatches"]
                                                               if item["tool_use_id"] == payload.get("tool_use_id")), None),
                          measured, "completed")
    summary, revision = None, None
    with store.transaction(root, task_id, observe_only=True) as ctx:
        revision = ctx["state"]["revision"]
        entry = next((item for item in ctx["lifecycle"]["dispatches"] if item["tool_use_id"] == payload.get("tool_use_id")), None)
        if entry is not None:
            if agent_id:
                store.bind(ctx, entry, agent_id, "exact")
            if isinstance(response.get("resolvedModel"), str):
                entry["observed_model"] = response["resolvedModel"]
            if status == "completed":
                store.set_status(ctx, entry, "completed")
            elif status == "async_launched":
                # The Host may background a call that did not request it; record what happened.
                entry["background"] = True
                store.set_status(ctx, entry, "launched")
            else:
                store.note(entry, "tool-result", status=str(status)[:32])
                ctx["lifecycle_changed"] = True
            store.record_host_facts(ctx, entry, payload.get("transcript_path"))
            summary = entry
    if payload.get("agent_id") is not None:
        return None  # A nested Scanner result belongs to its parent; global evidence stays with the Controller.
    lines = [gaps] if gaps else []
    if summary is not None:
        state = "returned its final report (native execution completed)" if summary["native_status"] == "completed" else (
            "was launched in the background; its completion notification is the result, not this acknowledgement"
            if status == "async_launched" else f"reported status {status!r}")
        lines.append(f"Thaliris {summary['role']} dispatch {summary['handoff_id'] or '(no evidence association)'} {state}. "
                     f"Task revision now {revision}: pass it as `--base-revision` to your next `judge`, `task-update` or `close`. "
                     "Native completion is execution termination only; you judge the result against the requirements.")
        if summary["handoff_id"] is None:
            lines.append("This dispatch has no evidence association, so its observations could not persist. "
                         "If they matter, import them with `observe --handoff-id` after `register`ing a handoff.")
    evidence = evidence_state(root, task_id)
    if evidence:
        # A child's final report is where the Controller judges: always show the packet (and remember it). A background
        # launch acknowledgement carries no result, and several parallel launches would repeat the same packet: show
        # it only when it differs from the one this Controller last saw (UserPromptSubmit shares the digest).
        is_new = store.packet_is_new(payload.get("session_id"), evidence["digest"])
        if is_new or status != "async_launched":
            lines.append(evidence["text"])
        else:
            lines.append(f"Evidence unchanged: {evidence['unincorporated']} unincorporated, {evidence['unresolved']} unresolved, "
                         f"task revision now {evidence['revision']}.")
    return _context("PostToolUse", "\n".join(lines)) if lines else None


def post_tool_use_failure(payload: dict) -> dict | None:
    if payload.get("tool_name") not in AGENT_TOOLS:
        return None
    association = store.resolve_session(payload.get("session_id"))
    if association is None:
        return None
    with store.transaction(*association, observe_only=True) as ctx:
        for entry in ctx["lifecycle"]["dispatches"]:
            if entry["tool_use_id"] == payload.get("tool_use_id"):
                # A spawn error does not prove that no child exists or that it stopped.
                store.note(entry, "tool-error", interrupt=bool(payload.get("is_interrupt")), error=str(payload.get("error"))[:200])
                ctx["lifecycle_changed"] = True
    return None


def _managed_child_type(payload: dict, association: tuple[Path, str] | None) -> bool:
    """A Thaliris agent type now, or one this session's task already dispatched (its snapshot governs)."""
    if roles.role_for_agent_type(payload.get("agent_type")) is not None:
        return True
    if association is None:
        return False
    try:
        lifecycle = store.read_lifecycle(*association)
    except (ValueError, OSError):
        return False
    agent_id, agent_type = store.native_id(payload.get("agent_id")), payload.get("agent_type")
    return any(item.get("subagent_type") == agent_type and (
        item.get("agent_id") == agent_id or (item.get("agent_id") is None and item["native_status"] in store.UNRESOLVED_STATUSES))
        for item in lifecycle["dispatches"])


def subagent_start(payload: dict) -> dict | None:
    agent_id = store.native_id(payload.get("agent_id"))
    association = store.resolve_session(payload.get("session_id"))
    gaps = _shape_gaps("SubagentStart", payload, {"agent_id": str, "agent_type": str})
    if gaps and (association is not None or roles.role_for_agent_type(payload.get("agent_type")) is not None):
        # The child cannot be bound without these fields: say so, and bind nothing (the fail-closed gates stay as they are).
        return _context("SubagentStart", gaps + " Keep decision-changing observations, with their sources, in your final report.")
    if agent_id is None or not _managed_child_type(payload, association):
        return None
    lines = [f"Thaliris runner for this session: `{runner_command()}` (run it as one direct command)."]
    entry, closed = None, None
    if association is not None:
        with store.transaction(*association, observe_only=True) as ctx:
            # Exact Host record only. "The only pending reservation of this role" is a
            # guess and once misattributed evidence to the wrong handoff (H144).
            entry = store.bind_from_host(ctx, agent_id, payload.get("agent_type"), payload.get("transcript_path"))
            if entry is not None:
                closed = ("was disposed by the Controller" if entry["disposed"]
                          else "already returned its final report" if entry["native_status"] in store.TERMINAL_STATUSES else None)
                store.set_status(ctx, entry, "running")  # a finished or disposed handoff keeps its status (event only)
                record = store.handoff(ctx["state"], entry["handoff_id"])
                if closed:
                    lines.append(f"Your Thaliris handoff {entry['handoff_id'] or '(no evidence association)'} {closed}. It grants "
                                 "no `observe` and no writes to a resumed or restarted agent; new work needs a fresh dispatch "
                                 "from the Controller. Answer your parent in text only.")
                elif record is not None:
                    lines.append(f"Your Thaliris handoff is {record['id']} (candidate {record['candidate']}, "
                                 f"requirement refs {record['requirement_refs'] or 'none — unassigned Scanner evidence'}). "
                                 f"Record decision-changing local facts with `{runner_command()} observe --text \"<what you found>\" "
                                 "--source \"<path:line, URL or command>\"` (one direct command; --source is required and may repeat).")
    if entry is None:
        lines.append("Your evidence association is confirmed from the Host's own record when you first run the runner. "
                     "If `observe` refuses, keep decision-changing observations, with their sources, in your final report.")
    elif entry.get("handoff_id") is None and not closed:
        lines.append("This dispatch carries no evidence association. Keep decision-changing observations, with their sources, in your final report.")
    return _context("SubagentStart", "\n".join(lines))


# ------------------------------------------- interim turn ends: a child waiting on its own background work
#
# A background child that ends a turn while background work it launched is still in flight has not returned its final
# report: the Host keeps it alive and resumes it when that work reports (its main-thread notification then says the
# agent "is waiting on its own background work" and "the result below may be interim"). SubagentStop carries the
# session's in-flight tasks (`background_tasks`: id, type, status; no owner), and the child's own transcript holds
# the Host-written tool results of the launches it made. A stop is interim only when both agree: a task the child
# launched is in flight. Anything else, including an unreadable or unexpected signal, is today's final report.

_BACKGROUND_RUN = re.compile(r"Command running in background with ID: ([A-Za-z0-9_-]{1,64})\.")
_MONITOR_STARTED = re.compile(r"Monitor started \(task ([A-Za-z0-9_-]{1,64})[,)]")
# The Host lists only in-flight tasks (running or pending, not foreground). A Monitor task is a shell task kind.
LIVE_TASK_STATUSES = frozenset({"running", "pending"})
OWN_TASK_TYPES = frozenset({"shell", "monitor"})
TRANSCRIPT_MAX_BYTES = 64 * 1024 * 1024


def _agent_transcript(payload: dict, agent_id: str) -> Path | None:
    value = payload.get("agent_transcript_path")
    if isinstance(value, str) and value:
        return Path(value)
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        return Path(transcript).with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
    return None


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and isinstance(content[0], dict) and isinstance(content[0].get("text"), str):
        return content[0]["text"]
    return ""


def own_background_launches(path: Path | None) -> set[str] | None:
    """Ids of the background shell and Monitor tasks a child launched, read from the Host's tool results in its transcript.

    Only a result that the child's own output cannot write counts, matched to the call that made it: a Bash/PowerShell
    call with `run_in_background` ("Command running in background with ID: …", returned before the command prints
    anything) or a Monitor start. A foreground command the Host moves to the background on its timeout is not counted:
    that tool result is the command's output, which the child controls, so such a stop stays a final report. The
    transcript format is undocumented: None (UNKNOWN) when it cannot be read or is larger than the bound; a line that
    does not parse is skipped.
    """
    if path is None:
        return None
    try:
        if path.stat().st_size > TRANSCRIPT_MAX_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    calls: dict[str, tuple[object, dict]] = {}
    launched: set[str] = set()
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("message") if isinstance(record, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if record.get("type") == "assistant" and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                calls[block["id"]] = (block.get("name"), block["input"] if isinstance(block.get("input"), dict) else {})
            elif record.get("type") == "user" and block.get("type") == "tool_result" and block.get("is_error") is not True:
                name, tool_input = calls.get(block.get("tool_use_id"), (None, {}))
                if name in SHELL_TOOLS and tool_input.get("run_in_background") is True:
                    pattern = _BACKGROUND_RUN
                elif name == "Monitor":
                    pattern = _MONITOR_STARTED
                else:
                    continue
                found = pattern.match(_result_text(block.get("content")))
                if found:
                    launched.add(found.group(1))
    return launched


def own_live_background_work(payload: dict, agent_id: str) -> tuple[list[str], str | None]:
    """The child's own in-flight background tasks at this stop, and a diagnostic when that cannot be established.

    Returns (ids, None) when the Host lists in flight a task the child launched; ([], None) when it lists none;
    ([], diagnostic) when the signal is ambiguous, which the caller treats as a final report (fail closed).
    """
    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list) or not all(isinstance(item, dict) for item in tasks):
        launched = own_background_launches(_agent_transcript(payload, agent_id))
        if launched:
            return [], (f"host_hook_shape: UNKNOWN (SubagentStop background_tasks absent or mistyped). The child launched "
                        f"background task(s) {', '.join(sorted(launched)[:8])} whose liveness is UNKNOWN, so this stop was "
                        "treated as its final report.")
        return [], None
    listed = {item["id"]: item for item in tasks if isinstance(item.get("id"), str) and item["id"] != agent_id
              and item.get("type") in OWN_TASK_TYPES}
    launched = own_background_launches(_agent_transcript(payload, agent_id))
    if launched is None:
        if not listed:
            return [], None  # nothing of a kind the child can own is in flight: an ordinary final report
        return [], (f"{len(listed)} background shell/monitor task(s) were in flight, but the child's transcript could not be "
                    "read, so whether any is its own is UNKNOWN; this stop was treated as its final report.")
    own = sorted(task_id for task_id in listed if task_id in launched)
    if launched and not own:
        # Usually the launches finished long ago. Recorded so that a Host "interim" notification can be explained.
        return [], (f"the child launched background task(s) {', '.join(sorted(launched)[:8])}, but background_tasks lists "
                    "none of them in flight, so this stop was treated as its final report.")
    unexpected = [task_id for task_id in own if listed[task_id].get("status") not in LIVE_TASK_STATUSES]
    if unexpected:
        return [], (f"the child's background task(s) {', '.join(unexpected[:8])} were listed with an unexpected status "
                    f"({', '.join(sorted({str(listed[item].get('status'))[:32] for item in unexpected}))}), so this stop "
                    "was treated as its final report.")
    return own, None


def _interim_stop(association: tuple[Path, str], agent_id: str, payload: dict) -> bool:
    """Record an interim stop when the child's own background work is in flight; True when it was one.

    Only an exactly bound, undisposed, unfinished background dispatch can stop interim (a foreground child's stop
    returns its tool result: the Host treats it as final too). An ambiguous signal leaves a diagnostic on the entry
    (and stderr) and returns False, so the caller records today's final report; so does any failure here.
    """
    try:
        return _record_interim_stop(association, agent_id, payload)
    except Exception as exc:  # noqa: BLE001 - a lifecycle fault fails closed: the stop is recorded as final as before
        print(f"thaliris SubagentStop: interim check failed ({type(exc).__name__}); the stop is treated as final.", file=sys.stderr)
        return False


def _record_interim_stop(association: tuple[Path, str], agent_id: str, payload: dict) -> bool:
    with store.transaction(*association, observe_only=True) as ctx:
        entry = store.bind_from_host(ctx, agent_id, payload.get("agent_type"), payload.get("transcript_path"))
        if entry is None or entry["disposed"] or entry["native_status"] in store.TERMINAL_STATUSES:
            return False
        own, diagnostic = own_live_background_work(payload, agent_id)
        if own and entry["binding"] in store.EXACT_BINDINGS and entry.get("background") is True:
            store.mark_interim(ctx, entry, own)
            try:
                store.keep_interim_claims(entry, payload.get("last_assistant_message"))
            except Exception as exc:  # noqa: BLE001 - the interim stop stands; the final message is still ingested
                store.note(entry, "interim-claims-not-kept", error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return True
        if own:
            diagnostic = (f"the child's own background task(s) {', '.join(own[:8])} were in flight, but its dispatch is "
                          f"{'not exactly bound' if entry['binding'] not in store.EXACT_BINDINGS else 'a foreground dispatch'}, "
                          "so this stop was treated as its final report.")
        if diagnostic:
            store.note(entry, "stop-treated-final", reason=diagnostic[:400])
            entry["final_stop_reason"] = diagnostic[:400]  # a field: the bounded event history may drop the event
            ctx["lifecycle_changed"] = True
            print(f"thaliris SubagentStop: {diagnostic}", file=sys.stderr)
    return False


def subagent_stop(payload: dict) -> dict | None:
    agent_id = store.native_id(payload.get("agent_id"))
    association = store.resolve_session(payload.get("session_id"))
    gaps = _shape_gaps("SubagentStop", payload, {"agent_id": str, "agent_type": str})
    if gaps and association is not None:
        print(f"thaliris {gaps}", file=sys.stderr)  # this event has no context channel; stderr is what hooks already use
    if agent_id is None or association is None or not _managed_child_type(payload, association):
        return None
    if _interim_stop(association, agent_id, payload):
        return None  # not the final report: no claims are ingested and the handoff stays open until the real completion
    message = payload.get("last_assistant_message")
    claims, diagnostic = _ingest_claims(association, agent_id, payload, message)
    with store.transaction(*association, observe_only=True) as ctx:
        entry = store.bind_from_host(ctx, agent_id, payload.get("agent_type"), payload.get("transcript_path"))
        if entry is not None:
            if diagnostic:
                store.note(entry, "claims-not-ingested", error=diagnostic)
            store.set_status(ctx, entry, "stopped", final_message_chars=len(message) if isinstance(message, str) else None,
                             **({"claims": claims} if claims else {}))
            store.record_host_facts(ctx, entry, payload.get("transcript_path"))
    return None


def _ingest_claims(association: tuple[Path, str], agent_id: str, payload: dict, message: object) -> tuple[str | None, str | None]:
    """Ingest the typed claims of an exactly bound, still running managed handoff, before it is marked stopped.

    Core turns adverse claims into open observations under the handoff's producer and counts supports; a missing or
    malformed block leaves only Core's marker. Nothing here judges a claim. When the message has no readable block and
    an interim stop of the same handoff kept one, that kept block is ingested instead, and its provenance is recorded on
    the entry (`claims_source`). Returns (claims status, diagnostic); any failure is a bounded diagnostic, never a
    failed or blocking hook.
    """
    try:
        with store.transaction(*association) as ctx:  # ledger write: authority check, then re-anchor on change
            entry = store.bind_from_host(ctx, agent_id, payload.get("agent_type"), payload.get("transcript_path"))
            if (entry is None or entry["binding"] not in store.EXACT_BINDINGS or not entry.get("handoff_id") or entry["disposed"]
                    or entry["native_status"] in store.TERMINAL_STATUSES or store.handoff(ctx["state"], entry["handoff_id"]) is None):
                return None, None
            kept = entry.get("interim_claims") if isinstance(entry.get("interim_claims"), dict) else None
            if kept and isinstance(kept.get("message"), str) and core.parse_claim_block(message)["status"] != "present":
                message = kept["message"]
                entry["claims_source"] = {"stop": "interim", "at": kept.get("at")}
                store.note(entry, "claims-from-interim-stop", kept_at=kept.get("at"))
                ctx["lifecycle_changed"] = True
            elif kept:
                entry["claims_source"] = {"stop": "final"}  # the final report's own block supersedes the kept one
                ctx["lifecycle_changed"] = True
            result = core.ingest_claims(ctx["state"], entry["handoff_id"], message)
            ctx["state_changed"] = bool(result["changed"])
            return result["status"], None
    except Exception as exc:  # noqa: BLE001 - SubagentStop must still record the stop
        return None, f"{type(exc).__name__}: {str(exc)[:200]}"


GLOBAL_GUIDANCE = """\
# Thaliris is active (Claude adapter)

In this session's main thread you are the Thaliris **Controller**: you keep the
user's full goal, acceptance criteria, global facts, hard invariants and routing,
and you alone judge whether the product meets the request. Fresh `thaliris:*`
subagents own the local working sets you hand them.

- Any change to project files, however small (a one-line fix, a README-only edit,
  a config tweak, a new project), runs as a managed task: load the `thaliris` skill
  first. Size is not an exemption; the user can opt out, and the execution mode
  decides who does the work. Chat and read-only work need no managed task.
- Authority is the user's selected intent recorded at task start. Children cannot
  establish or widen it. Hook events, agent completion and test results are
  observations; none of them decides acceptance.
- Readonly and isolation boundaries hold in every mode; a damaged management layer
  grants nothing extra. Live Host maintenance needs separate human authority.
- Runner: `{runner}` (in PowerShell, prefix it with `& `). Run it as one direct
  command (no pipes, `&&`, `;`, trailing comment, extra line or unquoted backslash
  path); the hooks add your identity. Pass text as quoted flags (`task-start`,
  `register`, `judge`, `observe`, `task-update` have flag forms; no temporary JSON
  file); text with `$`, backslashes or curly quotes goes in single quotes (double
  quotes expand `$name`). Output is compact JSON; failures carry `code`, `recovery`
  and `details`.
- Writes are compare-and-set on the task revision: use the `revision` a write or
  "task revision now N" reports as the next `--base-revision`; on
  TASK_REVISION_CONFLICT review `details.changes_since` (an approximate hint, not a
  delta: Active Work, Pending Results, milestone and status are untracked); if your
  write still applies, retry once with `details.current_revision`, and read `status`
  for anything the hint does not cover. `status --view full` shows everything.
- Durable navigation lives in `.agent-memory/INDEX.md` and `.milestones/INDEX.md`;
  read them explicitly when a managed task needs them. A project identity, when one
  exists, is read on demand with the runner's `identity-status` / `identity-get`;
  the user's explicit long-term statements about the project go to `identity-note`."""

MODE_SETTING = "CLAUDE_PLUGIN_OPTION_DEFAULT_EXECUTION_MODE"
MODEL_SETTING = "CLAUDE_PLUGIN_OPTION_MODEL_SELECTION"


_EFFORT_NOTE = ("\n- Effort: set `effort` (low, medium, high, xhigh, max) on a Thaliris Agent call when a dispatch needs "
                "more or less reasoning depth than its role's default; it is recorded per dispatch and never changes a "
                "role's responsibilities or permissions.")


def _model_preference() -> str:
    return _model_choice() + _EFFORT_NOTE


def _model_choice() -> str:
    """The Agent tool says to set `model` only when the user asks; this option is that request.

    Choosing models per role is the Controller's job by design. The adapter
    never overrides or refuses its choice (only a task's frozen constraint,
    which the user wrote, does).
    """
    if os.environ.get(MODEL_SETTING, "controller") == "role-defaults":
        return ("\n- Model selection (plugin option `model_selection`): the user prefers each role's default model; "
                "set `model` only when they ask for one. Exception, the user's frontend rule: this is the user "
                "explicitly asking you to set `model` for substantive frontend work (new UI or layout, component "
                "architecture, responsive behavior, interaction, animation, creative SVG, nontrivial visual design, "
                "even when the diff is small), which goes to `opus` or a stronger available model; only a genuinely "
                "trivial, fully specified edit such as changing a colour or a text, with no design or behavior "
                "decision, may use a light model. Route by the described work and the decisions it needs, not by "
                "keyword, file-count or diff-size heuristics. If a frozen `claude-model:` constraint prevents this, "
                "report the conflict instead of silently downgrading. Every other dispatch uses the role defaults.")
    return ("\n- Model selection (plugin option `model_selection` = controller): the user asks you to choose the model "
            "of every Thaliris role per dispatch, so set `model` on Agent calls as the work requires; this is the user "
            "explicitly asking you to set it. Division of labor and cognitive isolation come first, never traded for cost; "
            "then pick the cheapest fitting model: `haiku` (effort low) for Git closure, CI observation and "
            "deterministic fully specified local work, `sonnet` for clear local work, `opus` for coupled or unclear "
            "work and decision-critical challenges. Substantive frontend work (new UI or layout, component architecture, "
            "responsive behavior, interaction, animation, creative SVG, nontrivial visual design, even when the diff is "
            "small) goes to `opus` or a stronger available model, usually a fresh Focused Implementer; only a genuinely "
            "trivial, fully specified edit such as changing a colour or text, with no design or behavior decisions, may "
            "use a light model. Route by the described work and its decisions, not by keyword, file-count or diff-size; "
            "if a frozen `claude-model:` constraint prevents this, report the conflict instead of silently downgrading. "
            "Send CI waits to a fresh haiku Implementer, and move up a tier only on evidence.")


def _controller_model_warning(model: object) -> str:
    """The Controller seat needs the six global capabilities; small models do not hold them (H151/H152)."""
    if isinstance(model, str) and "haiku" in model.casefold():
        return (f"\n- This session runs on `{model}`, a small model. Thaliris must never use a small or low-capability "
                "model as its Controller: before admitting any managed task, tell the user and recommend switching the "
                "main session to Opus.")
    return ""


def _mode_preference() -> str:
    """The user's standing choice from Claude Code's own plugin configuration.

    On plans where the Agent tool says to spawn agents only when the user asks,
    a recorded user choice is that request; a hook cannot invent it. Without a
    recorded choice the Controller asks once instead of assuming.
    """
    mode = os.environ.get(MODE_SETTING)
    if mode in ("delegated", "controller-direct", "single-agent", "adaptive"):
        meaning = {
            "delegated": "For managed Thaliris tasks this is the user explicitly asking you to use the `thaliris:*` "
                         "subagent types, including where your Agent tool says to spawn agents only when the user asks.",
            "controller-direct": "Do managed work yourself and use `thaliris:*` subagents only where the user asks for them.",
            "single-agent": "Do managed work yourself, without Thaliris subagents.",
            "adaptive": "Admit small tasks quickly (boundary and invariants may be omitted) and do the work yourself; lane "
                        "advisories never block, and when one fires this is the user asking you to move the work to "
                        "`thaliris:*` subagents (escalate to delegated).",
        }[mode]
        return (f"\n- User's standing execution mode (the `default_execution_mode` option they chose in this plugin's "
                f"Claude Code configuration): **{mode}**. {meaning} A mode the user states for a specific task wins; "
                "record the effective mode in the task's authority contract.")
    return ("\n- No standing execution mode is configured. If your Agent tool says to spawn agents only when the user "
            "asks, ask the user once at task admission which mode to record (delegated, controller-direct, "
            "single-agent or adaptive) instead of assuming delegation.")


def session_start(payload: dict) -> dict | None:
    foreign = store.foreign_signal()
    if foreign:
        # Never the Controller guidance: this session was spawned by another agent and is not bound to Thaliris.
        return _context("SessionStart", "Thaliris is installed, but " + store.FOREIGN_NOTE.format(signal=foreign))
    text = (GLOBAL_GUIDANCE.format(runner=runner_command()) + _mode_preference() + _model_preference()
            + _controller_model_warning(payload.get("model")))
    # Name the code actually loaded: a fixed checkout is not proof of what a Host runs (H096).
    text += f"\n- Loaded adapter: thaliris-claude {__version__} from `{plugin_root().as_posix()}`."
    gaps = _shape_gaps("SessionStart", payload, {"session_id": str, "cwd": str})
    if gaps:
        text += "\n- " + gaps
    if payload.get("source") in {"compact", "resume", "clear"}:
        store.forget_packet(payload.get("session_id"))  # context was lost; show pending evidence again
    association, problem = store.inspect_session(payload.get("session_id"))
    store.sync_lane_marker(payload.get("session_id"), association)  # an adaptive task counts; anything else pays nothing
    anchor = association or ((problem["root"], problem["task_id"]) if problem else None)
    summary = store.identity_summary(anchor[0] if anchor else store.project_root(Path(payload.get("cwd") or ".")))
    if summary:  # navigation only; nothing at all when the project has no identity
        text += f"\n- {summary}"
    if payload.get("source") in {"startup", "resume"}:
        # A process start fixes which agent directories the Host watches (see `register-role`).
        store.note_session_agents_dir(payload.get("session_id"), anchor[0] if anchor
                                      else store.project_root(Path(payload.get("cwd") or ".")))
    if problem is not None:
        # The session's task exists but cannot be read: say so instead of presenting a session with no task.
        text += (f"\n\nThis session is associated with a Thaliris {store.problem_text(problem)} Managed Thaliris dispatches are "
                 "refused for this session until it is readable; your own reads and writes are unaffected.")
    if association is not None:
        root, task_id = association
        try:
            with store.transaction(root, task_id, observe_only=True) as ctx:
                record = ctx["store"].read() or {}
                mode = record.get("contract", {}).get("execution_mode", "UNKNOWN")
                pending = len(store.unresolved(ctx["lifecycle"]))
                revision = ctx["state"]["revision"]
                context = store.task_context_text(ctx["state"])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            text += (f"\n\nThis session's associated task {task_id} could not be read ({_error_code(exc)}: {str(exc)[:200]}); "
                     "managed assurance is UNKNOWN, not safe. Child writes and messages are refused while the lifecycle is "
                     "unreadable; your own work is unaffected. Read `status` for the diagnosis and `guidance --section recovery`; "
                     "the user decides whether to repair it or start an independent new task.")
        else:
            text += (f"\n\nThis session is associated with ACTIVE Thaliris task `{task_id}` in `{root}` "
                     f"(mode {mode}, revision {revision}, {pending} unresolved child dispatch(es)). Continue it or abandon it "
                     "explicitly. If the skill guidance is no longer in context (for example after compaction), load the "
                     "`thaliris` skill again before routing.")
            if context:
                text += "\n\n" + context
    return _context("SessionStart", text)


def user_prompt_submit(payload: dict) -> dict | None:
    """Background results arrive as new main-thread turns, not as PostToolUse.

    Re-present the bounded evidence packet at the start of a Controller turn when
    it changed since the session last saw it; an unchanged packet repeated every
    turn is noise, not continuity.
    """
    if payload.get("agent_id") is not None or store.foreign_signal():
        return None
    association = store.resolve_session(payload.get("session_id"))
    if association is not None:
        # The Host writes a child's actual tool set after it stops; background
        # children have no later tool result, so collect it on the next turn.
        with store.transaction(*association, observe_only=True) as ctx:
            for entry in ctx["lifecycle"]["dispatches"]:
                if entry.get("agent_id") and entry["native_status"] in store.TERMINAL_STATUSES and not entry.get("observed_tools"):
                    store.record_host_facts(ctx, entry, payload.get("transcript_path"))
    recorded, reconciled = [], []
    if association is not None:
        notices = background_notifications(payload.get("prompt"))
        finals = [notice["agent_id"] for notice in notices if notice["phase"] == "final"]
        if finals:
            # A final stop the adapter saw as interim: ingest the claims block it kept, before the entry is finished.
            lifecycle = store.read_lifecycle(*association)
            for agent in finals:
                entry = store.dispatch_for_agent(lifecycle, agent)
                if entry is not None and entry.get("interim_stop") is not None and entry.get("interim_claims"):
                    _ingest_claims(association, agent, {}, None)
        if any(notice["phase"] != "unknown" for notice in notices):
            with store.transaction(*association, observe_only=True) as ctx:
                for notice in notices:
                    entry = store.dispatch_for_agent(ctx["lifecycle"], notice["agent_id"])
                    line = _reconcile_stop(ctx, entry, notice["phase"]) if entry is not None else None
                    if line:
                        reconciled.append(line)
        for notice in notices:
            done = _record_usage(association, lambda lifecycle, agent=notice["agent_id"]: store.dispatch_for_agent(lifecycle, agent),
                                 notice["measured"], notice["outcome"])
            if done:
                recorded.append(done)
    lines = [f"Thaliris recorded dispatch usage for handoff(s) {', '.join(item['handoff_id'] for item in recorded)}; "
             f"task revision now {recorded[-1]['revision']}."] if recorded else []
    lines += reconciled
    gaps = notification_gaps(payload.get("prompt")) if association is not None else ""
    if gaps:
        lines.append(gaps)
    evidence = evidence_state(*association) if association else None
    if evidence and store.packet_is_new(payload.get("session_id"), evidence["digest"]):
        lines.append(evidence["text"])
    return _context("UserPromptSubmit", "\n".join(lines)) if lines else None


HANDLERS = {
    "PreToolUse": pre_tool_use, "PostToolUse": post_tool_use, "PostToolUseFailure": post_tool_use_failure,
    "SubagentStart": subagent_start, "SubagentStop": subagent_stop, "SessionStart": session_start,
    "UserPromptSubmit": user_prompt_submit,
}


def _load_project_roles(payload: dict) -> None:
    """Make the project's Controller-registered roles resolvable for this event."""
    association = store.resolve_session(payload.get("session_id"))
    root = association[0] if association else store.project_root(Path(payload.get("cwd") or "."))
    roles.load_custom_roles(store.custom_role_records(root) if store.is_managed_project(root) else {})


def _fail_closed(event: str, payload: dict) -> bool:
    """A crashed hook must not silently admit a managed Thaliris dispatch."""
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    return (event == "PreToolUse" and payload.get("tool_name") in AGENT_TOOLS
            and roles.role_for_agent_type(tool_input.get("subagent_type")) is not None
            and store.read_session(payload.get("session_id")) is not None)


def main(event: str) -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read() or b"{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict) or event not in HANDLERS:
        return 0
    started, error = time.perf_counter(), None
    try:
        _load_project_roles(payload)
        result = HANDLERS[event](payload)
    except Exception as exc:  # Hooks degrade to transparent, except managed dispatch.
        error = f"{type(exc).__name__}: {exc}"
        print(f"thaliris hook {event} failed: {error}", file=sys.stderr)
        result = _deny(f"Thaliris hook failure ({type(exc).__name__}); managed dispatch assurance is UNKNOWN.") if _fail_closed(event, payload) else None
    _probe_log(event, payload, result, error, started)
    if result is not None:
        # ASCII-only JSON: valid for any decoder, so no console code page (cp1252 when piped on Windows) can make the
        # hook crash after the work is done and silently drop the output (including the appended --native-session).
        sys.stdout.write(json.dumps(result, ensure_ascii=True))
    return 0


def _probe_log(event: str, payload: dict, result: dict | None, error: str | None, started: float) -> None:
    """Opt-in Host probe record. It contains raw payloads (prompts, commands): use only in throwaway repos."""
    target = os.environ.get("THALIRIS_CLAUDE_PROBE_LOG")
    if not target:
        return
    line = {"at": time.time(), "event": event, "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            "launcher_started_at": os.environ.get("THALIRIS_CLAUDE_LAUNCHED_AT"), "python": sys.version.split()[0],
            "payload": payload, "result": result, "error": error}
    try:
        with open(target, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass
