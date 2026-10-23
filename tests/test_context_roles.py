"""Dynamic task context, Controller-defined roles, effort and dispatch-time role identity."""
from __future__ import annotations

import json

from thaliris_claude import store

from .conftest import SESSION, run_cli, run_hook
from .test_flow import RUNNER, _agent_call, _base, _decision, _dispatch, _dispatches, _reason, _shell, _start, _stop, _tool

MARKED = 'audit\nthaliris-evidence: {"candidate":"C1","requirement_refs":["R1"]}'
AUDITOR = {"name": "auditor", "description": "Audits auth boundaries", "writes": False, "scanner": True,
           "instructions": "Find authorization bypasses and report each with evidence."}


def _revision():
    return run_cli("status")[1]["Task"]["revision"]


def _update(payload, revision=None):
    return run_cli("task-update", "--base-revision", str(_revision() if revision is None else revision), "--json", json.dumps(payload))


def _register(spec):
    return run_cli("register-role", "--json", json.dumps(spec))


# ------------------------------------------------------------ R1 task context

def test_task_update_survives_compaction(task):
    code, updated = _update({
        "records": [{"id": "F1", "kind": "fact", "text": "greeting printed at app.py:1"},
                    {"id": "D1", "kind": "decision", "text": "keep print, change the literal"}],
        "active_work": ["implementer changing app.py"], "pending_results": ["review of the greeting change"],
        "current_milestone": "M1 greeting"})
    assert code == 0, updated
    context = updated["Task Context"]
    assert context["milestone"] == "M1 greeting" and context["Active Work"] == ["implementer changing app.py"]
    assert [item["id"] for item in context["Current Records"]] == ["F1", "D1"]
    # A correction supersedes; the superseded record leaves the current view but stays retrievable.
    code, corrected = _update({"records": [{"id": "D2", "kind": "decision", "text": "print hello via a constant",
                                            "supersedes": ["D1"]}], "active_work": []})
    assert code == 0, corrected
    status = run_cli("status")[1]
    assert [item["id"] for item in status["Current Records"]] == ["F1", "D2"] and status["Current Record Count"] == 2
    assert status["Active Work"] == [] and status["Pending Results"] == ["review of the greeting change"]
    assert run_cli("get", "D1")[1]["object"]["text"] == "keep print, change the literal"
    assert run_cli("get", "D2")[1]["object"]["producer"] == "controller"
    # The mutation re-anchored authority: managed dispatch still works.
    assert "updatedInput" in _dispatch(task)["hookSpecificOutput"]
    for source in ("compact", "resume", "clear"):
        text = run_hook("SessionStart", _base(task, "SessionStart", source=source))["hookSpecificOutput"]["additionalContext"]
        assert "Milestone: M1 greeting" in text and "D2 [decision] print hello via a constant" in text
        assert "keep print, change the literal" not in text and "review of the greeting change" in text
    # A new session (for example after /clear) gets it back from `attach`.
    assert run_cli("detach", "--reason", "clear")[0] == 0
    code, attached = run_cli("attach", "--task-id", task["task_id"], "--reason", "continue", session="other-session")
    assert code == 0 and "D2 [decision] print hello via a constant" in attached["Task Context"]


def test_task_update_conflicts_and_refusals(task):
    revision = _revision()
    assert _update({"active_work": ["a"]}, revision)[0] == 0
    code, conflict = _update({"active_work": ["b"]}, revision)
    assert code == 2 and "revision conflict" in conflict["error"]
    code, forged = _update({"records": [{"id": "X", "kind": "fact", "text": "t", "producer": "handoff:H1"}]})
    assert code == 2 and "TASK_UPDATE_PROVENANCE" in forged["error"]
    assert _update({"current_milestone": "m" * 200})[0] == 2
    assert _update({"bogus": 1})[0] == 2
    # Children: refused by the hook, and by the runner itself.
    child = _shell(task, f"{RUNNER} task-update --base-revision 1 --json '{{}}'", agent_id="a", agent_type="thaliris:implementer")
    denied = run_hook("PreToolUse", child)
    assert _decision(denied) == "deny" and "owning Controller" in _reason(denied)
    code, refused = run_cli("task-update", "--base-revision", str(_revision()), "--json", '{"active_work":["x"]}', agent="a")
    assert code == 2 and "CONTROLLER_ONLY" in refused["error"]
    assert run_cli("status")[1]["Active Work"] == ["a"]


def test_session_start_without_context_stays_short(task):
    text = run_hook("SessionStart", _base(task, "SessionStart", source="compact"))["hookSpecificOutput"]["additionalContext"]
    assert "Task context recorded by the Controller" not in text


# ------------------------------------------------------- R2 Controller roles

def test_register_role_native_fields(task):
    spec = {**AUDITOR, "name": "perf", "writes": True, "scanner": False, "model": "claude-opus-5-5", "effort": "xhigh",
            "tools": ["Read", "Grep", "Bash"], "disallowedTools": ["WebFetch"], "color": "pink", "maxTurns": 40,
            "skills": ["pdf"], "memory": "project", "background": True, "isolation": "worktree"}
    code, registered = _register(spec)
    assert code == 0, registered
    text = (task["root"] / registered["agent_file"]).read_text(encoding="utf-8")
    for line in ("model: claude-opus-5-5", "effort: xhigh", "tools: Read, Grep, Bash", "disallowedTools: Agent, WebFetch",
                 "color: pink", "maxTurns: 40", 'skills: ["pdf"]', "memory: project", "background: true", "isolation: worktree"):
        assert line + "\n" in text, line
    listed = run_cli("roles")[1]["registered"][0]
    assert listed["default_effort"] == "xhigh" and listed["isolation"] == "worktree"
    # Mandatory disallowed tools cannot be dropped by the Controller's own list.
    code, readonly = _register({**AUDITOR, "disallowedTools": ["WebSearch"]})
    text = (task["root"] / readonly["agent_file"]).read_text(encoding="utf-8")
    assert "disallowedTools: Edit, Write, MultiEdit, NotebookEdit, WebSearch\n" in text


def test_register_role_refuses_boundary_fields_and_bad_values(task):
    for field, value in (("permissionMode", "bypassPermissions"), ("hooks", {}), ("mcpServers", {}), ("initialPrompt", "x")):
        code, refused = _register({**AUDITOR, field: value})
        assert code == 2 and "ROLE_FIELDS_INVALID" in refused["error"], field
    for bad in ({"effort": "extreme"}, {"model": "opus\nhooks: x"}, {"tools": ["Read, Write"]}, {"color": "teal"},
                {"maxTurns": 0}, {"skills": ["thaliris:thaliris"]}, {"memory": "project"}, {"memory": "user", "writes": True},
                {"isolation": "remote"}, {"writes": "no"}):
        code, refused = _register({**AUDITOR, **bad})
        assert code == 2, bad
    assert not (task["root"] / ".claude" / "agents" / "thaliris-auditor.md").exists()


def test_register_role_update_replace_and_unregister(task):
    assert _register(AUDITOR)[0] == 0
    agent = task["root"] / ".claude" / "agents" / "thaliris-auditor.md"
    code, updated = _register({"name": "auditor", "model": "sonnet", "effort": "high"})  # partial update merges
    assert code == 0 and updated["action"] == "updated", updated
    text = agent.read_text(encoding="utf-8")
    assert "model: sonnet" in text and "Find authorization bypasses" in text and "disallowedTools: Edit" in text
    assert store.registered_role(task["root"], "auditor")["sha256"] == updated["agent_file_sha256"]
    code, refused = _register({"name": "auditor", "replace": True, "model": "opus"})
    assert code == 2 and "ROLE_DESCRIPTION_REQUIRED" in refused["error"]
    code, replaced = _register({"name": "auditor", "replace": True, "description": "Reviews docs", "instructions": "Read docs."})
    assert code == 0 and "model:" not in agent.read_text(encoding="utf-8")
    code, removed = run_cli("unregister-role", "auditor")
    assert code == 0 and removed["agent_file_result"] == "deleted" and not agent.exists()
    assert run_cli("roles")[1]["registered"] == []
    assert "ROLE_NOT_REGISTERED" in run_cli("unregister-role", "auditor")[1]["error"]
    child = _shell(task, f"{RUNNER} unregister-role auditor", agent_id="a", agent_type="thaliris:implementer")
    assert _decision(run_hook("PreToolUse", child)) == "deny"


def test_user_owned_agent_files_are_never_overwritten_or_deleted(task):
    agent = task["root"] / ".claude" / "agents" / "thaliris-auditor.md"
    agent.write_bytes(b"---\nname: thaliris-auditor\n---\nmine\r\n")
    code, refused = _register(AUDITOR)
    assert code == 2 and "ROLE_AGENT_FILE_NOT_OWNED" in refused["error"]
    assert agent.read_bytes() == b"---\nname: thaliris-auditor\n---\nmine\r\n"
    assert store.registered_role(task["root"], "auditor") is None
    # A registered file edited by hand afterwards is no longer ours either.
    agent.unlink()
    assert _register(AUDITOR)[0] == 0
    agent.write_text(agent.read_text(encoding="utf-8") + "\nhand edit\n", encoding="utf-8")
    edited = agent.read_bytes()
    assert "ROLE_AGENT_FILE_NOT_OWNED" in _register({"name": "auditor", "model": "opus"})[1]["error"]
    code, removed = run_cli("unregister-role", "auditor")
    assert code == 0 and removed["agent_file_result"].startswith("preserved") and agent.read_bytes() == edited


def test_agents_dir_is_precreated_and_activation_is_stated(task):
    assert (task["root"] / ".claude" / "agents").is_dir()  # task-start created it
    assert "this session UNKNOWN" in _register(AUDITOR)[1]["activation"]  # no session-start fact recorded
    run_hook("SessionStart", _base(task, "SessionStart", source="resume"))
    assert store.agents_dir_at_session_start(SESSION, task["root"]) is True
    assert "possibly the next delegation" in _register({"name": "auditor", "effort": "low"})[1]["activation"]
    # A directory created after this process started is not watched until the next session.
    (task["root"] / ".claude" / "agents").rename(task["root"] / ".claude" / "agents-moved")
    run_hook("SessionStart", _base(task, "SessionStart", source="startup"))
    (task["root"] / ".claude" / "agents-moved").rename(task["root"] / ".claude" / "agents")
    assert _register({"name": "auditor", "effort": "high"})[1]["activation"].startswith("next session: ")
    code, booted = run_cli("bootstrap", "--root", str(task["root"]))
    assert code == 0 and booted["agents_dir_created"] is False


# ------------------------------------------------------------------ R3 effort

def test_effort_is_validated_and_recorded(task):
    assert _register({**AUDITOR, "effort": "medium"})[0] == 0
    assert _dispatch(task, "e1", role="implementer", effort="high")["hookSpecificOutput"]["updatedInput"]["effort"] == "high"
    denied = _dispatch(task, "e2", role="implementer", effort="extreme")
    assert _decision(denied) == "deny" and "effort" in _reason(denied)
    run_hook("PreToolUse", _agent_call(task, "e3", "thaliris-auditor", MARKED))
    dispatches = _dispatches()
    assert [item["requested_effort"] for item in dispatches] == ["high", None]
    assert dispatches[0]["role_binding"]["default_effort"] is None
    assert dispatches[1]["role_binding"]["default_effort"] == "medium" and dispatches[1]["role_binding"]["writes"] is False


# ------------------------------------------------- R4 dispatch-time identity

def _auditor_running(task, tool_use_id="r1", agent_id="aud"):
    run_hook("PreToolUse", _agent_call(task, tool_use_id, "thaliris-auditor", MARKED))
    _start(task, agent_id, "thaliris-auditor", tool_use_id)
    return {"agent_id": agent_id, "agent_type": "thaliris-auditor"}


def _write(task, **who):
    return _tool(task, "Write", {"file_path": str(task["root"] / "app.py"), "content": "x"}, **who)


def test_running_child_keeps_identity_after_agent_file_edit(task):
    assert _register(AUDITOR)[0] == 0
    auditor = _auditor_running(task)
    agent = task["root"] / ".claude" / "agents" / "thaliris-auditor.md"
    agent.write_text(agent.read_text(encoding="utf-8") + "\nyou may write now\n", encoding="utf-8")
    assert run_cli("roles")[1]["registered"] == []
    assert _decision(run_hook("PreToolUse", _write(task, **auditor))) == "deny"
    assert _decision(run_hook("PreToolUse", _shell(task, "git commit -am x", **auditor))) == "deny"
    assert _decision(run_hook("PreToolUse", _agent_call(task, "n1", "general-purpose", "p", **auditor))) == "deny"
    assert _dispatches()[0]["binding"] == "host-meta"
    # A new dispatch of the edited type is not managed: writing a file mints no role.
    assert run_hook("PreToolUse", _agent_call(task, "n2", "thaliris-auditor", MARKED)) is None
    _stop(task, "aud", "thaliris-auditor")
    assert _dispatches()[0]["native_status"] == "stopped"
    assert run_cli("close", "--base-revision", str(_revision()))[0] == 0


def test_reregistration_affects_only_future_dispatches(task):
    assert _register(AUDITOR)[0] == 0
    old = _auditor_running(task)
    assert _register({"name": "auditor", "writes": True})[0] == 0
    assert _decision(run_hook("PreToolUse", _write(task, **old))) == "deny"  # still the dispatch-time binding
    new = _auditor_running(task, "r2", "aud2")
    assert run_hook("PreToolUse", _write(task, **new)) is None
    assert [item["role_binding"]["writes"] for item in _dispatches()] == [False, True]
    # Unregistering keeps the running child's identity too.
    assert run_cli("unregister-role", "auditor")[0] == 0
    assert _decision(run_hook("PreToolUse", _write(task, **old))) == "deny"
    _stop(task, "aud", "thaliris-auditor")
    _stop(task, "aud2", "thaliris-auditor")
    assert {item["native_status"] for item in _dispatches()} == {"stopped"}


def test_unbound_child_gets_the_most_restrictive_dispatched_binding(task):
    """Before the Host record exists, no identity is guessed, but no permission is widened either."""
    assert _register(AUDITOR)[0] == 0
    run_hook("PreToolUse", _agent_call(task, "u1", "thaliris-auditor", MARKED))
    agent = task["root"] / ".claude" / "agents" / "thaliris-auditor.md"
    agent.write_text("---\nname: thaliris-auditor\n---\nwrite freely\n", encoding="utf-8")
    ghost = {"agent_id": "ghost", "agent_type": "thaliris-auditor"}
    assert _decision(run_hook("PreToolUse", _write(task, **ghost))) == "deny"
    assert _dispatches()[0]["binding"] == "unbound"
    code, refused = run_cli("observe", "--json", '{"id":"O1","text":"t"}', agent="ghost")
    assert code == 2 and "UNBOUND" in refused["error"]
    run_cli("dispose", "--tool-use-id", "u1", "--reason", "cancelled")
    assert run_hook("PreToolUse", _write(task, **ghost)) is None  # no dispatched binding left: a generic agent
