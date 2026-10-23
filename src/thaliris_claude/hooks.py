"""Claude Code hook handlers.

Hooks bind mechanical identity (session, native agent, handoff), enforce role
and readonly boundaries, and re-present unresolved evidence to the Controller.
Host fields that are missing stay UNKNOWN; hook events never decide acceptance.
"""
from __future__ import annotations

import json
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
_MARKER = re.compile(r"^[ \t]*thaliris-evidence:[ \t]*(\{.*\})[ \t]*$", re.MULTILINE)
_READ_ONLY_CORE = ("task-status", "task-get", "task-show", "catalog", "document-get", "version", "stale", "milestone-check")


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


def _caller(payload: dict) -> tuple[bool, str | None, str | None]:
    """Return (is_child, Thaliris role or None, native agent id).

    `agent_id` is present only inside a subagent; the main thread is the
    Controller position. A child with a generic agent type has no Thaliris role.
    """
    if payload.get("agent_id") is None:
        return False, "controller", None
    return True, roles.role_for_agent_type(payload.get("agent_type")), store.native_id(payload.get("agent_id"))


def evidence_packet(root: Path, task_id: str) -> str | None:
    core.select_task(root, task_id)
    status = core.task_status(root)
    continuity = status["Evidence Continuity"]
    if not (continuity["unincorporated_total"] or continuity["unresolved_judgments_total"]):
        return None
    lines = [f"Thaliris evidence for task {task_id} (revision {status['Task']['revision']}): "
             f"{continuity['unincorporated_total']} observation(s) not yet incorporated into a requirement judgment, "
             f"{continuity['unresolved_judgments_total']} unresolved repair/unknown judgment(s)."]
    for item in continuity["unincorporated"]:
        lines.append(f"- observation {item['observation_id']} → requirement {item['requirement_ref'] or '(unassigned)'} "
                     f"(candidate {item['candidate']}, handoff {item['handoff_id']})")
    for item in continuity["unresolved_judgments"]:
        lines.append(f"- judgment {item['id']}: {item['outcome']} for {item['requirement_ref']} (candidate {item['candidate']})")
    if continuity["next_offset"] is not None:
        lines.append(f"- … more with `status --evidence-offset {continuity['next_offset']}`")
    lines.append("Before you call a requirement satisfied or close the task, reassess each item for the current candidate "
                 "(satisfied / unknown / repair / no-current-impact) with `judge`. Reading or forwarding it is not a disposition.")
    return "\n".join(lines)


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
    is_child, caller_role, agent_id = _caller(payload)
    target_role = roles.role_for_agent_type(tool_input.get("subagent_type"))
    if target_role is None:
        if is_child and caller_role is not None:
            return _deny("Thaliris children delegate discovery only to a fresh `thaliris:investigator` Scanner; "
                         "generic agents and forks are not Thaliris roles.")
        return None
    if is_child and not roles.delegation_allowed(caller_role or "", target_role):
        return _deny(f"Role {caller_role or 'generic agent'} may not delegate to {target_role}. "
                     "Return the uncovered need to your parent in your final report.")
    if tool_input.get("isolation") == "remote":
        return _deny("Thaliris roles run locally so their identity can be observed; omit `isolation: remote`.")
    association = store.resolve_session(payload.get("session_id"))
    if association is None:
        return None  # No managed task: ordinary native routing, no reservation.
    root, task_id = association
    prompt = tool_input.get("prompt") if isinstance(tool_input.get("prompt"), str) else ""
    updated = dict(tool_input)
    try:
        marker = _marker(prompt)
        with store.transaction(root, task_id) as ctx:
            contract = ctx["authority"]["contract"]
            if contract["execution_mode"] == "single-agent":
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
            alias = store.validate_constraint(contract)
            requested = tool_input.get("model")
            if alias is not None:
                if requested not in (None, alias):
                    raise Refusal(f"The task's frozen execution constraint selects model `{alias}`; per-spawn overrides are refused.")
                updated["model"] = alias
            # Otherwise the model is the Controller's choice; the adapter never overrides or refuses it.
            entry = store.reserve(ctx, role=target_role, subagent_type=tool_input["subagent_type"],
                                  tool_use_id=str(payload.get("tool_use_id") or ""), parent=parent, prompt=prompt,
                                  model=updated.get("model"), background=bool(tool_input.get("run_in_background")),
                                  association=marker)
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


def _child_fenced(payload: dict) -> bool:
    association = store.resolve_session(payload.get("session_id"))
    agent_id = store.native_id(payload.get("agent_id"))
    if association is None or agent_id is None:
        return False
    try:
        entry = store.dispatch_for_agent(store.read_lifecycle(*association), agent_id)
    except (ValueError, OSError):
        return False
    return bool(entry and entry["disposed"])


def _shell(payload: dict, tool_input: dict) -> dict | None:
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    is_child, caller_role, agent_id = _caller(payload)
    invocation = guards.runner_invocation(command, runner_path())
    if invocation is not None:
        if not invocation["direct"]:
            return _deny("Run the Thaliris runner as one direct command, without pipes, `&&`, redirection, variables "
                         "or command substitution. Write JSON payloads to a file in a separate call if quoting is awkward.")
        if invocation["forged_identity"]:
            return _deny("`--native-session` and `--native-agent` are supplied by the adapter hook; remove them.")
        subcommand = invocation["subcommand"]
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
            with store.transaction(*association, observe_only=True) as ctx:
                entry = store.bind_from_host(ctx, agent_id, payload.get("agent_type"), payload.get("transcript_path"))
                if entry is not None and entry["native_status"] == "reserved":
                    store.set_status(ctx, entry, "running")  # the Host record is written after SubagentStart
        suffix = f" --native-session {session}" + (f" --native-agent {agent_id}" if is_child else "")
        output = {"hookEventName": "PreToolUse", "updatedInput": {**tool_input, "command": command.rstrip() + suffix}}
        if _approve_runner(payload, subcommand):
            output.update(permissionDecision="allow",
                          permissionDecisionReason="Thaliris runner, verified as one direct invocation of this plugin's own CLI")
        return {"hookSpecificOutput": output}
    if is_child:
        if guards.direct_core_call(command):
            return _deny("Children use the Thaliris runner, not the raw Core CLI or adapter module.")
        writes = guards.obvious_write(command)
        if writes and caller_role is not None and not roles.repo_write_allowed(caller_role):
            return _deny(f"The {caller_role} role is non-writing; this command looks like a write. Report what should change instead.")
        if writes and guards.mentions_protected(command):
            return _deny("Children cannot change Controller or security state (.context/, Thaliris authority, Claude settings).")
        if writes and _child_fenced(payload):
            return _deny("Your handoff was disposed by the Controller; further writes are fenced.")
        return None
    if store.resolve_session(payload.get("session_id")) is not None:
        if guards.direct_core_call(command) and not any(name in command for name in _READ_ONLY_CORE):
            return _deny("In a managed task, mutate the ledger through the Thaliris runner so authority stays anchored.")
        ledger = (".context/tasks", ".context/state.json", store.LIFECYCLE_NAME, store.ROLES_NAME)
        if guards.obvious_write(command) and any(name in command.replace("\\", "/") for name in ledger):
            return _deny("Hand edits to the task ledger or lifecycle break authority anchoring; use the runner.")
    return None


def _file(payload: dict, tool_input: dict) -> dict | None:
    target = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(target, str):
        return None
    is_child, caller_role, _ = _caller(payload)
    association = store.resolve_session(payload.get("session_id"))
    root = association[0] if association else store.project_root(Path(payload.get("cwd") or "."))
    if is_child:
        if caller_role is not None and not roles.repo_write_allowed(caller_role):
            return _deny(f"The {caller_role} role is non-writing. Report the change you would make instead.")
        if _child_fenced(payload):
            return _deny("Your handoff was disposed by the Controller; further writes are fenced.")
        if store.is_managed_project(root) and guards.protected_target(root, target, controller=False):
            return _deny("Children cannot change Controller or security state (.context/, Claude settings).")
        return None
    if store.is_managed_project(root) and guards.protected_target(root, target, controller=True):
        return _deny("Hand edits to the task ledger or lifecycle break authority anchoring; use the Thaliris runner.")
    return None


def _message(payload: dict, tool_input: dict) -> dict | None:
    """SendMessage resumes even a finished agent.

    An active child may receive supplemental evidence; a child that returned its
    final report, or was disposed, is not reused for new work (H092/H145).
    """
    association = store.resolve_session(payload.get("session_id"))
    target = store.native_id(tool_input.get("to"))
    if association is None or target is None:
        return None
    entry = store.dispatch_for_agent(store.read_lifecycle(*association), target)
    if entry is not None and entry["disposed"]:
        return _deny("That Thaliris handoff was disposed; a late message cannot reactivate it. Start a fresh child for new work.")
    if entry is not None and entry["native_status"] in store.TERMINAL_STATUSES:
        return _deny("That Thaliris child already returned its final report. Dispatch a fresh child with a new handoff "
                     "(you can include the earlier result as selected evidence).")
    return None


def _skill(payload: dict, tool_input: dict) -> dict | None:
    """The Controller skill is the main thread's routine; children keep their role (H143)."""
    if payload.get("agent_id") is None:
        return None
    if str(tool_input.get("skill") or tool_input.get("name") or "").removeprefix(f"{roles.PLUGIN_NAMESPACE}:") == "thaliris":
        return _deny("The thaliris skill is the owning Controller's routine; your role instructions govern this assignment. "
                     "A specific rule can still be read with the runner's `guidance` command.")
    return None


def pre_tool_use(payload: dict) -> dict | None:
    tool, tool_input = payload.get("tool_name"), payload.get("tool_input")
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

def post_tool_use(payload: dict) -> dict | None:
    if payload.get("tool_name") not in AGENT_TOOLS:
        return None
    association = store.resolve_session(payload.get("session_id"))
    if association is None:
        return None
    root, task_id = association
    response = payload.get("tool_response")
    response = response if isinstance(response, dict) else {}
    agent_id, status = store.native_id(response.get("agentId")), response.get("status")
    summary = None
    with store.transaction(root, task_id, observe_only=True) as ctx:
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
    lines = []
    if summary is not None:
        state = "returned its final report (native execution completed)" if summary["native_status"] == "completed" else (
            "was launched in the background; its completion notification is the result, not this acknowledgement"
            if status == "async_launched" else f"reported status {status!r}")
        lines.append(f"Thaliris {summary['role']} dispatch {summary['handoff_id'] or '(no evidence association)'} {state}. "
                     "Native completion is execution termination only; you judge the result against the requirements.")
        if summary["handoff_id"] is None:
            lines.append("This dispatch has no evidence association, so its observations could not persist. "
                         "If they matter, import them with `observe --handoff-id` after `register`ing a handoff.")
    packet = evidence_packet(root, task_id)
    if packet:
        store.packet_is_new(payload.get("session_id"), packet)  # result delivery always shows it; remember it
        lines.append(packet)
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


def subagent_start(payload: dict) -> dict | None:
    role = roles.role_for_agent_type(payload.get("agent_type"))
    agent_id = store.native_id(payload.get("agent_id"))
    if role is None or agent_id is None:
        return None
    lines = [f"Thaliris runner for this session: `{runner_command()}` (run it as one direct command)."]
    association = store.resolve_session(payload.get("session_id"))
    entry = None
    if association is not None:
        with store.transaction(*association, observe_only=True) as ctx:
            # Exact Host record only. "The only pending reservation of this role" is a
            # guess and once misattributed evidence to the wrong handoff (H144).
            entry = store.bind_from_host(ctx, agent_id, payload.get("agent_type"), payload.get("transcript_path"))
            if entry is not None:
                store.set_status(ctx, entry, "running")
                record = store.handoff(ctx["state"], entry["handoff_id"])
                if record is not None:
                    lines.append(f"Your Thaliris handoff is {record['id']} (candidate {record['candidate']}, "
                                 f"requirement refs {record['requirement_refs'] or 'none — unassigned Scanner evidence'}). "
                                 f"Record decision-changing local facts with `{runner_command()} observe --json '<observation JSON>'`.")
    if entry is None:
        lines.append("Your evidence association is confirmed from the Host's own record when you first run the runner. "
                     "If `observe` refuses, keep decision-changing observations, with their sources, in your final report.")
    elif entry.get("handoff_id") is None:
        lines.append("This dispatch carries no evidence association. Keep decision-changing observations, with their sources, in your final report.")
    return _context("SubagentStart", "\n".join(lines))


def subagent_stop(payload: dict) -> dict | None:
    agent_id = store.native_id(payload.get("agent_id"))
    association = store.resolve_session(payload.get("session_id"))
    if agent_id is None or association is None or roles.role_for_agent_type(payload.get("agent_type")) is None:
        return None
    with store.transaction(*association, observe_only=True) as ctx:
        entry = store.bind_from_host(ctx, agent_id, payload.get("agent_type"), payload.get("transcript_path"))
        if entry is not None:
            message = payload.get("last_assistant_message")
            store.set_status(ctx, entry, "stopped", final_message_chars=len(message) if isinstance(message, str) else None)
            store.record_host_facts(ctx, entry, payload.get("transcript_path"))
    return None


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
  command; the hooks add your identity.
- Durable navigation lives in `.agent-memory/INDEX.md` and `.milestones/INDEX.md`;
  read them explicitly when a managed task needs them."""

MODE_SETTING = "CLAUDE_PLUGIN_OPTION_DEFAULT_EXECUTION_MODE"
MODEL_SETTING = "CLAUDE_PLUGIN_OPTION_MODEL_SELECTION"


def _model_preference() -> str:
    """The Agent tool says to set `model` only when the user asks; this option is that request.

    Choosing models per role is the Controller's job by design. The adapter
    never overrides or refuses its choice (only a task's frozen constraint,
    which the user wrote, does).
    """
    if os.environ.get(MODEL_SETTING, "controller") == "role-defaults":
        return ("\n- Model selection (plugin option `model_selection`): the user prefers each role's default model; "
                "set `model` only when they ask for one.")
    return ("\n- Model selection (plugin option `model_selection` = controller): the user asks you to choose the model "
            "of every Thaliris role per dispatch, so set `model` on Agent calls as the work requires; this is the user "
            "explicitly asking you to set it. For a Focused Implementer, `opus` suits coupled work that crosses modules or "
            "contracts, involves concurrency, lifecycle or timing, has an unclear causal model, or follows a failed "
            "attempt or a decision-basis finding; `sonnet` suits local coupling with a clear causal model.")


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
    if mode in ("delegated", "controller-direct", "single-agent"):
        meaning = {
            "delegated": "For managed Thaliris tasks this is the user explicitly asking you to use the `thaliris:*` "
                         "subagent types, including where your Agent tool says to spawn agents only when the user asks.",
            "controller-direct": "Do managed work yourself and use `thaliris:*` subagents only where the user asks for them.",
            "single-agent": "Do managed work yourself, without Thaliris subagents.",
        }[mode]
        return (f"\n- User's standing execution mode (the `default_execution_mode` option they chose in this plugin's "
                f"Claude Code configuration): **{mode}**. {meaning} A mode the user states for a specific task wins; "
                "record the effective mode in the task's authority contract.")
    return ("\n- No standing execution mode is configured. If your Agent tool says to spawn agents only when the user "
            "asks, ask the user once at task admission which mode to record (delegated, controller-direct or "
            "single-agent) instead of assuming delegation.")


def session_start(payload: dict) -> dict | None:
    text = (GLOBAL_GUIDANCE.format(runner=runner_command()) + _mode_preference() + _model_preference()
            + _controller_model_warning(payload.get("model")))
    # Name the code actually loaded: a fixed checkout is not proof of what a Host runs (H096).
    text += f"\n- Loaded adapter: thaliris-claude {__version__} from `{plugin_root().as_posix()}`."
    if payload.get("source") in {"compact", "resume", "clear"}:
        store.forget_packet(payload.get("session_id"))  # context was lost; show pending evidence again
    association = store.resolve_session(payload.get("session_id"))
    if association is not None:
        root, task_id = association
        try:
            with store.transaction(root, task_id, observe_only=True) as ctx:
                record = ctx["store"].read() or {}
                mode = record.get("contract", {}).get("execution_mode", "UNKNOWN")
                pending = len(store.unresolved(ctx["lifecycle"]))
                revision = ctx["state"]["revision"]
        except (ValueError, OSError) as exc:
            text += f"\n\nThis session's associated task {task_id} could not be read ({exc}); managed assurance is UNKNOWN."
        else:
            text += (f"\n\nThis session is associated with ACTIVE Thaliris task `{task_id}` in `{root}` "
                     f"(mode {mode}, revision {revision}, {pending} unresolved child dispatch(es)). Continue it or abandon it "
                     "explicitly. If the skill guidance is no longer in context (for example after compaction), load the "
                     "`thaliris` skill again before routing.")
    return _context("SessionStart", text)


def user_prompt_submit(payload: dict) -> dict | None:
    """Background results arrive as new main-thread turns, not as PostToolUse.

    Re-present the bounded evidence packet at the start of a Controller turn when
    it changed since the session last saw it; an unchanged packet repeated every
    turn is noise, not continuity.
    """
    if payload.get("agent_id") is not None:
        return None
    association = store.resolve_session(payload.get("session_id"))
    if association is not None:
        # The Host writes a child's actual tool set after it stops; background
        # children have no later tool result, so collect it on the next turn.
        with store.transaction(*association, observe_only=True) as ctx:
            for entry in ctx["lifecycle"]["dispatches"]:
                if entry.get("agent_id") and entry["native_status"] in store.TERMINAL_STATUSES and not entry.get("observed_tools"):
                    store.record_host_facts(ctx, entry, payload.get("transcript_path"))
    packet = evidence_packet(*association) if association else None
    if not packet or not store.packet_is_new(payload.get("session_id"), packet):
        return None
    return _context("UserPromptSubmit", packet)


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
        sys.stdout.write(json.dumps(result, ensure_ascii=False))
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
