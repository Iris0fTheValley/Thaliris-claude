"""The Thaliris runner for Claude Code.

Identity flags (`--native-session`, `--native-agent`) are appended by the
PreToolUse hook from Host fields; a model never supplies them. Direct shared-OS
invocation is not authentication, and missing identity stays UNKNOWN.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

from thaliris import authority as core_authority, core, identity, identity_basis

from . import __version__, hooks, inputs, roles, store

_IDENTITY_READS = frozenset({"identity-status", "identity-get", "identity-trace"})
_IDENTITY_MUTATIONS = frozenset({"identity-amend", "identity-challenge", "identity-relate"})
_ROLE_TABLE_BEGIN, _ROLE_TABLE_END = "<!-- thaliris-roles:begin -->", "<!-- thaliris-roles:end -->"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _parser() -> argparse.ArgumentParser:
    # No abbreviations: `--native-s x` must not silently become an identity flag (H023).
    common = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    common.add_argument("--native-session", help=argparse.SUPPRESS)
    common.add_argument("--native-agent", help=argparse.SUPPRESS)
    common.add_argument("--root", type=Path, help="project root (defaults to the session's task or the cwd)")
    common.add_argument("--task-id", help="explicit task for read-only diagnostics")
    parser = _Parser(prog="thaliris", description="Thaliris runner for Claude Code", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, parents=[common], help=help_text, allow_abbrev=False)

    add("guidance", "print Controller guidance or one reference section").add_argument("--section")
    add("bootstrap", "initialize the project for managed work and report navigation")
    command = add("task-start", "admit a managed task from a human-selected authority contract (a file, or the contract flags)")
    command.add_argument("goal")
    command.add_argument("--contract", help="path to a JSON authority contract (UTF-8, UTF-8 BOM or UTF-16 BOM); "
                                            "alternatively give the contract flags, never both")
    inputs.add_flags(command, "task-start")
    command = add("attach", "associate this session with an existing ACTIVE task")
    command.add_argument("--reason", required=True)
    command = add("status", "bounded routing and evidence status (compact by default; --view full for every section)")
    command.add_argument("--view", choices=("compact", "full"), default="compact")
    command.add_argument("--evidence-offset", type=int, default=0, help="page open evidence from this offset")
    command.add_argument("--since-revision", type=int, help="also list what may have changed after this task revision (bounded hint)")
    add("get", "retrieve one task object by id").add_argument("id")
    add("catalog", "list durable navigation").add_argument("path", nargs="?")
    add("document-get", "read 1-8 selected durable documents").add_argument("paths", nargs="+")
    for name, help_text in (("register", "register requirements or legacy handoff associations"),
                            ("judge", "record a Controller requirement judgment"),
                            ("promote", "promote selected records to .agent-memory"),
                            ("document-maintain", "maintain selected durable documents")):
        command = add(name, help_text)
        command.add_argument("--base-revision", type=int, required=True)
        _payload_args(command)
        if name in inputs.FLAGS:
            inputs.add_flags(command, name)
    add("identity-status", "project identity state, items and open challenges (read-only; absent identity is normal)")
    command = add("identity-get", "read the recorded project identity, selected items, one amendment or one challenge")
    command.add_argument("--version", type=int)
    command.add_argument("--item", action="append")
    command.add_argument("--amendment")
    command.add_argument("--challenge")
    command = add("identity-trace", "trace registered relations around one identity-plane ref")
    command.add_argument("ref")
    command.add_argument("--direction", choices=("up", "down", "both"), default="both")
    command.add_argument("--depth", type=int, default=1)
    for name, help_text in (("identity-amend", "record an amendment on the human owner's explicit decision (Controller)"),
                            ("identity-challenge", "open or dismiss a project identity challenge (Controller)"),
                            ("identity-relate", "register or retract purpose relations (Controller)")):
        command = add(name, help_text)
        command.add_argument("--base-revision", type=int, required=True)
        _payload_args(command)
    command = add("identity-note", "record, retract or list verbatim user statements about the project's long-term direction (Controller; no task needed)")
    command.add_argument("--quote", help="the user's verbatim words")
    command.add_argument("--kind", choices=("purpose", "direction", "constraint", "non-goal", "decision", "rejection", "material", "other"))
    command.add_argument("--context", help="one-line Controller framing (stored as Controller-authored)")
    command.add_argument("--ref", action="append", help="repeatable reference, e.g. path:docs/vision.md")
    command.add_argument("--retract", metavar="N-0001", help="retract a wrongly recorded note (needs --reason)")
    command.add_argument("--reason")
    command.add_argument("--list", dest="list_notes", action="store_true", help="list notes (pending flagged)")
    command.add_argument("--include-retracted", action="store_true")
    command = add("identity-suggest", "turn the close-time identity offer on, off or back to default at a scope, on the user's words (Controller)")
    group = command.add_mutually_exclusive_group(required=True)
    group.add_argument("--on", dest="setting", action="store_const", const="on")
    group.add_argument("--off", dest="setting", action="store_const", const="off")
    group.add_argument("--clear", dest="setting", action="store_const", const="clear")
    command.add_argument("--scope", required=True, choices=("session", "workspace", "project", "global"))
    command = add("task-update", "update the dynamic task context: records (facts, decisions, progress, open items), "
                                 "Active Work, Pending Results, milestone")
    command.add_argument("--base-revision", type=int, required=True)
    _payload_args(command)
    inputs.add_flags(command, "task-update")
    command = add("observe", "record one local observation with adapter provenance")
    command.add_argument("--handoff-id", help="Controller import of a historical child result")
    _payload_args(command)
    inputs.add_flags(command, "observe")
    command = add("mode", "switch execution mode on a human decision")
    command.add_argument("--mode", required=True, choices=core_authority.MODES)
    command.add_argument("--human-instruction", required=True)
    command.add_argument("--base-revision", type=int, required=True)
    command.add_argument("--expected-authority-sha256", required=True)
    command = add("dispose", "end management of one dispatch and fence its grants")
    command.add_argument("--handoff-id")
    command.add_argument("--tool-use-id")
    command.add_argument("--reason", required=True)
    command = add("artifact", "register a task Artifact")
    command.add_argument("--base-revision", type=int, required=True)
    command.add_argument("--id", required=True)
    command.add_argument("--path", required=True)
    command.add_argument("--summary", required=True)
    command.add_argument("--source-ref", action="append")
    command = add("close", "record mechanical closure (DONE) after your product decision")
    command.add_argument("--base-revision", type=int, required=True)
    add("detach", "end this session's association; the task stays ACTIVE for a later attach").add_argument("--reason", required=True)
    command = add("recover-authority", "restore anchored task bytes after an authority conflict and fence known children")
    command.add_argument("--expected-authority-sha256", required=True)
    command.add_argument("--reason", required=True)
    add("render-agents", "regenerate agents/*.md and the skill role table").add_argument("--check", action="store_true")
    add("version", "print the adapter version")
    add("roles", "list built-in and Controller-registered roles")
    command = add("register-role", "register a project role as a native thaliris-<name> agent, or update a registered one")
    _payload_args(command)
    command = add("unregister-role", "remove a registered project role (running children keep their dispatch-time binding)")
    command.add_argument("name")
    return parser


def _payload_args(command: argparse.ArgumentParser) -> None:
    group = command.add_mutually_exclusive_group()
    group.add_argument("--json", dest="payload_json", help="the payload as JSON text")
    group.add_argument("--input", help="a payload file: UTF-8, UTF-8 BOM or UTF-16 BOM")


def _payload(args, state: dict | None = None, **options) -> dict:
    """The call's payload: --json, --input, or the command's flag form (never a mix)."""
    name = args.command
    flagged = name in inputs.FLAGS and inputs.flag_form(args, name)
    given = args.payload_json is not None or args.input is not None
    if flagged and given:
        raise ValueError("PAYLOAD_SOURCE_CONFLICT: use either --json/--input or the flag form, not both")
    if flagged:
        return inputs.build(args, name, state, **options)
    if not given:
        raise ValueError("PAYLOAD_REQUIRED: pass --json, --input" + (" or the flag form (see --help)" if name in inputs.FLAGS else ""))
    raw = args.payload_json if args.payload_json is not None else inputs.read_text(args.input)
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("payload must be a JSON object")
    return value


def _session(args) -> str:
    session = store.native_id(args.native_session)
    if session is None:
        raise ValueError(
            "NATIVE_IDENTITY_UNKNOWN: the Claude Code hook did not add your identity to this call. Likely causes: the command was "
            "not one plain direct invocation (a trailing or leading `#` comment, a pipe, `&&`, `;`, redirection, an extra line); "
            "the runner path was written with unquoted backslashes or is another plugin version's path than this session's hook "
            "expects; or the runner was started outside Claude Code with the Thaliris plugin hooks active. Run exactly one "
            f"direct command: {hooks.runner_command()} {getattr(args, 'command', '<command>')} <arguments>")
    return session


def _controller(args) -> str:
    if args.native_agent is not None:
        raise ValueError("CONTROLLER_ONLY: this operation belongs to the owning Controller")
    return _session(args)


def _task(args) -> tuple[Path, str]:
    association = store.resolve_session(_session(args))
    if association is None:
        raise ValueError("NO_ACTIVE_TASK_FOR_SESSION: start a task, or `attach` this session to an existing one")
    return association


def _read_task(args) -> tuple[Path, str]:
    """Read-only navigation may name a task explicitly; it is never an authority grant."""
    if args.task_id:
        return store.project_root(args.root or Path.cwd()), args.task_id
    return _task(args)


def _child_entry(ctx: dict, args) -> dict:
    agent_id = store.native_id(args.native_agent)
    entry = store.dispatch_for_agent(ctx["lifecycle"], agent_id) if agent_id else None
    if entry is None or entry["binding"] not in store.EXACT_BINDINGS:
        raise ValueError("NATIVE_IDENTITY_UNBOUND: report this in your final message instead")
    if entry["disposed"]:
        raise ValueError("DISPATCH_DISPOSED")
    if entry["native_status"] in store.TERMINAL_STATUSES:
        raise ValueError("DISPATCH_FINISHED: new work needs a fresh child")
    return entry


def _project(args) -> Path:
    """The managed root is explicit or unambiguous; never guess among nested repositories (H138/H139)."""
    if args.root is not None:
        return store.project_root(args.root)
    cwd = Path.cwd().resolve()
    root = store.project_root(cwd)
    if core._git_root(cwd) is None and not store.is_managed_project(root):
        nested = []
        for child in sorted(cwd.iterdir())[:200]:
            if child.is_dir() and not child.name.startswith("."):
                nested += [child] if (child / ".git").exists() else [
                    grandchild for grandchild in sorted(child.iterdir())[:200]
                    if grandchild.is_dir() and (grandchild / ".git").exists()]
        if nested:
            raise ValueError("AMBIGUOUS_PROJECT_ROOT: this directory is not a repository but contains repositories ("
                             + ", ".join(path.relative_to(cwd).as_posix() for path in nested[:8])
                             + "). Pass --root <the repository you mean>.")
    return root


COMPACT_RECORD_CHARS = 200


def _authority_summary(root: Path, task_id: str, *, full: bool) -> dict | None:
    target = store.authority(root, task_id)
    record = target.read()
    if record is None:
        return None
    summary = {"status": record["status"], "execution_mode": record["contract"]["execution_mode"],
               "sha256": core_authority.digest(target.path())}
    constraint = record["contract"].get("execution_constraint")
    if full or constraint is not None:  # a frozen constraint stays visible in the compact view
        summary["execution_constraint"] = constraint
    return summary


def _status(root: Path, task_id: str, offset: int = 0, *, view: str = "compact", since_revision: int | None = None) -> dict:
    if view == "full":
        return _status_full(root, task_id, offset, since_revision)
    core.select_task(root, task_id)
    state = core._load_state(root)
    result = core.task_status_packet(state, offset, view="compact", since_revision=since_revision)
    dispatches = store.read_lifecycle(root, task_id)["dispatches"]
    by_status: dict[str, int] = {}
    for item in dispatches:
        by_status[item["native_status"]] = by_status.get(item["native_status"], 0) + 1
    result["Dispatches"] = {
        "total": len(dispatches), "by_native_status": by_status,
        "open": [{key: item.get(key) for key in ("handoff_id", "role", "native_status", "requested_model", "background")}
                 | ({"disposed": True} if item["disposed"] else {})
                 for item in dispatches if item["native_status"] not in store.TERMINAL_STATUSES]}
    records, total = store.current_records(state, text_chars=COMPACT_RECORD_CHARS)
    result["Records"] = {"total": total, "items": [{"id": item["id"], "kind": item["kind"], "text": item["text"]
                                                    + ("…" if item["truncated"] else "")} for item in records]}
    result["Authority"] = _authority_summary(root, task_id, full=False)
    return result


def _status_full(root: Path, task_id: str, offset: int = 0, since_revision: int | None = None) -> dict:
    core.select_task(root, task_id)
    result = core.task_status(root, offset, view="full", since_revision=since_revision)
    lifecycle = store.read_lifecycle(root, task_id)
    result["Claude Dispatches"] = [
        {key: item.get(key) for key in ("handoff_id", "role", "native_status", "binding", "background", "disposed", "parent",
                                        "requested_model", "requested_effort", "observed_model", "observed_tools")}
        | {"agent": store.native_hash(item["agent_id"]) if item.get("agent_id") else None,
           "parent": item["parent"] if item["parent"] == "controller" else store.native_hash(item["parent"]),
           "role_binding": _binding_summary(item.get("role_binding"))}
        for item in lifecycle["dispatches"][-16:]]
    state = core._load_state(root)
    records, total = store.current_records(state)
    result["Current Records"] = records
    result["Current Record Count"] = total
    result["Claude Dispatch Count"] = len(lifecycle["dispatches"])
    result["Authority"] = _authority_summary(root, task_id, full=True)
    return result


def _binding_summary(snapshot: object) -> dict | None:
    """The dispatch-time role binding, as hooks enforce it for that child (None: entry predates snapshots)."""
    if not isinstance(snapshot, dict):
        return None
    return {"source": snapshot.get("source"), "writes": snapshot.get("writes"), "delegates": snapshot.get("delegates"),
            "default_model": snapshot.get("default_model"), "default_effort": snapshot.get("default_effort"),
            "agent_file_sha256": (snapshot.get("agent_file_sha256") or "")[:16] or None}


def _role_view(key: str, record: dict) -> dict:
    view = {"role": roles.custom_role_id(key), "subagent_type": roles.CUSTOM_PREFIX + key, "default_model": record.get("model"),
            "default_effort": record.get("effort"), "writes": record.get("writes", True),
            "delegates": ["investigator"] if record.get("scanner") else [], "description": record.get("description"),
            "agent_file": record.get("agent_file")}
    view.update({field: record[field] for field in ("tools", "disallowedTools", "color", "maxTurns", "skills", "memory",
                                                    "background", "isolation") if record.get(field) not in (None, [], False)})
    return view


def _activation(session: str, root: Path, created_dir: bool) -> str:
    """When the Host can dispatch a role file written now.

    The Host documents that agent directories existing at session start are
    watched, so new files work on the next delegation. A headless probe on Claude
    Code 2.1.302 (Windows) did not pick up a new file mid-session even then, so
    same-session use is only possible, never promised. A new session always works.
    """
    at_start = None if created_dir else store.agents_dir_at_session_start(session, root)
    if created_dir or at_start is False:
        return ("next session: .claude/agents/ did not exist when this Claude Code session started, so this session "
                "does not watch it")
    if at_start:
        return ("next session; possibly the next delegation in this session: .claude/agents/ existed when this session "
                "started, which the Host documents as enough, but a headless probe (Claude Code 2.1.302) did not pick up "
                "a new agent file mid-session. If the Agent tool reports the type as not found, dispatch it from a new session.")
    return ("next session; this session UNKNOWN (whether .claude/agents/ existed when it started was not recorded). "
            "If the Agent tool reports the type as not found, dispatch it from a new session.")


def _register_role(args) -> dict:
    session = _controller(args)
    root, task_id = _task(args)
    spec = _payload(args)
    key = spec.pop("name", None)
    if not isinstance(key, str) or not roles._NAME.fullmatch(key) or roles.role_for_agent_type(f"thaliris:{key}"):
        raise ValueError("ROLE_NAME_INVALID: use 2-40 lowercase letters, digits or '-', not a built-in role name")
    replace = spec.pop("replace", False)
    if not isinstance(replace, bool):
        raise ValueError("ROLE_FIELD_INVALID: replace is true or false")
    with store.transaction(root, task_id) as ctx:  # an authorized Controller act, under the task lock
        previous = store.registered_role(root, key)
        # An update merges onto the registered record (null resets a field); `replace` starts from scratch.
        base = {} if replace or previous is None else {field: previous[field] for field in roles.CUSTOM_FIELDS if field in previous}
        record = roles.validate_custom_record(base | spec)
        created_dir = store.ensure_agents_dir(root)
        entry = store.register_role(ctx, key, record, roles.render_custom_agent(key, record))
    return {"ok": True, "role": roles.custom_role_id(key), "subagent_type": roles.CUSTOM_PREFIX + key,
            "action": "updated" if previous is not None else "registered", "agent_file": entry["agent_file"],
            "agent_file_sha256": entry["sha256"], "activation": _activation(session, root, created_dir),
            "running_children": "keep the role binding recorded at their dispatch; this change applies to future dispatches"}


def _render(check: bool) -> dict:
    root = hooks.plugin_root()
    outputs = dict(roles.rendered_agents())
    skill = root / "skills" / "thaliris" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    start, end = text.index(_ROLE_TABLE_BEGIN) + len(_ROLE_TABLE_BEGIN), text.index(_ROLE_TABLE_END)
    outputs["skills/thaliris/SKILL.md"] = text[:start] + "\n" + roles.render_role_table() + "\n" + text[end:]
    drift = [name for name, content in outputs.items()
             if not (root / name).is_file() or (root / name).read_text(encoding="utf-8") != content]
    if not check:
        for name in drift:
            core._atomic_write(root / name, outputs[name].encode("utf-8"))
    return {"ok": not (check and drift), "drift" if check else "written": drift}


def run(args) -> dict:
    name = args.command
    if name == "version":
        import platform
        import thaliris  # the Core actually imported (the vendored, pinned copy), not an installed distribution
        return {"ok": True, "version": __version__, "plugin_root": hooks.plugin_root().as_posix(),
                "python": platform.python_version(), "core_version": thaliris.__version__}
    if name == "roles":
        association = store.resolve_session(args.native_session)
        root = association[0] if association and args.root is None else store.project_root(args.root or Path.cwd())
        builtin = [{"role": role, "subagent_type": roles.get_binding(role).subagent_type, "default_model": roles.get_binding(role).model,
                    "writes": roles.repo_write_allowed(role), "delegates": sorted(roles.get_binding(role).allowed_delegation_targets)}
                   for role in roles.child_roles()]
        custom = [_role_view(key, record) for key, record in sorted(store.custom_role_records(root).items())]
        return {"ok": True, "builtin": builtin, "registered": custom,
                "note": "Models and effort are the Controller's choice per dispatch; defaults apply only when it sets none."}
    if name == "register-role":
        return _register_role(args)
    if name == "unregister-role":
        _controller(args)
        root, task_id = _task(args)
        if not roles._NAME.fullmatch(args.name):
            raise ValueError("ROLE_NAME_INVALID")
        with store.transaction(root, task_id) as ctx:
            removed = store.unregister_role(ctx, args.name)
        return {"ok": True, "role": roles.custom_role_id(args.name), **removed,
                "running_children": "keep the role binding recorded at their dispatch; new dispatches of this type are generic agents"}
    if name == "render-agents":
        return _render(args.check)
    if name == "guidance":
        base = hooks.plugin_root() / "skills" / "thaliris"
        path = base / ("SKILL.md" if not args.section else f"{args.section}.md")
        if args.section and (not path.resolve().is_relative_to(base.resolve()) or not path.is_file()):
            raise ValueError("unknown guidance section; available: " + ", ".join(sorted(p.stem for p in base.glob("*.md") if p.name != "SKILL.md")))
        return {"ok": True, "guidance": path.read_text(encoding="utf-8")}
    if name == "bootstrap":
        _controller(args)
        root = _project(args)
        initialized = None if store.is_managed_project(root) else core.init(root)
        agents_dir_created = store.ensure_agents_dir(root)  # so roles registered later land in a watched directory
        association = store.resolve_session(args.native_session)
        return {"ok": True, "root": str(root), "initialized": initialized is not None, "agents_dir_created": agents_dir_created,
                "navigation": [".agent-memory/INDEX.md", ".milestones/INDEX.md"],
                "associated_task": association[1] if association else None, "runner": hooks.runner_command()}
    if name == "task-start":
        session = _controller(args)
        if store.resolve_session(session) is not None:
            raise ValueError("SESSION_ALREADY_HAS_ACTIVE_TASK: close or abandon it first")
        root = _project(args)
        flagged = inputs.flag_form(args, "task-start")
        if flagged and args.contract is not None:
            raise ValueError("PAYLOAD_SOURCE_CONFLICT: use either --contract <file> or the contract flags, not both")
        if flagged:
            contract = inputs.build(args, "task-start")
        elif args.contract is None:
            raise ValueError("CONTRACT_REQUIRED: pass --contract <file>, or --human-instruction --boundary --invariants "
                             "--acceptance --execution-mode (optionally --execution-constraint)")
        elif args.contract.lstrip().startswith("{"):
            # --contract names a file, never inline JSON (H059): the flags are the inline form.
            raise ValueError("CONTRACT_FILE_PATH_REQUIRED: --contract takes a file path; pass the contract as flags "
                             "(--human-instruction --boundary --invariants --acceptance --execution-mode) instead of JSON text")
        else:
            contract = json.loads(inputs.read_text(args.contract))
        result = store.start_task(root, args.goal, contract, session)
        result["agents_dir_created"] = store.ensure_agents_dir(root)
        return result
    if name == "attach":
        session = _controller(args)
        if not args.task_id:
            raise ValueError("attach requires --task-id")
        root = _project(args)
        with store.transaction(root, args.task_id) as ctx:
            pass  # The anchored authority must be ACTIVE and intact.
        store.write_session(session, root, args.task_id, args.reason)
        # A new session lost the conversation: hand back what the Controller recorded.
        result = {"ok": True, "task_id": args.task_id, "root": str(root), "Task Context": store.task_context_text(ctx["state"])}
        summary = store.identity_summary(root)
        if summary:
            result["Project Identity"] = summary
        return result
    if name in {"status", "get"}:
        root, task_id = _read_task(args)
        if name == "status":
            return _status(root, task_id, args.evidence_offset, view=args.view, since_revision=args.since_revision)
        core.select_task(root, task_id)
        return core.task_get(root, args.id)
    if name in {"catalog", "document-get"} | _IDENTITY_READS:
        association = store.resolve_session(args.native_session)
        root = association[0] if association and args.root is None else store.project_root(args.root or Path.cwd())
        if name == "identity-status":
            return identity.identity_status(root, session_key=store.native_id(args.native_session))
        if name == "identity-get":
            return identity.identity_get(root, version=args.version, items=args.item, amendment=args.amendment, challenge=args.challenge)
        if name == "identity-trace":
            return identity.identity_trace(root, args.ref, args.direction, args.depth)
        return core.catalog(root, args.path) if name == "catalog" else core.document_get(root, args.paths)
    if name in {"identity-note", "identity-suggest"}:
        # Controller-only, but no ACTIVE task: a user's explicit words are recorded whenever they are said.
        session = _controller(args)
        association = store.resolve_session(session)
        root = association[0] if association and args.root is None else store.project_root(args.root or Path.cwd())
        if name == "identity-suggest":
            return identity_basis.identity_suggest(root, "claude:controller", args.scope, args.setting, session_key=session)
        if args.list_notes:
            if args.quote is not None or args.retract is not None:
                raise ValueError("FLAG_FORM_CONFLICT: --list cannot be combined with --quote or --retract")
            return identity_basis.identity_note_list(root, include_retracted=args.include_retracted)
        if args.retract is not None:
            if args.quote is not None or not args.reason:
                raise ValueError("FLAG_FORM_INCOMPLETE: `identity-note --retract N-0001` needs --reason and no --quote")
            return identity_basis.identity_note(root, "claude:controller", {"retract": args.retract, "reason": args.reason})
        if not args.quote:
            raise ValueError("FLAG_FORM_INCOMPLETE: `identity-note` needs --quote (or --retract/--list)")
        payload = {"quote": args.quote}
        if args.kind:
            payload["kind"] = args.kind
        if args.context is not None:
            payload["context"] = args.context
        if args.ref:
            payload["refs"] = list(args.ref)
        return identity_basis.identity_note(root, "claude:controller", payload)
    root, task_id = _task(args)
    if name == "observe":
        with store.transaction(root, task_id) as ctx:
            if args.native_agent is not None:
                if args.handoff_id:
                    raise ValueError("children never select a handoff id; the adapter supplies it")
                entry = _child_entry(ctx, args)
                payload = _payload(args, ctx["state"], prefix=(entry["handoff_id"] or "") + ".")
                result = store.observe(ctx, payload, agent_id=args.native_agent, handoff_id=None)
            else:
                if not args.handoff_id:
                    raise ValueError("Controller imports name the exact --handoff-id")
                payload = _payload(args, ctx["state"], prefix=args.handoff_id + ".", qualified=True)
                result = store.observe(ctx, payload, agent_id=None, handoff_id=args.handoff_id)
        return {"ok": True, **_write_response(ctx["state"], ["evidence_continuity"], appended=result.pop("appended"),
                                              child=args.native_agent is not None), **result}
    if name in {"promote", "document-maintain"}:
        actor = "controller"
        with store.critical(root, task_id) as ctx:  # authority check, Core write and re-anchor in one lock span
            if args.native_agent is not None:
                entry = _child_entry(ctx, args)
                if entry["role"] != "curator":
                    raise ValueError("CURATOR_OR_CONTROLLER_ONLY")
                actor = f"handoff:{entry['handoff_id']}" if entry["handoff_id"] else "curator"
            core.select_task(root, task_id)
            function = core.task_promote if name == "promote" else core.document_maintain
            temporary = _payload_file(root, _payload(args))
            try:
                return function(root, actor, args.base_revision, str(temporary))
            finally:
                temporary.unlink(missing_ok=True)
    _controller(args)
    if name in _IDENTITY_MUTATIONS:
        # Identity mutations never bump the task revision and are not memory promotion; the Controller's act, on the
        # human owner's decision carried in the payload. Core records the actor and task provenance.
        with store.critical(root, task_id):  # the anchored authority must be ACTIVE and intact
            core.select_task(root, task_id)
            source = _payload(args)  # tolerant decoding; Core receives the parsed object
            return getattr(identity, name.replace("-", "_"))(root, "claude:controller", args.base_revision, source)
    if name == "task-update":
        with store.critical(root, task_id) as ctx:  # the anchored authority must be ACTIVE and intact
            payload = _payload(args, ctx["state"])
            for record in payload.get("records") or []:
                if isinstance(record, dict) and {"producer", "revision"} & set(record):
                    raise ValueError("TASK_UPDATE_PROVENANCE: the runner supplies record producer and revision")
            core.select_task(root, task_id)
            before = {item["id"] for item in ctx["state"]["records"]}
            temporary = _payload_file(root, payload)
            try:
                result = core.task_update(root, "controller", args.base_revision, str(temporary))
            finally:
                temporary.unlink(missing_ok=True)
            state = core._load_state(root)
            return _write_response(state, result["changed"], appended=[item["id"] for item in state["records"] if item["id"] not in before])
    if name in {"register", "judge"}:
        with store.transaction(root, task_id) as ctx:
            payload = _payload(args, ctx["state"])
            appended = store.append_controller(ctx, args.base_revision, payload, "register" if name == "register" else "judgments")
            response = _write_response(ctx["state"], ["evidence_continuity"], appended=appended)
            if name == "judge":  # echo what a bare id resolved to, per judgment
                response["observation_refs"] = {item["id"]: item["observation_refs"] for item in payload.get("judgments") or [payload]}
            return response
    if name == "dispose":
        with store.transaction(root, task_id) as ctx:
            matches = [item for item in ctx["lifecycle"]["dispatches"]
                       if (args.handoff_id and item["handoff_id"] == args.handoff_id) or (args.tool_use_id and item["tool_use_id"] == args.tool_use_id)]
            if len(matches) != 1:
                raise ValueError("DISPATCH_NOT_FOUND: name exactly one --handoff-id or --tool-use-id")
            entry = matches[0]
            entry["disposed"] = True
            store.note(entry, "disposed", reason=args.reason[:500])
            ctx["lifecycle_changed"] = True
        # Disposal ends management and fences grants; it proves nothing about native termination.
        return {"ok": True, "handoff_id": entry["handoff_id"], "native_status": entry["native_status"], "native_termination": "UNKNOWN"
                if entry["native_status"] in store.UNRESOLVED_STATUSES else entry["native_status"]}
    if name == "mode":
        with store.critical(root, task_id, checkpoint=False) as ctx:  # Core's switch re-anchors the authority itself
            unresolved = store.unresolved(ctx["lifecycle"])
            if unresolved:
                raise ValueError(f"UNRESOLVED_CHILD_DISPATCHES: {len(unresolved)} pending/running; wait for them or `dispose` them first")
            return ctx["store"].switch_mode(args.expected_authority_sha256, args.base_revision, args.mode, args.human_instruction)
    if name == "artifact":
        with store.critical(root, task_id):
            core.select_task(root, task_id)
            result = core.task_artifact(root, args.base_revision, args.id, args.path, args.summary,
                                        producer="controller", registered_by="claude:controller", evidence_refs=args.source_ref)
            return _write_response(core._load_state(root), result["changed"])
    if name == "close":
        with store.transaction(root, task_id) as ctx:
            unresolved = store.unresolved(ctx["lifecycle"])
            if unresolved:
                raise ValueError("CHILD_EXECUTION_UNRESOLVED: " + ", ".join(
                    f"{item['role']}:{item['handoff_id'] or item['tool_use_id']}={item['native_status']}" for item in unresolved)
                    + ". Wait for terminal evidence or `dispose` the dependency.")
            core.check_revision(ctx["state"], args.base_revision)
            ctx["state"]["status"] = "DONE"
            ctx["state"]["revision"] += 1
            ctx["state_changed"] = True
            result = {**_write_response(ctx["state"], ["status"]),
                      "note": "DONE records mechanical closure; it is not product acceptance."}
        try:  # after the close committed; any failure just leaves the field out
            offer = identity_basis.identity_offer(root, store.native_id(args.native_session))
        except Exception:
            offer = None
        if offer:
            result["identity_offer"] = offer
        return result
    if name == "detach":
        store.clear_session(args.native_session)
        return {"ok": True, "task_id": task_id, "status": "ACTIVE", "note": "Session association removed; authority and ledger are unchanged."}
    if name == "recover-authority":
        lifecycle_path = store.lifecycle_path(root, task_id)

        def fence(record: dict) -> None:
            # Known children lose their grants; their native termination stays UNKNOWN.
            lifecycle = store.read_lifecycle(root, task_id)
            for entry in lifecycle["dispatches"]:
                if not entry["disposed"]:
                    entry["disposed"] = True
                    store.note(entry, "fenced-by-recovery", reason=args.reason[:500])
            store.write_lifecycle(root, task_id, lifecycle)

        result = store.authority(root, task_id).recover(
            args.expected_authority_sha256, args.reason, restore_adapter=fence,
            archive_paths={store.LIFECYCLE_NAME: lifecycle_path} if lifecycle_path.is_file() else {})
        return {**result, "native_termination": "UNKNOWN"}
    raise ValueError(f"unsupported command: {name}")


def _write_response(state: dict, changed: list[str], *, appended: list[str] | None = None, child: bool = False) -> dict:
    """Core's compact write acknowledgement: ack, new revision and the open-evidence summary (totals plus bounded items).

    A child's write carries only the totals: the item list is the Controller's to re-present.
    """
    response = core.task_write_response(state, changed, appended=appended)
    if child:
        response["evidence"] = {key: value for key, value in response["evidence"].items() if key.endswith("_total")}
    return response


def _payload_file(root: Path, payload: dict) -> Path:
    path = root / ".context" / f".claude-input-{store.new_handoff_id()}.json"
    core._atomic_write(path, json.dumps(payload).encode("utf-8"))
    return path


def _emit(text: str) -> None:
    """Write runner output as UTF-8 bytes whatever the console codec is.

    Runner output stays human-readable (not ASCII-escaped), but a redirected or
    piped Windows stdout defaults to a legacy code page (cp1252) that cannot
    encode Chinese; writing through the text layer would raise after the work
    was done. Bytes go to the underlying buffer; a stream without one is a test double.
    """
    stream = sys.stdout
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
        return
    stream.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()


_ADAPTER_CODE = re.compile(r"([A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)(?:: |$)")


def _error(exc: BaseException) -> dict:
    """Core's structured envelope (code, recovery, details) for a ThalirisError; an adapter `CODE: message` gets its code too."""
    envelope = core.error_envelope(exc)
    if "code" not in envelope:
        match = _ADAPTER_CODE.match(str(exc))
        if match:
            envelope["code"] = match.group(1)
    return envelope


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["hook"]:
        return hooks.main(argv[1] if len(argv) > 1 else "")
    try:
        for flag in ("--native-session", "--native-agent"):
            # argparse keeps the last repeated value; identity must appear at most once (H023).
            if sum(item == flag or item.startswith(flag + "=") for item in argv) > 1:
                raise ValueError(f"DUPLICATE_IDENTITY_FLAG: {flag}")
        result = run(_parser().parse_args(argv))
    except core.TaskStateSchemaIncompatible as exc:
        result, code = {"ok": False, **exc.diagnostic}, 3
    except (ValueError, OSError, RuntimeError, KeyError) as exc:
        result, code = _error(exc), 2
    else:
        code = 0 if result.get("ok", True) else 3
    # Compact JSON (Controllers pay for every byte they read); keys are sorted, text stays UTF-8 and readable.
    _emit(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
    return code
