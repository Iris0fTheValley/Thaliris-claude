"""Claude native identity bound to the shared Thaliris Core.

Core owns the task ledger, evidence continuity and authority anchors. This
module only adds the mechanical Claude facts Core cannot know: which session
is associated with which task, and which native agent carries which handoff.
It never decides relevance, completion or acceptance.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from thaliris import core, identity
from thaliris.authority import AuthorityStore, validate_contract

# Only Thaliris-owned config is anchored. Anchoring user-owned Claude settings made
# legitimate human edits look like tampering, and recovery would revert their bytes
# (history H094/H098). Children are still refused writes to .claude/settings*.json.
PROTECTED_PATHS = (".context/config.json",)
MODEL_ALIASES = ("sonnet", "opus", "haiku", "fable", "inherit")
# Explicit renames: a frozen `claude-model:<name>` whose name was retired resolves to its successor here.
# Add an entry when a Host renames or retires an alias; existing tasks keep dispatching without being rewritten.
ALIAS_RENAMES: dict[str, str] = {}
# Family mapping: a versioned or prefixed spelling of a family ("claude-opus-4-1", "sonnet-4.5", "3-5-sonnet-20241022",
# "sonnet[1m]") resolves to that family's current alias, so a model update does not strand a task frozen on the old name.
_ALIAS_FAMILY = re.compile(
    r"(?:claude[-_.]?)?(?:\d+(?:[-_.]\d+)*[-_.])?(sonnet|opus|haiku|fable)"
    r"(?:[-_.]?(?:\d+(?:[-_.]\d+)*|latest|preview))*(?:\[\w+\])?")
LIFECYCLE_NAME = "claude-lifecycle.json"
LIFECYCLE_VERSION = 1
_NATIVE_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
# Native statuses: reserved -> launched/running -> completed/stopped.
# These describe execution only; none of them is product acceptance or Core DONE.
UNRESOLVED_STATUSES = frozenset({"reserved", "launched", "running"})
TERMINAL_STATUSES = frozenset({"completed", "stopped"})
# Bindings that rest on an exact Host record, never on "the only pending one" (H144).
EXACT_BINDINGS = frozenset({"exact", "host-meta"})


def home() -> Path:
    return Path(os.environ.get("THALIRIS_CLAUDE_HOME") or Path.home() / ".thaliris" / "claude")


def native_id(value: object) -> str | None:
    return value if isinstance(value, str) and _NATIVE_ID.fullmatch(value) else None


def native_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_root(path: Path) -> Path:
    path = path.resolve()
    return core._git_root(path) or path


def is_managed_project(root: Path) -> bool:
    return (root / ".context" / "config.json").is_file()


# Session association lives outside the repository so a workspace's old ACTIVE
# record never selects a new session's task, and children cannot rewrite it.
def _session_path(session_id: str) -> Path:
    return home() / "sessions" / f"{native_hash(session_id)}.json"


def read_session(session_id: str | None) -> dict | None:
    if not native_id(session_id):
        return None
    try:
        value = json.loads(_session_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and isinstance(value.get("root"), str) and isinstance(value.get("task_id"), str) else None


def write_session(session_id: str, root: Path, task_id: str, reason: str) -> None:
    core._atomic_write(_session_path(session_id), (json.dumps(
        {"root": str(root), "task_id": task_id, "associated_at": now(), "reason": reason}, sort_keys=True) + "\n").encode())


def authority(root: Path, task_id: str) -> AuthorityStore:
    return AuthorityStore(root, home() / "task-authority", protected_paths=PROTECTED_PATHS, task_id=task_id)


def resolve_alias(alias: object) -> str | None:
    """Map a model alias (current, renamed or versioned) to a currently dispatchable alias, or None when unknown.

    `inherit` is never a constraint, so it does not resolve here.
    """
    if not isinstance(alias, str):
        return None
    name = alias.strip().casefold()
    name = ALIAS_RENAMES.get(name, name)
    if name in MODEL_ALIASES[:-1]:
        return name
    match = _ALIAS_FAMILY.fullmatch(name)
    return match.group(1) if match else None


def validate_constraint(contract: dict) -> str | None:
    """Return the current alias for the frozen `claude-model:<alias>` constraint.

    The frozen text is never rewritten; a renamed or versioned alias keeps resolving through `resolve_alias`.
    An alias that resolves to nothing raises, because the frozen intent is unknown and must not be guessed.
    """
    value = contract.get("execution_constraint")
    if value is None:
        return None
    prefix, _, alias = str(value).partition(":")
    resolved = resolve_alias(alias) if prefix == "claude-model" else None
    if resolved is None:
        raise ValueError("UNSUPPORTED_EXECUTION_CONSTRAINT")
    return resolved


def clear_session(session_id: str | None) -> None:
    if native_id(session_id):
        _session_path(session_id).unlink(missing_ok=True)
        forget_packet(session_id)
        lane_marker(session_id).unlink(missing_ok=True)


def forget_packet(session_id: str | None) -> None:
    if native_id(session_id):
        _session_path(session_id).with_suffix(".packet").unlink(missing_ok=True)


def packet_is_new(session_id: str | None, packet: str) -> bool:
    """Remember the last evidence packet shown to this session.

    Re-presenting an unchanged packet every turn turns continuity into noise;
    a changed packet (new observation, new judgment) is always shown.
    """
    if not native_id(session_id):
        return True
    path = _session_path(session_id).with_suffix(".packet")
    digest = hashlib.sha256(packet.encode("utf-8")).hexdigest()
    try:
        if path.read_text(encoding="utf-8").strip() == digest:
            return False
    except OSError:
        pass
    try:
        core._atomic_write(path, digest.encode())
    except OSError:
        pass
    return True


def note_session_agents_dir(session_id: str | None, root: Path) -> None:
    """Remember whether this Host process started with the project's `.claude/agents/`.

    Claude Code watches agent directories that existed at session start, so this
    fact decides whether a role registered later is usable on the next delegation.
    Only process starts (startup, resume) record it; compaction keeps the process.
    """
    if not native_id(session_id):
        return
    try:
        existed = (root / ".claude" / "agents").is_dir()
        core._atomic_write(_session_path(session_id).with_suffix(".agents"), (json.dumps(
            {"root": str(root), "agents_dir": existed, "at": now()}, sort_keys=True) + "\n").encode())
    except OSError:
        pass


def agents_dir_at_session_start(session_id: str | None, root: Path) -> bool | None:
    """True/False when recorded for this session and root; None is UNKNOWN."""
    if not native_id(session_id):
        return None
    try:
        value = json.loads(_session_path(session_id).with_suffix(".agents").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("root") != str(root) or not isinstance(value.get("agents_dir"), bool):
        return None
    return value["agents_dir"]


ROLES_NAME = "claude-roles.json"


def _roles_path(root: Path) -> Path:
    return core._safe_without_final_symlink(root, f".context/{ROLES_NAME}")


def _read_registry(root: Path) -> dict:
    try:
        registry = json.loads(_roles_path(root).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "roles": {}}
    except (OSError, ValueError) as exc:
        raise ValueError("CLAUDE_ROLE_REGISTRY_INVALID: the role registry could not be read") from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("roles", {}), dict):
        raise ValueError("CLAUDE_ROLE_REGISTRY_INVALID: the role registry has an unexpected shape")
    registry.setdefault("roles", {})
    return registry


def _write_registry(root: Path, registry: dict) -> None:
    core._atomic_write(_roles_path(root), (json.dumps(registry, sort_keys=True, indent=2) + "\n").encode())


def file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def custom_role_records(root: Path) -> dict[str, dict]:
    """Controller-registered roles whose agent file still matches its recorded digest.

    A missing or edited agent file is not a Thaliris role for a *new* dispatch
    (UNKNOWN), so a child cannot mint a managed role by writing an agent file of
    its own. Children already dispatched keep the role binding snapshot recorded
    in their lifecycle entry instead (see `reserve`).
    """
    try:
        registry = _read_registry(root)
    except ValueError:
        return {}
    verified = {}
    for name, record in registry["roles"].items():
        try:
            agent = core._safe_without_final_symlink(root, record["agent_file"])
            if hashlib.sha256(agent.read_bytes()).hexdigest() == record["sha256"]:
                verified[name] = record
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return verified


def registered_role(root: Path, name: str) -> dict | None:
    """One registry entry, verified or not (the base for an update)."""
    record = _read_registry(root)["roles"].get(name)
    return record if isinstance(record, dict) else None


def agent_file(name: str) -> str:
    return f".claude/agents/thaliris-{name}.md"


def ensure_agents_dir(root: Path) -> bool:
    """Create `.claude/agents/` so roles registered later land in a directory the Host watches.

    Claude Code picks up new or edited agent files on the next delegation, except
    in directories that did not exist when the session started. Returns whether
    the directory had to be created; an empty directory changes no existing bytes.
    """
    path = core._safe_without_final_symlink(root, ".claude/agents")
    if path.is_dir():
        return False
    path.mkdir(parents=True, exist_ok=True)
    return True


def _owned_agent_path(root: Path, name: str, previous: dict | None) -> Path:
    """Refuse to touch an agent file whose bytes the registry did not write."""
    path = core._safe_without_final_symlink(root, agent_file(name))
    current = file_sha256(path)
    if current is not None and (previous is None or previous.get("sha256") != current):
        raise ValueError(f"ROLE_AGENT_FILE_NOT_OWNED: {agent_file(name)} exists and its bytes are not the ones this "
                         "registry wrote (a user-owned file, or edited by hand). It was left untouched. Ask the user "
                         "whether to keep, move or delete it, or register the role under another name.")
    return path


def register_role(ctx: dict, name: str, record: dict, agent_text: str) -> dict:
    """Write the native agent file and its registry entry under the task lock.

    The file is (re)written only when it is absent or still byte-identical to what
    the registry last wrote; anything else is preserved and refused.
    """
    root = ctx["root"]
    registry = _read_registry(root)
    previous = registry["roles"].get(name) if isinstance(registry["roles"].get(name), dict) else None
    path = _owned_agent_path(root, name, previous)
    payload = agent_text.encode("utf-8")
    core._atomic_write(path, payload)
    entry = {**record, "agent_file": agent_file(name), "sha256": hashlib.sha256(payload).hexdigest(),
             "registered_at": now(), "registered_in_task": ctx["task_id"]}
    if previous is not None:
        entry["first_registered_at"] = previous.get("first_registered_at") or previous.get("registered_at")
    registry["roles"][name] = entry
    _write_registry(root, registry)
    return entry


def unregister_role(ctx: dict, name: str) -> dict:
    """Remove one registered role; delete its agent file only while the registry owns its bytes.

    Children already dispatched keep their snapshot; only future dispatches change.
    """
    root = ctx["root"]
    registry = _read_registry(root)
    previous = registry["roles"].get(name)
    if not isinstance(previous, dict):
        raise ValueError("ROLE_NOT_REGISTERED")
    path = core._safe_without_final_symlink(root, agent_file(name))
    current = file_sha256(path)
    if current is None:
        result = "absent"
    elif current == previous.get("sha256"):
        path.unlink()
        result = "deleted"
    else:
        result = "preserved: its bytes changed after registration, so it is now an ordinary unmanaged agent file"
    del registry["roles"][name]
    _write_registry(root, registry)
    return {"agent_file": agent_file(name), "agent_file_result": result}


def lifecycle_path(root: Path, task_id: str) -> Path:
    return core._safe_without_final_symlink(root, f".context/tasks/{task_id}/{LIFECYCLE_NAME}")


# Forward migration: each step upgrades a lifecycle value one version in memory. A file older than LIFECYCLE_VERSION is
# migrated on read and persisted by the next transaction; only a version newer than this code is refused. Add a step
# `{n: migrate_n_to_n_plus_1}` whenever LIFECYCLE_VERSION is raised, and keep it additive (never drop or reinterpret fields).
def _migrate_0_to_1(value: dict) -> dict:
    """A file with no version number predates versioning; its shape is version 1's."""
    return {**value, "version": 1}


LIFECYCLE_MIGRATIONS = {0: _migrate_0_to_1}


def _migrate_lifecycle(value: dict) -> tuple[dict, bool]:
    version = value.get("version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise _lifecycle_error("CLAUDE_LIFECYCLE_INVALID", f"the lifecycle version {version!r} is not a version number")
    if version > LIFECYCLE_VERSION:
        # A newer plugin wrote this. Never reinterpret it: identity facts stay UNKNOWN until a newer plugin runs.
        raise core.ThalirisError(
            "CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED",
            f"CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED: this task's lifecycle is version {version}, newer than this adapter's "
            f"{LIFECYCLE_VERSION}. Upgrade the Thaliris plugin to the version that wrote it (or newer).",
            recovery=("The lifecycle file was written by a newer Thaliris plugin than the one loaded in this session. Do not edit "
                      "it. Upgrade the plugin (`claude plugin update thaliris@thaliris`, then a new session or `/reload-plugins`) "
                      "and `attach --task-id` to the same task; the task and its authority are intact. Child writes and messages "
                      "are refused until then. See `guidance --section recovery`."))
    migrated = version < LIFECYCLE_VERSION
    while version < LIFECYCLE_VERSION:
        step = LIFECYCLE_MIGRATIONS.get(version)
        if step is None:
            raise _lifecycle_error("CLAUDE_LIFECYCLE_INVALID", f"no migration from lifecycle version {version}")
        value = step(value)
        version = value["version"]
    return value, migrated


# Keys every version-1 entry carries (the 0.1 shape included): hooks and the runner index them directly.
# `role_binding`, `requested_effort` and the observed_* fields were added later and are read with .get().
REQUIRED_ENTRY_KEYS = ("tool_use_id", "role", "subagent_type", "parent", "handoff_id", "binding", "native_status", "disposed")
_LIFECYCLE_RECOVERY = ("The lifecycle file is damaged or was written by another adapter version. Do not edit it by hand. Child "
                       "writes and messages are refused while it is unreadable; the Controller's own work is unaffected. "
                       "Ask the user: run the adapter version that wrote it, or start an independent new task. "
                       "See `guidance --section recovery`.")


def _lifecycle_error(code: str, detail: str) -> core.ThalirisError:
    return core.ThalirisError(code, f"{code}: {detail}", recovery=_LIFECYCLE_RECOVERY)


def _check_entries(task_id: str, dispatches: list) -> None:
    for index, item in enumerate(dispatches):
        if not isinstance(item, dict):
            raise _lifecycle_error("CLAUDE_LIFECYCLE_INVALID", f"dispatch {index} of task {task_id} is not an object")
        missing = [key for key in REQUIRED_ENTRY_KEYS if key not in item]
        if missing:
            raise _lifecycle_error("CLAUDE_LIFECYCLE_INVALID", f"dispatch {index} of task {task_id} lacks {', '.join(missing)}")
        if (not isinstance(item["disposed"], bool) or item["native_status"] not in UNRESOLVED_STATUSES | TERMINAL_STATUSES
                or not isinstance(item["parent"], str)):
            raise _lifecycle_error("CLAUDE_LIFECYCLE_INVALID", f"dispatch {index} of task {task_id} has an unreadable field")


def read_lifecycle(root: Path, task_id: str) -> dict:
    return load_lifecycle(root, task_id)[0]


def load_lifecycle(root: Path, task_id: str) -> tuple[dict, bool]:
    """Return ``(lifecycle, migrated)``: an older file is migrated forward in memory; the next transaction persists it."""
    path = lifecycle_path(root, task_id)
    if not path.is_file():
        return {"version": LIFECYCLE_VERSION, "task_id": task_id, "dispatches": []}, False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:  # JSON or encoding damage
        raise _lifecycle_error("CLAUDE_LIFECYCLE_INVALID", "the lifecycle file is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("task_id") != task_id or not isinstance(value.get("dispatches"), list):
        raise _lifecycle_error("CLAUDE_LIFECYCLE_INVALID", "the lifecycle file has an unexpected shape")
    value, migrated = _migrate_lifecycle(value)
    _check_entries(task_id, value["dispatches"])
    return value, migrated


def write_lifecycle(root: Path, task_id: str, value: dict) -> None:
    """Caller holds Core's task lock."""
    core._atomic_write(lifecycle_path(root, task_id), (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


@contextmanager
def transaction(root: Path, task_id: str, *, observe_only: bool = False):
    """Hold Core's task lock over the authority check, ledger and lifecycle.

    Yields a context with the loaded state and lifecycle. Mutators set
    ``ctx["state_changed"]`` / ``ctx["lifecycle_changed"]``; state writes are
    re-anchored before the lock is released. ``observe_only`` records native
    execution facts without an authority check and refuses ledger writes, so
    damaged authority never hides what the Host reported.
    """
    with core._lock(root):
        core.select_task(root, task_id)
        store = authority(root, task_id)
        record = None if observe_only else store.check()
        if record is None and not observe_only:
            raise ValueError("TASK_AUTHORITY_NOT_ACTIVE")
        state = core._load_state(root, active=not observe_only)
        lifecycle, migrated = load_lifecycle(root, task_id)
        ctx = {"root": root, "task_id": task_id, "authority": record, "store": store, "state": state, "lifecycle": lifecycle,
               "state_changed": False, "lifecycle_changed": migrated}  # an older lifecycle is persisted in its new version
        yield ctx
        if ctx["state_changed"]:
            if observe_only:
                raise ValueError("OBSERVE_ONLY_TRANSACTION")
            core._write_state(root, ctx["state"])
            store.checkpoint()
        if ctx["lifecycle_changed"]:
            write_lifecycle(root, task_id, ctx["lifecycle"])


@contextmanager
def critical(root: Path, task_id: str, *, checkpoint: bool = True):
    """One lock span for an authorized Core mutation: authority check, the Core call and the re-anchor.

    Core's mutators take the task lock themselves and the file lock is not reentrant, so a
    transaction around them used to release it first. Here Core's lock is a no-op for this
    root while this process already holds it, so nothing runs between the authority check,
    Core's write and the checkpoint (an interleaved hook saw the new state bytes against the
    old anchor, and state changed between check and write would have been anchored by the
    checkpoint). Core's own checks (revision CAS, validation) run unchanged. The checkpoint
    runs only on success.
    """
    with transaction(root, task_id) as ctx:
        target, original = root.resolve(), core._lock

        @contextmanager
        def held(path):
            if Path(path).resolve() == target:
                yield
            else:
                with original(path):
                    yield

        core._lock = held
        try:
            yield ctx
            if checkpoint:
                ctx["store"].checkpoint()
        finally:
            core._lock = original


def inspect_session(session_id: str | None) -> tuple[tuple[Path, str] | None, dict | None]:
    """Return ``(association, problem)`` for a session.

    ``association`` is (root, task_id) when the associated Core task is ACTIVE and readable. ``problem`` is set when the
    session names a task whose state exists but cannot be read (an incompatible schema after an upgrade or downgrade,
    damaged JSON, an identity conflict, an I/O error): that task is NOT absent, so callers must not treat the session as
    unmanaged. A task whose state file is missing or that is no longer ACTIVE is simply "no task". The common no-task
    path reads one small file and nothing else.
    """
    association = read_session(session_id)
    if association is None:
        return None, None
    root, task_id = Path(association["root"]), association["task_id"]
    try:
        core.select_task(root, task_id)
    except ValueError:
        return None, None  # a session record that names no valid task id names no task
    try:
        state = core._load_state(root)
    except core.TaskStateSchemaIncompatible as exc:
        return None, {"root": root, "task_id": task_id, "code": "STATE_SCHEMA_INCOMPATIBLE", "diagnostic": exc.diagnostic,
                      "message": "the task state schema is incompatible with this Core"}
    except core.ThalirisError as exc:
        if exc.code == core.errors.TASK_STATE_MISSING:
            return None, None
        return None, {"root": root, "task_id": task_id, "code": exc.code, "message": exc.message[:300]}
    except (ValueError, OSError) as exc:
        return None, {"root": root, "task_id": task_id, "code": "TASK_STATE_UNREADABLE", "message": str(exc)[:300]}
    return ((root, task_id) if state["status"] == "ACTIVE" else None), None


def resolve_session(session_id: str | None) -> tuple[Path, str] | None:
    """Return the associated (root, task_id) when its Core task is still ACTIVE and readable."""
    return inspect_session(session_id)[0]


SCHEMA_RECOVERY = ("Do not edit or delete the task state. Managed assurance for this task is UNKNOWN. The user decides: run the "
                   "plugin/Core version that wrote it, follow Core's recovery_action where it applies, or start an independent "
                   "new task with `task-start`. See `guidance --section recovery`.")


def problem_text(problem: dict) -> str:
    """What the Controller needs to know about an associated task that cannot be read (bounded, no secrets)."""
    detail = ""
    diagnostic = problem.get("diagnostic")
    if diagnostic:
        detail = (f" (state schema {diagnostic.get('from_version')!r}, this Core reads {diagnostic.get('to_version')!r}; "
                  f"Core's recovery_action: {diagnostic.get('recovery_action')})")
    return (f"task {problem['task_id']} in `{problem['root']}` exists but its state cannot be read: {problem['code']}{detail}. "
            "Managed assurance for it is UNKNOWN: not absent, not safe. Do not edit or delete its files. Read `status` for the "
            "diagnosis; the user decides whether to repair it (for example by running the plugin version that wrote it) or to "
            "start an independent new task with `task-start`. See `guidance --section recovery`.")


def unreadable_error(problem: dict) -> Exception:
    """The error a runner command raises for an associated task that cannot be read."""
    if problem.get("diagnostic"):
        return core.TaskStateSchemaIncompatible(problem["diagnostic"], problem["task_id"])
    return core.ThalirisError(problem["code"], f"{problem['code']}: the session's task {problem['task_id']} exists but its "
                              f"state cannot be read ({problem['message']})", recovery=problem_text(problem),
                              details={"task_id": problem["task_id"]})


def child_task(session_id: str | None) -> tuple[Path, str] | None:
    """The (root, task_id) whose lifecycle governs a session's children, even when the task state is unreadable."""
    association, problem = inspect_session(session_id)
    return association or ((problem["root"], problem["task_id"]) if problem else None)


def dispatch_for_agent(lifecycle: dict, agent_id: str) -> dict | None:
    matches = [item for item in lifecycle["dispatches"] if item.get("agent_id") == agent_id]
    return matches[0] if len(matches) == 1 else None


def snapshot_identity(lifecycle: dict, agent_id: str | None, agent_type: object) -> dict | None:
    """The role binding a running child was dispatched with.

    A child bound to exactly one dispatch of the same native type gets that
    dispatch's snapshot. A child not yet bound, whose type matches unresolved
    dispatches, gets the most restrictive of their snapshots: that is not an
    identity claim (no evidence binds to it), only the permissions any of them
    could have. Entries without a snapshot (older adapters) yield None.
    """
    if not isinstance(agent_type, str):
        return None
    if agent_id is not None:
        entry = dispatch_for_agent(lifecycle, agent_id)
        if entry is not None:
            snapshot = entry.get("role_binding")
            if entry.get("subagent_type") == agent_type and entry.get("binding") != "conflict" and isinstance(snapshot, dict):
                return {**snapshot, "bound": True}
            return None
    candidates = [item.get("role_binding") for item in lifecycle["dispatches"]
                  if item.get("subagent_type") == agent_type and item.get("agent_id") is None
                  and not item["disposed"] and item["native_status"] in UNRESOLVED_STATUSES]
    if not candidates or not all(isinstance(item, dict) for item in candidates):
        return None
    delegates = set(candidates[0].get("delegates") or [])
    for item in candidates[1:]:
        delegates &= set(item.get("delegates") or [])
    return {**candidates[0], "writes": all(item.get("writes") is True for item in candidates),
            "delegates": sorted(delegates), "bound": False}


def handoff(state: dict, handoff_id: str | None) -> dict | None:
    if handoff_id is None:
        return None
    for item in state.get("evidence_continuity", {}).get("handoffs", []):
        if item["id"] == handoff_id:
            return item
    return None


def new_handoff_id() -> str:
    return "H" + uuid.uuid4().hex[:8]


def reserve(ctx: dict, *, role: str, subagent_type: str, tool_use_id: str, parent: str,
            prompt: str, model: str | None, background: bool, association: dict | None,
            effort: str | None = None, role_binding: dict | None = None) -> dict:
    """Record one native dispatch and, when associated, its Core handoff.

    ``parent`` is "controller" or the delegating child's native agent id.
    Nested Scanner lineage inherits the parent's candidate with empty
    requirement refs: provenance does not establish global relevance.

    ``role_binding`` snapshots the role's identity and permissions as resolved
    at dispatch. Hooks resolve this child from the snapshot for the rest of its
    life, so a later edit or re-registration of the role never reinterprets it.
    Both new fields are optional additions to a version-1 entry: entries written
    before them fall back to resolving the role from the current registry.
    """
    lifecycle, state = ctx["lifecycle"], ctx["state"]
    if any(item.get("tool_use_id") == tool_use_id for item in lifecycle["dispatches"]):
        raise ValueError("DISPATCH_ALREADY_RESERVED")
    parent_dispatch = None if parent == "controller" else dispatch_for_agent(lifecycle, parent)
    record = None
    if parent_dispatch is not None:
        parent_handoff = handoff(state, parent_dispatch.get("handoff_id"))
        if parent_handoff is not None:
            record = {"id": new_handoff_id(), "parent_handoff_id": parent_handoff["id"],
                      "candidate": parent_handoff["candidate"], "requirement_refs": []}
    elif association is not None:
        record = {"id": new_handoff_id(), "parent_handoff_id": None,
                  "candidate": association["candidate"], "requirement_refs": association["requirement_refs"]}
        if association.get("durable_paths"):
            record["durable_paths"] = association["durable_paths"]
    if record is not None:
        record["producer"] = f"handoff:{record['id']}"
        core._register_evidence_handoff(state, record)
        state["revision"] += 1
        ctx["state_changed"] = True
    entry = {
        "tool_use_id": tool_use_id, "role": role, "subagent_type": subagent_type,
        "parent": parent, "handoff_id": record["id"] if record else None,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "requested_model": model, "requested_effort": effort, "role_binding": role_binding,
        "background": background, "agent_id": None,
        "binding": "unbound", "native_status": "reserved", "disposed": False,
        "created_at": now(), "events": [],
    }
    lifecycle["dispatches"].append(entry)
    ctx["lifecycle_changed"] = True
    return entry


def note(entry: dict, event: str, **fields) -> None:
    entry["events"] = (entry.get("events", []) + [{"event": event, "at": now(), **fields}])[-16:]


def bind(ctx: dict, entry: dict, agent_id: str, how: str) -> bool:
    """Bind a native agent id; conflicting identities stay UNKNOWN, never overwritten."""
    if entry.get("agent_id") not in (None, agent_id):
        note(entry, "identity-conflict", observed=native_hash(agent_id))
        entry["binding"] = "conflict"
        ctx["lifecycle_changed"] = True
        return False
    if any(other is not entry and other.get("agent_id") == agent_id for other in ctx["lifecycle"]["dispatches"]):
        note(entry, "identity-conflict", observed=native_hash(agent_id))
        ctx["lifecycle_changed"] = True
        return False
    entry["agent_id"] = agent_id
    if entry["binding"] != "exact":
        entry["binding"] = how
    ctx["lifecycle_changed"] = True
    return True


def set_status(ctx: dict, entry: dict, status: str, **fields) -> None:
    # A late launch acknowledgement never hides terminal evidence. A start after the
    # child finished (a SendMessage resume) or after its disposal is real activity,
    # but it grants nothing: the status stays and the event history records the fact.
    terminal = entry["native_status"] in TERMINAL_STATUSES
    if status == "launched" and terminal:
        note(entry, status, **fields)
        ctx["lifecycle_changed"] = True
        return
    if status == "running" and (terminal or entry["disposed"]):
        event = "resumed-after-terminal" if terminal else "started-after-dispose"
        if (entry.get("events") or [{}])[-1].get("event") != event:  # a resume loop must not push the history out
            note(entry, event, **fields)
            ctx["lifecycle_changed"] = True
        return
    # A start after an interim stop is the same handoff continuing (it never returned its final report).
    resumed = status == "running" and entry.pop("interim_stop", None) is not None
    if status in TERMINAL_STATUSES:
        entry.pop("interim_stop", None)
        entry.pop("interim_claims", None)  # ingested (or superseded) by the final report just before this
    entry["native_status"] = status
    note(entry, "resumed-after-interim" if resumed else status, **fields)
    ctx["lifecycle_changed"] = True


INTERIM_IDS_MAX = 16


def mark_interim(ctx: dict, entry: dict, background_task_ids: list[str]) -> None:
    """Record a turn end the Host keeps alive for the child's own live background work (not its final report).

    Only an exactly bound, undisposed, unfinished background handoff reaches here. The entry stays unresolved
    (`running`), so the child keeps the writes its handoff grants when it resumes; nothing about acceptance changes.
    The next stop without live background work of its own is the final report, and finishes it as before.
    """
    if entry["disposed"] or entry["native_status"] in TERMINAL_STATUSES:
        raise ValueError("INTERIM_STOP_ON_CLOSED_HANDOFF")  # a caller bug: a closed handoff is never reopened
    entry["native_status"] = "running"
    entry["interim_stop"] = {"at": now(), "background_task_ids": background_task_ids[:INTERIM_IDS_MAX]}
    entry["was_interim"] = True  # kept for the entry's life: the bounded event history may drop the event
    note(entry, "interim-stop", background_tasks=len(background_task_ids))
    ctx["lifecycle_changed"] = True


INTERIM_CLAIMS_MAX_CHARS = 64 * 1024


def keep_interim_claims(entry: dict, message: object) -> None:
    """Keep a bounded copy of an interim stop's message when it carries a readable claims block.

    A child often writes its report, claims block included, at the stop where it starts waiting, and ends with a bare
    message. The final stop ingests this copy when its own message has no readable block; the latest kept copy wins.
    """
    if not isinstance(message, str) or core.parse_claim_block(message)["status"] != "present":
        return
    if len(message) > INTERIM_CLAIMS_MAX_CHARS:
        note(entry, "interim-claims-not-kept", chars=len(message))
        return
    entry["interim_claims"] = {"message": message, "at": now()}
    note(entry, "interim-claims-kept", chars=len(message))


def host_agent_meta(transcript_path: object, agent_id: str) -> dict | None:
    """Read the Host's own record for one subagent, beside the session transcript.

    Claude Code writes `<session>/subagents/agent-<id>.meta.json` (toolUseId,
    agentType, parentAgentId, spawnDepth, model) and `.prefix.json` (actual tool
    names and model). The format is undocumented: anything missing or malformed
    is UNKNOWN, never a guess.
    """
    if not isinstance(transcript_path, str) or not native_id(agent_id):
        return None
    directory = Path(transcript_path).with_suffix("") / "subagents"
    try:
        meta = json.loads((directory / f"agent-{agent_id}.meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or not isinstance(meta.get("toolUseId"), str):
        return None
    try:
        prefix = json.loads((directory / f"agent-{agent_id}.prefix.json").read_text(encoding="utf-8")).get("state", {})
        meta["observed_tools"] = [name for name in prefix.get("toolNames", []) if isinstance(name, str)][:64]
        meta["observed_model"] = prefix.get("model") if isinstance(prefix.get("model"), str) else None
    except (OSError, ValueError, AttributeError):
        pass
    return meta


def bind_from_host(ctx: dict, agent_id: str, agent_type: object, transcript_path: object) -> dict | None:
    """Bind by the Host's exact toolUseId record; a mismatch is a recorded conflict."""
    current = dispatch_for_agent(ctx["lifecycle"], agent_id)
    if current is not None and current["binding"] in EXACT_BINDINGS:
        return current
    meta = host_agent_meta(transcript_path, agent_id)
    if meta is None:
        return current
    entry = next((item for item in ctx["lifecycle"]["dispatches"] if item["tool_use_id"] == meta["toolUseId"]), None)
    if entry is None:
        return current
    if agent_type != entry["subagent_type"] or meta.get("agentType", agent_type) != agent_type:
        note(entry, "identity-conflict", observed_type=str(agent_type)[:64])
        entry["binding"] = "conflict"
        ctx["lifecycle_changed"] = True
        return None
    return entry if bind(ctx, entry, agent_id, "host-meta") else None


def record_host_facts(ctx: dict, entry: dict, transcript_path: object) -> None:
    """Keep the Host-observed model and tool set next to what was requested (H027/H111)."""
    meta = host_agent_meta(transcript_path, entry["agent_id"]) if entry.get("agent_id") else None
    if meta is None:
        return
    # The prefix model is the resolved id; meta's `model` is only the requested alias.
    if meta.get("observed_model") and entry.get("observed_model") != meta["observed_model"]:
        entry["observed_model"] = meta["observed_model"]
        ctx["lifecycle_changed"] = True
    if meta.get("observed_tools") and entry.get("observed_tools") != meta["observed_tools"]:
        entry["observed_tools"] = meta["observed_tools"]
        ctx["lifecycle_changed"] = True


def active_scanner(lifecycle: dict, parent: str) -> dict | None:
    for item in lifecycle["dispatches"]:
        if item["parent"] == parent and not item["disposed"] and item["native_status"] in UNRESOLVED_STATUSES:
            return item
    return None


def unresolved(lifecycle: dict) -> list[dict]:
    return [item for item in lifecycle["dispatches"] if not item["disposed"] and item["native_status"] in UNRESOLVED_STATUSES]


def namespaced_observation(entry: dict, payload: dict) -> dict:
    """Prefix child-chosen ids with the handoff id so siblings never collide."""
    prefix = entry["handoff_id"] + "."
    local_sources = {source.get("id") for source in payload.get("sources", []) if isinstance(source, dict)}

    def rename(value):
        return prefix + value if isinstance(value, str) and not value.startswith(prefix) else value

    result = dict(payload)
    result["id"] = rename(payload.get("id"))
    if isinstance(payload.get("sources"), list):
        result["sources"] = [{**source, "id": rename(source.get("id"))} if isinstance(source, dict) else source for source in payload["sources"]]
    if isinstance(payload.get("source_refs"), list):
        result["source_refs"] = [rename(ref) if ref in local_sources else ref for ref in payload["source_refs"]]
    return result


OBSERVATION_FIELDS = {"id", "text", "source_refs", "sources", "observed_state", "handoff_id", "candidate", "requirement_refs"}


def observation_items(payload: dict) -> tuple[list[dict], bool]:
    """One observation object, or a batch `{"observations": [...]}` (1-64, one revision); and whether it was a batch."""
    if isinstance(payload, dict) and set(payload) == {"observations"}:
        items = payload["observations"]
        if not isinstance(items, list) or not 1 <= len(items) <= core.CONTINUITY_BATCH_MAX:
            raise core.ThalirisError("CONTINUITY_INVALID", "invalid observations batch",
                                     details={"collection": "observations",
                                              "expected": f"a list of 1 to {core.CONTINUITY_BATCH_MAX} objects"})
        return items, True
    return [payload], False


def observe(ctx: dict, payload: dict, *, agent_id: str | None, handoff_id: str | None) -> dict:
    """Append one observation, or an atomic batch, with adapter-supplied provenance.

    A child is identified by its bound native agent; the Controller imports a
    historical child result by exact handoff id and is marked as submitter.
    """
    state, lifecycle = ctx["state"], ctx["lifecycle"]
    items, batch = observation_items(payload)
    if not all(isinstance(item, dict) and not set(item) - OBSERVATION_FIELDS for item in items):
        raise ValueError("INVALID_OBSERVATION_FIELDS")
    if agent_id is not None:
        entry = dispatch_for_agent(lifecycle, agent_id)
        if entry is None or entry["binding"] not in EXACT_BINDINGS:
            raise ValueError("OBSERVATION_ASSOCIATION_UNAVAILABLE: report this observation and its sources in your final report")
        if entry["disposed"]:
            raise ValueError("DISPATCH_DISPOSED: this handoff was disposed by the Controller")
        if entry["native_status"] in TERMINAL_STATUSES:
            # A finished handoff grants nothing to a later turn of the same native agent (H113).
            raise ValueError("DISPATCH_FINISHED: this handoff already returned; new work needs a fresh child")
        record = handoff(state, entry["handoff_id"])
        if record is None:
            raise ValueError("OBSERVATION_ASSOCIATION_UNAVAILABLE: this handoff carries no evidence association; report it in your final report")
        items = [namespaced_observation(entry, item) for item in items]
        extra = {"native_actor": native_hash(agent_id)}
    else:
        record = handoff(state, handoff_id)
        if record is None:
            raise ValueError("HANDOFF_NOT_FOUND")
        extra = {"submitted_by": "controller"}
    stamped = []
    for item in items:
        for field, expected in (("handoff_id", record["id"]), ("candidate", record["candidate"]), ("requirement_refs", record["requirement_refs"])):
            if field in item and item[field] != expected:
                raise ValueError(f"OBSERVATION_PROVENANCE_CONFLICT: {field}")
        stamped.append({**item, "handoff_id": record["id"], "candidate": record["candidate"],
                        "requirement_refs": record["requirement_refs"], **extra})
    appended = core.append_observations(state, [(record["producer"], item) for item in stamped])
    ctx["state_changed"] = True
    result = {"handoff_id": record["id"], "candidate": record["candidate"], "appended": appended}
    if not batch:
        result["observation_id"] = appended[0]
    return result


def append_controller(ctx: dict, base_revision: int, payload: dict, kind: str, *,
                      override_reason: str | None = None) -> tuple[list[str], list[str]]:
    """Controller register/judge under the same lock and Core's revision CAS (a conflict carries its details).

    ``root`` reaches Core's withholding gate: without it a task with declared checks has an UNKNOWN tree and every
    gated `satisfied` would be withheld. The gate refuses (VERIFICATION_WITHHELD); only the Controller's recorded
    ``override_reason`` lifts it. Returns (appended ids, mechanical observation ids Core added in the same revision).
    """
    core.check_revision(ctx["state"], base_revision)
    if override_reason is not None:
        payload = core._with_override_reason(payload, kind, override_reason)
    observed = len(ctx["state"].get("evidence_continuity", {}).get("observations", []))
    appended = core._append_continuity_state(ctx["state"], "controller", payload, kind, root=ctx["root"])
    ctx["state_changed"] = True
    mechanical = [item["id"] for item in ctx["state"]["evidence_continuity"]["observations"][observed:]
                  if item.get("provenance") == core.MECHANISM_PRODUCER] if kind == "judgments" else []
    return appended, mechanical


# ------------------------------------------------------------ adaptive lane marker
#
# The lane counter adds every Controller tool result to an adaptive task's token counter. PostToolUse for Read/Bash/...
# is most of a session's hook calls, so the shell launcher must decide without starting Python whether this session
# counts at all: it reads `session_id` from the payload by parameter expansion and exits unless `lane/<session_id>`
# exists under the adapter home. The marker is written when an adaptive task is admitted or attached (or a task is
# switched to adaptive) and removed on close, detach or a switch away; SessionStart re-syncs it. A session id that is
# not a plain filename gets a hashed marker the launcher finds only by its any-marker fallback. The marker only lets
# Python start; Python re-checks the association and the anchored mode, so a stale marker counts nothing.

_LANE_SAFE = re.compile(r"[A-Za-z0-9-]{1,128}")


def lane_marker(session_id: str) -> Path:
    name = session_id if _LANE_SAFE.fullmatch(session_id) else "_" + native_hash(session_id)
    return home() / "lane" / name


def sync_lane_marker(session_id: str | None, association: tuple[Path, str] | None = None) -> bool:
    """Write or remove this session's marker from its associated task's anchored mode; True when it now counts."""
    if not native_id(session_id):
        return False
    adaptive = False
    association = association if association is not None else resolve_session(session_id)
    if association is not None:
        try:
            record = authority(*association).read() or {}
            adaptive = record.get("status") == "ACTIVE" and record.get("contract", {}).get("execution_mode") == "adaptive"
        except (OSError, ValueError):
            adaptive = False
    marker = lane_marker(session_id)
    try:
        if adaptive:
            marker.parent.mkdir(parents=True, exist_ok=True)
            if not marker.is_file():
                core._atomic_write(marker, b"adaptive\n")
        else:
            marker.unlink(missing_ok=True)
    except OSError:
        return False
    return adaptive


def current_records(state: dict, limit: int = 12, text_chars: int = 600) -> tuple[list[dict], int]:
    """The most recent Controller records no later record supersedes (bounded), and their total count."""
    superseded = {ref for item in state["records"] for ref in item.get("supersedes", [])}
    current = [item for item in state["records"] if item["id"] not in superseded]
    shown = [{"id": item["id"], "kind": item["kind"], "status": item["status"], "revision": item["revision"],
              "text": item["text"][:text_chars], "truncated": len(item["text"]) > text_chars,
              **({"supersedes": item["supersedes"]} if item.get("supersedes") else {})} for item in current[-limit:]]
    return shown, len(current)


def identity_summary(root: Path) -> str | None:
    """Core's one-line project identity summary, or None when absent or unreadable.

    Navigation only: a hook must degrade rather than fail, and identity text is read
    on demand (`identity-get`), never injected.
    """
    try:
        return identity.identity_summary(root)
    except Exception:  # noqa: BLE001 - OSError, bounded-read refusals, a repository Core cannot resolve
        return None


def task_context_text(state: dict, budget: int = 6000) -> str | None:
    """A bounded summary of the Controller's dynamic task context for re-injection.

    It is what the Controller recorded with `task-update`, presented again after
    compaction or resume so global semantic state is not re-investigated. It is
    history the Controller wrote, not fresh verification.
    """
    records, total = current_records(state, limit=12, text_chars=400)
    if not (state.get("current_milestone") or state["active_work"] or state["pending_results"] or records):
        return None
    lines = [f"Task context recorded by the Controller (revision {state['revision']}; history, re-verify what a decision depends on):"]
    if state.get("current_milestone"):
        lines.append(f"- Milestone: {state['current_milestone']}")
    for title, values in (("Active Work", state["active_work"]), ("Pending Results", state["pending_results"])):
        if values:
            lines.append(f"- {title}:")
            lines += [f"  - {value[:300]}" for value in values[:16]]
            if len(values) > 16:
                lines.append(f"  - … {len(values) - 16} more in `status`")
    if records:
        lines.append(f"- Current records ({len(records)} most recent of {total} not superseded):")
        lines += [f"  - {item['id']} [{item['kind']}] {item['text']}{' …' if item['truncated'] else ''}" for item in records]
    text, suffix = "\n".join(lines), "\n- … truncated; read the rest with `status` or `get <record id>`."
    return text if len(text) <= budget else text[:budget - len(suffix)] + suffix


def start_task(root: Path, goal: str, contract: dict, session_id: str) -> dict:
    contract = validate_contract(contract)
    validate_constraint(contract)
    if not is_managed_project(root):
        core.init(root)
    result = core.task_start(root, goal, None, None, actor="claude:controller")
    task_id = result["task_id"]
    core.select_task(root, task_id)
    store = authority(root, task_id)
    store.establish(core.task_show(root)["state"], contract, adapter_fields={
        "claude_session_sha256": native_hash(session_id), "host_actor_assurance": "UNKNOWN"})
    write_session(session_id, root, task_id, "task-start")
    sync_lane_marker(session_id, (root, task_id))
    return result
