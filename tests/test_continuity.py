"""Continuity: a legal upgrade, resume or local fault never strands the original task, and never loosens a refusal.

Each group locks one boundary: a task that exists but cannot be read is not "no task"; a finished or disposed child is
never reactivated by a second start; writes follow the handoff's terminal state; an unreadable lifecycle fails closed for
children only; a damaged entry is a coded error, not a crash; the 0.1 lifecycle shape still reads.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from thaliris import core

from thaliris_claude import hooks, store

from .conftest import SESSION, run_cli, run_hook
from .test_flow import (RUNNER, _agent_call, _base, _decision, _dispatch, _dispatches, _handoff, _reason, _result,
                        _shell, _start, _stop, _tool)

IMPL = {"agent_id": "impl", "agent_type": "thaliris:implementer"}


def _state_file(task):
    return task["root"] / ".context" / "tasks" / task["task_id"] / "state.json"


def _lifecycle_file(task):
    return store.lifecycle_path(task["root"], task["task_id"])


def _set_schema(task, version):
    original = _state_file(task).read_bytes()
    state = json.loads(original)
    state["schema_version"] = version
    _state_file(task).write_text(json.dumps(state), encoding="utf-8")
    return original


def _write(task, **who):
    return _tool(task, "Write", {"file_path": str(task["root"] / "app.py"), "content": "x"}, **who)


def _running_implementer(task, tool_use_id="tu1"):
    handoff = _handoff(_dispatch(task, tool_use_id, role="implementer"))
    _start(task, "impl", "thaliris:implementer", tool_use_id)
    return handoff


def _entry(index=0):
    return store.read_lifecycle(*store.resolve_session(SESSION))["dispatches"][index]


# ------------------------------------------------- A: a task that exists but cannot be read is not "no task"

@pytest.mark.parametrize("version", [core._STATE_SCHEMA_VERSION + 1, 1])
def test_incompatible_state_schema_is_reported_and_dispatch_fails_closed(task, version):
    original = _set_schema(task, version)
    association, problem = store.inspect_session(SESSION)
    assert association is None and problem["code"] == "STATE_SCHEMA_INCOMPATIBLE" and problem["task_id"] == task["task_id"]

    context = run_hook("SessionStart", _base(task, "SessionStart", source="resume"))["hookSpecificOutput"]["additionalContext"]
    assert task["task_id"] in context and "STATE_SCHEMA_INCOMPATIBLE" in context and "recovery" in context
    assert "associated with ACTIVE Thaliris task" not in context  # never presented as a healthy task

    denied = run_hook("PreToolUse", _agent_call(task, "tu1", "thaliris:implementer", "work"))
    assert _decision(denied) == "deny" and task["task_id"] in _reason(denied) and "STATE_SCHEMA_INCOMPATIBLE" in _reason(denied)
    assert "updatedInput" not in denied["hookSpecificOutput"] and not _lifecycle_file(task).exists()  # nothing reserved
    assert run_hook("PreToolUse", _agent_call(task, "tu2", "general-purpose", "work")) is None  # not a Thaliris dispatch

    code, status = run_cli("status")
    assert code == 3 and status["status"] == "STATE_SCHEMA_INCOMPATIBLE" and status["task_id"] == task["task_id"]
    assert "NO_ACTIVE_TASK_FOR_SESSION" not in json.dumps(status) and "recovery" in status
    explicit = run_cli("status", "--task-id", task["task_id"])[1]  # the path that always gave the right diagnosis
    assert explicit["status"] == "STATE_SCHEMA_INCOMPATIBLE" and explicit["state_sha256"] == status["state_sha256"]
    code, refused = run_cli("task-update", "--base-revision", "1", "--milestone", "m")
    assert code == 3 and refused["status"] == "STATE_SCHEMA_INCOMPATIBLE"  # mutators say the same

    # The Controller's own file work is untouched, and restoring the bytes restores everything.
    assert run_hook("PreToolUse", _write(task)) is None
    _state_file(task).write_bytes(original)
    assert "updatedInput" in _dispatch(task)["hookSpecificOutput"]
    assert run_cli("status")[0] == 0


def test_damaged_state_json_is_unreadable_not_absent(task):
    _state_file(task).write_text("{not json", encoding="utf-8")
    association, problem = store.inspect_session(SESSION)
    assert association is None and problem["code"] == "TASK_STATE_INVALID"
    assert _decision(_dispatch(task)) == "deny"
    code, error = run_cli("status")
    assert code == 2 and error["code"] == "TASK_STATE_INVALID" and "NO_ACTIVE_TASK" not in json.dumps(error)


def test_missing_or_closed_state_is_still_no_task(task):
    """Only an unreadable task fails closed: a closed or absent task keeps the ordinary non-managed path."""
    _state_file(task).unlink()
    assert store.inspect_session(SESSION) == (None, None)
    assert _dispatch(task) is None
    assert "NO_ACTIVE_TASK_FOR_SESSION" in run_cli("status")[1]["error"]


def test_no_task_path_reads_no_task_state_and_starts_no_process(home, repo, monkeypatch):
    """The ordinary no-task dispatch decides from the session record alone: no state load, no lifecycle, no subprocess."""
    def forbidden(*args, **kwargs):
        raise AssertionError("the no-task path must not do this")

    monkeypatch.setattr(core, "_load_state", forbidden)
    monkeypatch.setattr(store, "read_lifecycle", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    payload = {"session_id": SESSION, "tool_name": "Agent", "tool_use_id": "t", "cwd": str(repo),
               "tool_input": {"prompt": "p", "subagent_type": "thaliris:investigator"}}
    assert hooks.pre_tool_use(payload) is None


# ------------------------------------------------- B: a finished or disposed child is never reactivated

def test_second_start_does_not_reactivate_a_finished_child(task):
    _running_implementer(task)
    _stop(task, "impl", "thaliris:implementer")
    assert _entry()["native_status"] == "stopped"

    resumed = _start(task, "impl", "thaliris:implementer")  # a SendMessage resume fires SubagentStart again
    assert _entry()["native_status"] == "stopped"
    assert [item["event"] for item in _entry()["events"]][-1] == "resumed-after-terminal"
    assert "observe --text" not in resumed["hookSpecificOutput"]["additionalContext"]  # no instructions to record evidence
    code, refused = run_cli("observe", "--text", "late", "--source", "app.py:1", agent="impl")
    assert code == 2 and refused["code"] == "DISPATCH_FINISHED"
    assert _decision(run_hook("PreToolUse", _write(task, **IMPL))) == "deny"
    _stop(task, "impl", "thaliris:implementer")
    assert _entry()["native_status"] in store.TERMINAL_STATUSES
    _start(task, "impl", "thaliris:implementer")
    _start(task, "impl", "thaliris:implementer")
    assert [item["event"] for item in _entry()["events"]].count("resumed-after-terminal") == 2  # two stop-start rounds; the repeated start is not re-noted
    revision = run_cli("status")[1]["Task"]["revision"]
    assert run_cli("close", "--base-revision", str(revision))[0] == 0  # a resumed child never blocks closure


def test_completed_tool_result_is_terminal_too(task):
    _running_implementer(task)
    _result(task, "tu1", {"status": "completed", "agentId": "impl"})
    assert _entry()["native_status"] == "completed"
    _start(task, "impl", "thaliris:implementer")
    assert _entry()["native_status"] == "completed"
    assert _decision(run_hook("PreToolUse", _write(task, **IMPL))) == "deny"


def test_start_after_dispose_keeps_the_fence(task):
    handoff = _handoff(_dispatch(task, "tu1", role="implementer"))
    assert run_cli("dispose", "--handoff-id", handoff, "--reason", "user cancelled")[0] == 0
    context = _start(task, "impl", "thaliris:implementer", "tu1")["hookSpecificOutput"]["additionalContext"]
    assert _entry()["native_status"] == "reserved" and _entry()["disposed"] is True
    assert _entry()["events"][-1]["event"] == "started-after-dispose" and "disposed" in context
    assert run_cli("observe", "--text", "x", "--source", "y", agent="impl")[0] == 2
    assert _decision(run_hook("PreToolUse", _write(task, **IMPL))) == "deny"
    assert run_cli("status")[1]["Dispatches"]["open"][0]["disposed"] is True


# ------------------------------------------------- C: writes follow the handoff's terminal state

def test_running_child_writes_until_it_finishes_then_is_fenced(task):
    _running_implementer(task)
    assert run_hook("PreToolUse", _write(task, **IMPL)) is None  # SubagentStop has not happened: not fenced
    assert run_hook("PreToolUse", _shell(task, "echo hello > app.py", **IMPL)) is None
    assert run_hook("PreToolUse", _tool(task, "Edit", {"file_path": str(task["root"] / "app.py"), "old_string": "a",
                                                        "new_string": "b"}, **IMPL)) is None
    _stop(task, "impl", "thaliris:implementer")
    for denied in (run_hook("PreToolUse", _write(task, **IMPL)),
                   run_hook("PreToolUse", _shell(task, "echo hello > app.py", **IMPL)),
                   run_hook("PreToolUse", _shell(task, "rm app.py", **IMPL))):
        assert _decision(denied) == "deny" and "fresh dispatch" in _reason(denied) and "returned its final report" in _reason(denied)
    assert run_hook("PreToolUse", _shell(task, "git status", **IMPL)) is None  # reads are not writes
    assert run_hook("PreToolUse", _write(task)) is None  # the Controller thread is never fenced by a child's state
    assert "updatedInput" in _dispatch(task, "tu2", role="implementer")["hookSpecificOutput"]  # new work: a new child
    _start(task, "impl2", "thaliris:implementer", "tu2")
    assert run_hook("PreToolUse", _write(task, agent_id="impl2", agent_type="thaliris:implementer")) is None


def test_background_launch_acknowledgement_does_not_fence_a_running_child(task):
    _dispatch(task, "tu1", role="implementer", run_in_background=True)
    _start(task, "impl", "thaliris:implementer", "tu1")
    _result(task, "tu1", {"isAsync": True, "status": "async_launched", "agentId": "impl"})
    assert _entry()["native_status"] == "launched"
    assert run_hook("PreToolUse", _write(task, **IMPL)) is None


def test_local_child_conflicts_do_not_widen_into_a_task_wide_refusal(task):
    """Dispatch preconditions stay local: a disposed, finished or conflicted child never blocks the next dispatch."""
    first = _handoff(_dispatch(task, "tu1", role="implementer"))
    run_cli("dispose", "--handoff-id", first, "--reason", "cancelled")
    _dispatch(task, "tu2", role="implementer")
    _start(task, "done", "thaliris:implementer", "tu2")
    _stop(task, "done", "thaliris:implementer")
    _dispatch(task, "tu3")
    _start(task, "clash", "thaliris:reviewer", "tu3")  # Host record names another type: a recorded identity conflict
    assert [item["binding"] for item in _dispatches()][-1] == "conflict"
    fresh = _dispatch(task, "tu4", role="implementer")
    assert "updatedInput" in fresh["hookSpecificOutput"] and "permissionDecision" not in fresh["hookSpecificOutput"]
    nested = run_hook("PreToolUse", _agent_call(task, "tu5", "thaliris:investigator", "scan", agent_id="ghost",
                                                agent_type="thaliris:implementer"))
    assert _decision(nested) == "deny"  # an unbound child's own request is refused, locally...
    assert "updatedInput" in _dispatch(task, "tu6")["hookSpecificOutput"]  # ...and the Controller's next one is not


# ------------------------------------------------- D: an unreadable lifecycle fails closed for children only

def _damage(task, how):
    path = _lifecycle_file(task)
    if how == "version":
        value = json.loads(path.read_text(encoding="utf-8"))
        value["version"] = 2
        path.write_text(json.dumps(value), encoding="utf-8")
        return "CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED"
    path.write_text("{truncated", encoding="utf-8")
    return "CLAUDE_LIFECYCLE_INVALID"


@pytest.mark.parametrize("how", ["version", "garbage"])
def test_unreadable_lifecycle_keeps_a_disposed_child_fenced(task, how):
    handoff = _running_implementer(task)
    assert run_cli("dispose", "--handoff-id", handoff, "--reason", "cancelled")[0] == 0
    code = _damage(task, how)

    for denied in (run_hook("PreToolUse", _write(task, **IMPL)),
                   run_hook("PreToolUse", _shell(task, "rm app.py", **IMPL)),
                   run_hook("PreToolUse", _tool(task, "SendMessage", {"to": "impl", "message": "go on"})),
                   run_hook("PreToolUse", _tool(task, "SendMessage", {"to": "impl", "message": "go on"}, **IMPL)),
                   _dispatch(task, "tu2")):
        assert _decision(denied) == "deny" and code in _reason(denied), denied
    assert "recovery" in _reason(run_hook("PreToolUse", _write(task, **IMPL))).casefold()

    # Controller work and generic subagents are untouched; a child's runner call reaches the runner, which names the real fault.
    assert run_hook("PreToolUse", _write(task)) is None
    assert run_hook("PreToolUse", _shell(task, "git status")) is None
    assert run_hook("PreToolUse", _write(task, agent_id="gen", agent_type="general-purpose")) is None
    runner = run_hook("PreToolUse", _shell(task, f"{RUNNER} status", **IMPL))
    assert runner["hookSpecificOutput"]["updatedInput"]["command"].endswith("--native-agent impl")
    code_, error = run_cli("status")
    assert code_ == 2 and error["code"] == code and "recovery" in error


def test_unreadable_lifecycle_does_not_stop_readonly_roles_or_the_controller(task):
    _running_implementer(task)
    _damage(task, "version")
    assert _decision(run_hook("PreToolUse", _write(task, agent_id="rev", agent_type="thaliris:reviewer"))) == "deny"  # still readonly
    assert run_hook("PreToolUse", _shell(task, "git diff", agent_id="rev", agent_type="thaliris:reviewer")) is None


def test_newer_lifecycle_dispatch_still_refused_with_its_code(task):
    path = _lifecycle_file(task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "task_id": task["task_id"], "dispatches": []}), encoding="utf-8")
    denied = _dispatch(task)
    assert _decision(denied) == "deny" and "CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED" in _reason(denied)


# ------------------------------------------------- E: a damaged entry is a coded error, not a crash

@pytest.mark.parametrize("damage", [lambda item: item.pop("disposed"), lambda item: item.update(native_status="gone"),
                                    lambda item: item.update(disposed="no")])
def test_damaged_lifecycle_entry_is_coded_and_session_start_survives(task, damage):
    _running_implementer(task)
    value = json.loads(_lifecycle_file(task).read_text(encoding="utf-8"))
    damage(value["dispatches"][0])
    _lifecycle_file(task).write_text(json.dumps(value), encoding="utf-8")

    for command in (("status",), ("status", "--view", "full")):
        code, error = run_cli(*command)
        assert code == 2 and error["code"] == "CLAUDE_LIFECYCLE_INVALID" and "recovery" in error, error
    context = run_hook("SessionStart", _base(task, "SessionStart", source="resume"))["hookSpecificOutput"]["additionalContext"]
    assert "CLAUDE_LIFECYCLE_INVALID" in context and task["task_id"] in context and "UNKNOWN" in context
    assert _decision(run_hook("PreToolUse", _write(task, **IMPL))) == "deny"  # the child's write fails closed meanwhile


# ------------------------------------------------- G: the 0.1 lifecycle shape keeps reading and dispatching

def _version_01_entry(entry):
    """The exact keys `reserve` wrote in 0.1: no `requested_effort`, no `role_binding`."""
    keys = ("tool_use_id", "role", "subagent_type", "parent", "handoff_id", "prompt_sha256", "requested_model", "background",
            "agent_id", "binding", "native_status", "disposed", "created_at", "events")
    return {key: entry[key] for key in keys}


def test_version_01_lifecycle_entries_read_and_dispatch(task):
    handoff = _handoff(_dispatch(task, "tu1", role="implementer"))
    value = json.loads(_lifecycle_file(task).read_text(encoding="utf-8"))
    value["dispatches"][0] = _version_01_entry(value["dispatches"][0])
    assert "role_binding" not in value["dispatches"][0] and "requested_effort" not in value["dispatches"][0]
    _lifecycle_file(task).write_text(json.dumps(value), encoding="utf-8")

    full = run_cli("status", "--view", "full")[1]["Claude Dispatches"][0]
    assert full["handoff_id"] == handoff and full["role_binding"] is None and full["requested_effort"] is None
    _start(task, "impl", "thaliris:implementer", "tu1")  # binds by the Host record and resolves the role from the registry
    assert run_hook("PreToolUse", _write(task, **IMPL)) is None
    assert run_cli("observe", "--text", "edited app.py", "--source", "app.py:1", agent="impl")[0] == 0
    assert _decision(run_hook("PreToolUse", _write(task, agent_id="rev", agent_type="thaliris:reviewer"))) == "deny"
    _stop(task, "impl", "thaliris:implementer")
    assert _decision(run_hook("PreToolUse", _write(task, **IMPL))) == "deny"
    assert "updatedInput" in _dispatch(task, "tu2", role="reviewer")["hookSpecificOutput"]  # new entries append in the new shape
    assert "role_binding" in store.read_lifecycle(task["root"], task["task_id"])["dispatches"][1]
    assert run_cli("dispose", "--handoff-id", handoff, "--reason", "x")[0] == 0  # old entries accept every adapter write


def test_message_to_finished_child_still_refused(task):
    """The original SendMessage refusal is unchanged by the unreadable-lifecycle handling."""
    _running_implementer(task)
    _stop(task, "impl", "thaliris:implementer")
    denied = run_hook("PreToolUse", _tool(task, "SendMessage", {"to": "impl", "message": "again"}))
    assert _decision(denied) == "deny" and "fresh child" in _reason(denied)
