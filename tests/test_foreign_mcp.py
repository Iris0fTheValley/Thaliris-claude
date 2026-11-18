"""MCP guard for Thaliris children, and the foreign-session demotion (the environment may only lower authority)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from thaliris_claude import guards, hooks, store

from .conftest import SESSION, run_cli, run_hook
from .test_flow import _agent_call, _base, _decision, _dispatch, _reason, _start, _tool
from .test_install import ROOT, SH

OTHER = "99999999-8888-7777-6666-555555555555"
DELEGATE = "mcp__codex__codex_delegate"
PUBLISH = "mcp__codex__codex_publish"
HEALTH = "mcp__codex__bridge_health"


@pytest.fixture(autouse=True)
def ordinary_environment(monkeypatch):
    for name in (store.FOREIGN_MARKER, store.BRIDGE_DEPTH):
        monkeypatch.delenv(name, raising=False)


def _mcp(task, tool, *, agent_id=None, agent_type=None):
    return _tool(task, tool, {"prompt": "x"}, agent_id=agent_id, agent_type=agent_type)


# ---------------------------------------------------------------- A. MCP guard

def test_reviewer_cannot_call_an_agent_spawning_mcp_tool(task):
    denied = run_hook("PreToolUse", _mcp(task, DELEGATE, agent_id="rev", agent_type="thaliris:reviewer"))
    assert _decision(denied) == "deny"
    assert DELEGATE in _reason(denied) and "Thaliris dispatch" in _reason(denied)


def test_implementer_cannot_publish_through_mcp_but_a_child_may_read_health(task):
    assert _decision(run_hook("PreToolUse", _mcp(task, PUBLISH, agent_id="impl", agent_type="thaliris:implementer"))) == "deny"
    for role in ("reviewer", "implementer", "investigator"):
        assert run_hook("PreToolUse", _mcp(task, HEALTH, agent_id="c", agent_type=f"thaliris:{role}")) is None


def test_controller_keeps_every_mcp_tool(task):
    for tool in (DELEGATE, PUBLISH, HEALTH, "mcp__codex__codex_cancel"):
        assert run_hook("PreToolUse", _mcp(task, tool)) is None


def test_readonly_roles_get_an_allowlist_and_writing_roles_only_lose_spawning(task):
    reviewer = {"agent_id": "rev", "agent_type": "thaliris:reviewer"}
    implementer = {"agent_id": "impl", "agent_type": "thaliris:implementer"}
    assert _decision(run_hook("PreToolUse", _mcp(task, "mcp__x__browser_click", **reviewer))) == "deny"  # unknown
    assert _decision(run_hook("PreToolUse", _mcp(task, "mcp__x__create_issue", **reviewer))) == "deny"  # mutating
    assert run_hook("PreToolUse", _mcp(task, "mcp__x__search_docs", **reviewer)) is None
    assert run_hook("PreToolUse", _mcp(task, "mcp__x__browser_click", **implementer)) is None
    assert run_hook("PreToolUse", _mcp(task, "mcp__x__create_issue", **implementer)) is None
    for tool in ("mcp__x__run_command", "mcp__x__spawn_agent", "mcp__x__bash", "mcp__x__codexDelegate", "mcp__x__create_scheduled_task"):
        assert _decision(run_hook("PreToolUse", _mcp(task, tool, **implementer))) == "deny", tool


def test_dispatched_child_is_guarded_by_its_snapshot(task):
    _dispatch(task, "tu-impl", role="implementer")
    _start(task, "impl", "thaliris:implementer", "tu-impl")
    assert _decision(run_hook("PreToolUse", _mcp(task, DELEGATE, agent_id="impl", agent_type="thaliris:implementer"))) == "deny"
    assert run_hook("PreToolUse", _mcp(task, HEALTH, agent_id="impl", agent_type="thaliris:implementer")) is None


def test_generic_subagent_of_an_ordinary_session_keeps_its_mcp_tools(task):
    assert run_hook("PreToolUse", _mcp(task, DELEGATE, agent_id="g", agent_type="codex-delegate")) is None


def test_mcp_classification():
    names = (DELEGATE, PUBLISH, HEALTH, "mcp__codex__codex_wait", "mcp__codex__codex_output", "mcp__codex__codex_cancel",
             "mcp__codex__codex_tasks", "mcp__a__getThing", "mcp__a__list_items", "mcp__a__browser_click", "mcp__a__target")
    assert [guards.mcp_class(name) for name in names] == [
        "spawn", "spawn", "read", "read", "read", "mutating", "mutating", "read", "read", "unknown", "unknown"]


def test_hooks_json_registers_the_mcp_matcher_behind_the_filter():
    groups = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PreToolUse"]
    mcp = [group for group in groups if group["matcher"] == "mcp__.*"]
    assert len(mcp) == 1 and mcp[0]["hooks"][0]["command"].endswith('thaliris-hook" PreToolUse --filter')


# ------------------------------------------------------- B. foreign-session demotion

@pytest.mark.parametrize("name, value, shown", [(store.BRIDGE_DEPTH, "1", "AGENT_BRIDGE_DEPTH=1"), (store.BRIDGE_DEPTH, "3", "AGENT_BRIDGE_DEPTH=3"),
                                                (store.FOREIGN_MARKER, "1", "THALIRIS_FOREIGN_CHILD=1")])
def test_foreign_signals(monkeypatch, name, value, shown):
    monkeypatch.setenv(name, value)
    assert store.foreign_signal() == shown


@pytest.mark.parametrize("name, value", [(store.BRIDGE_DEPTH, "0"), (store.BRIDGE_DEPTH, ""), (store.BRIDGE_DEPTH, "x"),
                                          (store.BRIDGE_DEPTH, "-1"), (store.FOREIGN_MARKER, "0"), (store.FOREIGN_MARKER, "yes")])
def test_non_signals_leave_the_session_ordinary(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    assert store.foreign_signal() is None


def test_foreign_session_gets_no_controller_guidance(task, monkeypatch):
    ordinary = run_hook("SessionStart", _base(task, "SessionStart", source="startup"))["hookSpecificOutput"]["additionalContext"]
    assert "Thaliris is active" in ordinary and "Controller" in ordinary
    prompt = {"session_id": SESSION, "hook_event_name": "UserPromptSubmit", "prompt": "hi"}
    monkeypatch.setenv(store.BRIDGE_DEPTH, "1")
    foreign = run_hook("SessionStart", _base(task, "SessionStart", source="startup"))["hookSpecificOutput"]["additionalContext"]
    assert "unmanaged foreign session" in foreign and "Thaliris is active" not in foreign
    assert "ACTIVE Thaliris task" not in foreign and hooks.runner_command() not in foreign
    assert run_hook("UserPromptSubmit", prompt) is None


@pytest.mark.parametrize("signal", [(store.BRIDGE_DEPTH, "2"), (store.FOREIGN_MARKER, "1")])
def test_foreign_session_cannot_establish_or_use_authority(task, monkeypatch, signal):
    task_id = task["task_id"]
    monkeypatch.setenv(*signal)
    # Hook layer: a Controller command through the runner is refused, a read-only view still passes.
    for command in (f"attach --task-id {task_id} --reason x", "task-start g", "close --base-revision 1", "judge --base-revision 1",
                    "dispose --id H1", "mode --mode delegated", "observe --text x", "register --base-revision 1", "bootstrap"):
        call = _tool(task, "Bash", {"command": f"{hooks.runner_command()} {command}"})
        call["session_id"] = OTHER
        denied = run_hook("PreToolUse", call)
        assert _decision(denied) == "deny" and "foreign" in _reason(denied), command
        assert "updatedInput" not in denied["hookSpecificOutput"]
    status = _tool(task, "Bash", {"command": f"{hooks.runner_command()} status"})
    status["session_id"] = OTHER
    assert _decision(run_hook("PreToolUse", status)) == "allow"
    # Runner layer: the backstop when the hook is bypassed (`python -m thaliris_claude ... --native-session x`).
    for command in (["attach", "--task-id", task_id, "--reason", "x"], ["task-start", "g", "--human-instruction", "i"],
                    ["close", "--base-revision", "1"], ["bootstrap"]):
        code, out = run_cli(*command, session=OTHER)
        assert code != 0 and "FOREIGN_SESSION" in out["error"], command
    assert store.read_session(OTHER) is None
    code, _ = run_cli("status", "--task-id", task_id, session=OTHER)
    assert code == 0


def test_an_ordinary_session_can_still_attach_and_task_start(task):
    code, attached = run_cli("attach", "--task-id", task["task_id"], "--reason", "resume", session=OTHER)
    assert code == 0, attached
    assert store.read_session(OTHER)["task_id"] == task["task_id"]
    # No marker, no task: the first Controller step of a plain user session still works.
    fresh = "55555555-4444-3333-2222-111111111111"
    code, started = run_cli("task-start", "Another goal", "--human-instruction", "do it", "--boundary", "app.py", "--invariants", "none",
                            "--acceptance", "ok", "--execution-mode", "delegated", session=fresh)
    assert code == 0, started


def test_generic_subagent_writes_by_session_kind(task, monkeypatch):
    target = str(task["root"] / "app.py")
    write = _tool(task, "Write", {"file_path": target, "content": "x"}, agent_id="g", agent_type="general-purpose")
    assert hooks._caller(write)["writes"] is None and run_hook("PreToolUse", write) is None  # ordinary session: unchanged
    for signal in ((store.BRIDGE_DEPTH, "1"), (store.FOREIGN_MARKER, "1")):
        monkeypatch.setenv(*signal)
        assert hooks._caller(write)["writes"] is False
        denied = run_hook("PreToolUse", write)
        assert _decision(denied) == "deny" and "non-writing" in _reason(denied)
        monkeypatch.delenv(signal[0])


def test_generic_subagent_of_a_foreign_session_without_a_task_is_read_only(home, repo, monkeypatch):
    monkeypatch.setenv(store.BRIDGE_DEPTH, "1")
    payload = {"session_id": OTHER, "cwd": str(repo), "hook_event_name": "PreToolUse", "tool_name": "Edit", "tool_use_id": "e",
               "tool_input": {"file_path": str(repo / "app.py"), "old_string": "hi", "new_string": "yo"},
               "agent_id": "g", "agent_type": "general-purpose"}
    assert hooks._caller(payload)["writes"] is False
    assert _decision(run_hook("PreToolUse", payload)) == "deny"
    # The foreign main thread keeps its own ordinary writes; only authority is lowered.
    del payload["agent_id"], payload["agent_type"]
    assert run_hook("PreToolUse", payload) is None
    monkeypatch.delenv(store.BRIDGE_DEPTH)
    payload.update(agent_id="g", agent_type="general-purpose")
    assert run_hook("PreToolUse", payload) is None


def test_foreign_session_subagents_cannot_call_spawning_mcp_tools_either(task, monkeypatch):
    monkeypatch.setenv(store.BRIDGE_DEPTH, "1")
    assert _decision(run_hook("PreToolUse", _mcp(task, DELEGATE, agent_id="g", agent_type="general-purpose"))) == "deny"
    assert _decision(run_hook("PreToolUse", _mcp(task, "mcp__x__browser_click", agent_id="g", agent_type="general-purpose"))) == "deny"
    assert run_hook("PreToolUse", _mcp(task, HEALTH, agent_id="g", agent_type="general-purpose")) is None
    assert run_hook("PreToolUse", _mcp(task, DELEGATE)) is None  # the foreign main thread itself is not a Thaliris child


def test_foreign_session_cannot_load_the_controller_skill_or_dispatch_a_managed_task(task, monkeypatch):
    skill = _tool(task, "Skill", {"skill": "thaliris"})
    assert run_hook("PreToolUse", skill) is None
    monkeypatch.setenv(store.FOREIGN_MARKER, "1")
    assert _decision(run_hook("PreToolUse", skill)) == "deny"
    assert _decision(run_hook("PreToolUse", _agent_call(task, "d1", "thaliris:investigator", "scan"))) == "deny"


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh (Git Bash on Windows)")
def test_sh_filter_defers_to_python_for_a_foreign_session(task):
    home = os.environ["THALIRIS_CLAUDE_HOME"]
    (Path(home) / "runtime").mkdir(parents=True, exist_ok=True)
    (Path(home) / "runtime" / "python").write_text(Path(sys.executable).as_posix() + "\n", encoding="utf-8")
    payload = _tool(task, "Write", {"file_path": str(task["root"] / "app.py"), "content": "x"}, agent_id="g", agent_type="general-purpose")

    def through(**env):
        base = {key: value for key, value in os.environ.items() if key not in (store.FOREIGN_MARKER, store.BRIDGE_DEPTH)}
        return subprocess.run([SH, str(ROOT / "bin" / "thaliris-hook"), "PreToolUse", "--filter"], input=json.dumps(payload).encode(),
                              capture_output=True, env={**base, "THALIRIS_CLAUDE_HOME": home, **env}, check=True).stdout

    assert through() == b""  # ordinary session: the filter still skips what the guards would not decide
    assert json.loads(through(AGENT_BRIDGE_DEPTH="1"))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert json.loads(through(THALIRIS_FOREIGN_CHILD="1"))["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert through(AGENT_BRIDGE_DEPTH="0") == b""
