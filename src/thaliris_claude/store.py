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

from thaliris import core
from thaliris.authority import AuthorityStore, validate_contract

# Only Thaliris-owned config is anchored. Anchoring user-owned Claude settings made
# legitimate human edits look like tampering, and recovery would revert their bytes
# (history H094/H098). Children are still refused writes to .claude/settings*.json.
PROTECTED_PATHS = (".context/config.json",)
MODEL_ALIASES = ("sonnet", "opus", "haiku", "fable", "inherit")
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


def validate_constraint(contract: dict) -> str | None:
    """Return the frozen model alias selected by a `claude-model:<alias>` constraint."""
    value = contract.get("execution_constraint")
    if value is None:
        return None
    prefix, _, alias = str(value).partition(":")
    if prefix != "claude-model" or alias not in MODEL_ALIASES[:-1]:
        raise ValueError("UNSUPPORTED_EXECUTION_CONSTRAINT")
    return alias


def clear_session(session_id: str | None) -> None:
    if native_id(session_id):
        _session_path(session_id).unlink(missing_ok=True)
        forget_packet(session_id)


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


def read_lifecycle(root: Path, task_id: str) -> dict:
    path = lifecycle_path(root, task_id)
    if not path.is_file():
        return {"version": LIFECYCLE_VERSION, "task_id": task_id, "dispatches": []}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("task_id") != task_id or not isinstance(value.get("dispatches"), list):
        raise ValueError("CLAUDE_LIFECYCLE_INVALID")
    if value.get("version") != LIFECYCLE_VERSION:
        # A newer plugin wrote this (or an older one, after a downgrade). Never
        # reinterpret it: identity facts stay UNKNOWN until a matching version runs.
        raise ValueError(f"CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED: {value.get('version')!r}; this adapter reads version {LIFECYCLE_VERSION}")
    return value


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
        ctx = {"root": root, "task_id": task_id, "authority": record, "store": store,
               "state": core._load_state(root, active=not observe_only), "lifecycle": read_lifecycle(root, task_id),
               "state_changed": False, "lifecycle_changed": False}
        yield ctx
        if ctx["state_changed"]:
            if observe_only:
                raise ValueError("OBSERVE_ONLY_TRANSACTION")
            core._write_state(root, ctx["state"])
            store.checkpoint()
        if ctx["lifecycle_changed"]:
            write_lifecycle(root, task_id, ctx["lifecycle"])


def resolve_session(session_id: str | None) -> tuple[Path, str] | None:
    """Return the associated (root, task_id) when its Core task is still ACTIVE."""
    association = read_session(session_id)
    if association is None:
        return None
    root = Path(association["root"])
    try:
        core.select_task(root, association["task_id"])
        state = core._load_state(root)
    except (ValueError, OSError):
        return None
    return (root, association["task_id"]) if state["status"] == "ACTIVE" else None


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
    # A late launch acknowledgement never hides terminal evidence. A new start
    # (a resumed agent) is real activity; the event history keeps the old fact.
    if status == "launched" and entry["native_status"] in {"completed", "stopped"}:
        note(entry, status, **fields)
        ctx["lifecycle_changed"] = True
        return
    entry["native_status"] = status
    note(entry, status, **fields)
    ctx["lifecycle_changed"] = True


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


def observe(ctx: dict, payload: dict, *, agent_id: str | None, handoff_id: str | None) -> dict:
    """Append one observation with adapter-supplied provenance.

    A child is identified by its bound native agent; the Controller imports a
    historical child result by exact handoff id and is marked as submitter.
    """
    state, lifecycle = ctx["state"], ctx["lifecycle"]
    if not isinstance(payload, dict) or set(payload) - {"id", "text", "source_refs", "sources", "observed_state", "handoff_id", "candidate", "requirement_refs"}:
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
        payload = namespaced_observation(entry, payload)
        extra = {"native_actor": native_hash(agent_id)}
    else:
        record = handoff(state, handoff_id)
        if record is None:
            raise ValueError("HANDOFF_NOT_FOUND")
        extra = {"submitted_by": "controller"}
    for field, expected in (("handoff_id", record["id"]), ("candidate", record["candidate"]), ("requirement_refs", record["requirement_refs"])):
        if field in payload and payload[field] != expected:
            raise ValueError(f"OBSERVATION_PROVENANCE_CONFLICT: {field}")
    payload = {**payload, "handoff_id": record["id"], "candidate": record["candidate"],
               "requirement_refs": record["requirement_refs"], **extra}
    core._append_continuity_state(state, record["producer"], payload, "observations")
    ctx["state_changed"] = True
    return {"observation_id": payload["id"], "handoff_id": record["id"], "candidate": record["candidate"]}


def append_controller(ctx: dict, base_revision: int, payload: dict, kind: str) -> None:
    """Controller register/judge under the same lock and revision CAS."""
    if ctx["state"]["revision"] != base_revision:
        raise ValueError("task revision conflict")
    core._append_continuity_state(ctx["state"], "controller", payload, kind)
    ctx["state_changed"] = True


def current_records(state: dict, limit: int = 12, text_chars: int = 600) -> tuple[list[dict], int]:
    """The most recent Controller records no later record supersedes (bounded), and their total count."""
    superseded = {ref for item in state["records"] for ref in item.get("supersedes", [])}
    current = [item for item in state["records"] if item["id"] not in superseded]
    shown = [{"id": item["id"], "kind": item["kind"], "status": item["status"], "revision": item["revision"],
              "text": item["text"][:text_chars], "truncated": len(item["text"]) > text_chars,
              **({"supersedes": item["supersedes"]} if item.get("supersedes") else {})} for item in current[-limit:]]
    return shown, len(current)


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
    return result
