"""End-to-end adapter flow with payload shapes observed on Claude Code 2.1.302."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from thaliris_claude import hooks, store

from .conftest import SESSION, run_cli, run_hook

RUNNER = hooks.runner_command()
OBSERVATION = {"id": "O1", "text": "greeting printed at app.py:1", "source_refs": ["S1"],
               "sources": [{"id": "S1", "kind": "local", "locator": "app.py:1", "summary": "print('hi')"}]}


def _base(task, event, **fields):
    return {"session_id": SESSION, "transcript_path": task["transcript"], "cwd": str(task["root"]), "hook_event_name": event, **fields}


def _agent_call(task, tool_use_id, subagent_type, prompt, *, agent_id=None, agent_type=None, **extra):
    payload = _base(task, "PreToolUse", tool_name="Agent", tool_use_id=tool_use_id,
                    tool_input={"description": "work", "prompt": prompt, "subagent_type": subagent_type, **extra})
    if agent_id:
        payload.update(agent_id=agent_id, agent_type=agent_type)
    return payload


def _tool(task, tool, tool_input, *, agent_id=None, agent_type=None):
    payload = _base(task, "PreToolUse", tool_name=tool, tool_use_id="x", tool_input=tool_input)
    if agent_id:
        payload.update(agent_id=agent_id, agent_type=agent_type)
    return payload


def _shell(task, command, **who):
    return _tool(task, "Bash", {"command": command, "description": "x"}, **who)


def _decision(result):
    return (result or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def _reason(result):
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def _host_record(task, agent_id, agent_type, tool_use_id):
    """What the Host writes beside its transcript (observed in the live probe)."""
    directory = Path(task["transcript"]).with_suffix("") / "subagents"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"agent-{agent_id}.meta.json").write_text(json.dumps(
        {"agentType": agent_type, "toolUseId": tool_use_id, "spawnDepth": 1, "model": "haiku", "finished": False}), encoding="utf-8")
    (directory / f"agent-{agent_id}.prefix.json").write_text(json.dumps(
        {"v": 1, "state": {"toolNames": ["Read", "Grep", "Glob", "Bash"], "model": "claude-haiku-5-5"}}), encoding="utf-8")


def _start(task, agent_id, agent_type, tool_use_id=None):
    if tool_use_id is not None:
        _host_record(task, agent_id, agent_type, tool_use_id)
    return run_hook("SubagentStart", _base(task, "SubagentStart", agent_id=agent_id, agent_type=agent_type))


def _stop(task, agent_id, agent_type):
    return run_hook("SubagentStop", _base(task, "SubagentStop", agent_id=agent_id, agent_type=agent_type,
                                          stop_hook_active=False, last_assistant_message="**Conclusion** done"))


def _result(task, tool_use_id, response, **who):
    return run_hook("PostToolUse", _base(task, "PostToolUse", tool_name="Agent", tool_use_id=tool_use_id, tool_input={},
                                         tool_response=response, **who))


def _dispatch(task, tool_use_id="tu1", role="investigator", marker=True, **extra):
    prompt = "Find where the greeting is printed."
    if marker:
        prompt += '\nthaliris-evidence: {"candidate":"C1","requirement_refs":["R1"]}'
    return run_hook("PreToolUse", _agent_call(task, tool_use_id, f"thaliris:{role}", prompt, **extra))


def _handoff(result):
    return result["hookSpecificOutput"]["updatedInput"]["prompt"].rsplit("thaliris-handoff: ", 1)[1]


def _dispatches():
    return run_cli("status", "--view", "full")[1]["Claude Dispatches"]


def test_session_start_names_the_loaded_adapter(task):
    result = run_hook("SessionStart", _base(task, "SessionStart", source="startup"))
    text = result["hookSpecificOutput"]["additionalContext"]
    assert RUNNER in text and task["task_id"] in text
    assert hooks.plugin_root().as_posix() in text  # H096: name what is actually loaded


def test_full_dispatch_observe_judge_close(task):
    result = _dispatch(task)
    handoff_id = _handoff(result)
    assert "permissionDecision" not in result["hookSpecificOutput"]  # normal permission flow is preserved
    started = _start(task, "agent-a", "thaliris:investigator", "tu1")
    assert handoff_id in started["hookSpecificOutput"]["additionalContext"]

    code, observed = run_cli("observe", "--json", json.dumps(OBSERVATION), agent="agent-a")
    assert code == 0, observed
    assert observed["observation_id"] == f"{handoff_id}.O1" and observed["candidate"] == "C1"
    assert run_cli("observe", "--handoff-id", handoff_id, "--json", json.dumps(OBSERVATION), agent="agent-a")[0] == 2

    revision = run_cli("status")[1]["Task"]["revision"]
    code, refused = run_cli("close", "--base-revision", str(revision))
    assert code == 2 and "CHILD_EXECUTION_UNRESOLVED" in refused["error"]

    _stop(task, "agent-a", "thaliris:investigator")
    delivered = _result(task, "tu1", {"status": "completed", "agentId": "agent-a", "resolvedModel": "claude-haiku-5-5"})
    context = delivered["hookSpecificOutput"]["additionalContext"]
    assert f"{handoff_id}.O1" in context and "requirement R1" in context

    dispatch = _dispatches()[0]
    assert dispatch["binding"] == "exact" and dispatch["observed_model"] == "claude-haiku-5-5"  # tool result upgrades host-meta
    assert dispatch["observed_tools"] == ["Read", "Grep", "Glob", "Bash"]
    status = run_cli("status")[1]
    judgment = {"id": "J1", "candidate": "C1", "requirement_ref": "R1", "observation_refs": [f"{handoff_id}.O1"],
                "outcome": "repair", "reasoning": "greeting still prints hi"}
    code, judged = run_cli("judge", "--base-revision", str(status["Task"]["revision"]), "--json", json.dumps(judgment))
    assert code == 0 and judged["evidence"]["unresolved_judgments_total"] == 1 and judged["appended"] == ["J1"]
    code, closed = run_cli("close", "--base-revision", str(judged["revision"]))
    assert code == 0 and closed["status"] == "DONE" and closed["evidence"]["unresolved_judgments_total"] == 1
    assert store.resolve_session(SESSION) is None


def test_same_role_children_are_never_bound_by_guessing(task):
    """H144: 'the only pending reservation of this role' is not identity."""
    _dispatch(task, "tu1")
    context = _start(task, "agent-x", "thaliris:investigator")["hookSpecificOutput"]["additionalContext"]
    assert "confirmed from the Host's own record" in context
    assert _dispatches()[0]["binding"] == "unbound"
    code, refused = run_cli("observe", "--json", json.dumps(OBSERVATION), agent="agent-x")
    assert code == 2 and "UNBOUND" in refused["error"]
    # The child's first runner call binds once the Host record exists.
    _host_record(task, "agent-x", "thaliris:investigator", "tu1")
    run_hook("PreToolUse", _shell(task, f"{RUNNER} observe --json '{{}}'", agent_id="agent-x", agent_type="thaliris:investigator"))
    assert _dispatches()[0]["binding"] == "host-meta" and _dispatches()[0]["native_status"] == "running"


def test_actual_tool_set_is_collected_after_the_child_stops(task):
    """Live probe: the Host writes prefix.json (actual tools) after SubagentStop."""
    _dispatch(task, "tu1")
    _start(task, "agent-a", "thaliris:investigator", "tu1")
    prefix = Path(task["transcript"]).with_suffix("") / "subagents" / "agent-agent-a.prefix.json"
    prefix.unlink()
    _stop(task, "agent-a", "thaliris:investigator")
    assert _dispatches()[0]["observed_tools"] is None
    prefix.write_text(json.dumps({"state": {"toolNames": ["Read", "Bash"], "model": "claude-haiku-5-5"}}), encoding="utf-8")
    run_hook("UserPromptSubmit", _base(task, "UserPromptSubmit", prompt="<task-notification>...</task-notification>"))
    assert _dispatches()[0]["observed_tools"] == ["Read", "Bash"]


def test_host_record_type_mismatch_is_a_conflict(task):
    _dispatch(task, "tu1")
    _start(task, "agent-x", "thaliris:reviewer", "tu1")
    assert _dispatches()[0]["binding"] == "conflict"
    assert run_cli("observe", "--json", json.dumps(OBSERVATION), agent="agent-x")[0] == 2


def test_exact_tool_result_binds_background_child(task):
    _dispatch(task, "tu1")
    _dispatch(task, "tu2")
    _start(task, "agent-x", "thaliris:investigator")
    _result(task, "tu2", {"isAsync": True, "status": "async_launched", "agentId": "agent-x", "resolvedModel": "claude-haiku-5-5"})
    dispatches = _dispatches()
    assert [item["binding"] for item in dispatches] == ["unbound", "exact"]
    assert dispatches[1]["background"] is True and dispatches[1]["native_status"] == "launched"


def test_finished_child_is_not_reused(task):
    """H092/H113/H145: an active child takes supplements; a finished one gets no new work or grants."""
    _dispatch(task, "tu1")
    _start(task, "agent-a", "thaliris:investigator", "tu1")
    message = _tool(task, "SendMessage", {"to": "agent-a", "message": "one more fact"})
    assert run_hook("PreToolUse", message) is None
    _stop(task, "agent-a", "thaliris:investigator")
    denied = run_hook("PreToolUse", message)
    assert _decision(denied) == "deny" and "fresh child" in _reason(denied)
    code, refused = run_cli("observe", "--json", json.dumps(OBSERVATION), agent="agent-a")
    assert code == 2 and "DISPATCH_FINISHED" in refused["error"]


def test_children_cannot_load_the_controller_skill(task):
    """H143/H178: children keep their role; the Controller routine stays in the main thread."""
    child = _tool(task, "Skill", {"skill": "thaliris:thaliris"}, agent_id="impl", agent_type="thaliris:implementer")
    assert _decision(run_hook("PreToolUse", child)) == "deny"
    assert run_hook("PreToolUse", _tool(task, "Skill", {"skill": "thaliris:thaliris"})) is None
    assert run_hook("PreToolUse", _tool(task, "Skill", {"skill": "pdf"}, agent_id="impl", agent_type="thaliris:implementer")) is None


def test_children_cannot_use_controller_commands_or_forge_identity(task):
    _dispatch(task)
    _start(task, "agent-a", "thaliris:investigator", "tu1")
    child = {"agent_id": "agent-a", "agent_type": "thaliris:investigator"}
    denied = run_hook("PreToolUse", _shell(task, f"{RUNNER} close --base-revision 3", **child))
    assert _decision(denied) == "deny" and "owning Controller" in _reason(denied)
    assert _decision(run_hook("PreToolUse", _shell(task, f"{RUNNER} status --native-session other"))) == "deny"
    allowed = run_hook("PreToolUse", _shell(task, f"{RUNNER} status", **child))
    assert allowed["hookSpecificOutput"]["updatedInput"]["command"].endswith(f"--native-session {SESSION} --native-agent agent-a")
    assert _decision(run_hook("PreToolUse", _shell(task, "thaliris-core --task-id x task-close --base-revision 1", **child))) == "deny"
    protected = run_hook("PreToolUse", _shell(task, "echo {} > .context/config.json", agent_id="agent-a", agent_type="thaliris:implementer"))
    assert _decision(protected) == "deny"


def test_runner_rejects_abbreviated_or_repeated_identity(task):
    """H023: argparse abbreviations and last-value-wins must not move identity."""
    assert "DUPLICATE_IDENTITY_FLAG" in run_cli("status", "--native-session", "other")[1]["error"]
    code, refused = run_cli("status", "--native-s", "other", session=None)
    assert code == 2 and "unrecognized" in refused["error"]


def test_reviewer_is_readonly_everywhere(task):
    write = _tool(task, "Edit", {"file_path": str(task["root"] / "app.py"), "old_string": "hi", "new_string": "hello"},
                  agent_id="rev", agent_type="thaliris:reviewer")
    assert _decision(run_hook("PreToolUse", write)) == "deny"
    assert _decision(run_hook("PreToolUse", _shell(task, "git commit -am fix", agent_id="rev", agent_type="thaliris:reviewer"))) == "deny"
    assert run_hook("PreToolUse", _shell(task, "git diff", agent_id="rev", agent_type="thaliris:reviewer")) is None
    write.update(agent_type="thaliris:implementer")
    assert run_hook("PreToolUse", write) is None


def test_delegation_matrix_depth_and_single_scanner(task):
    _dispatch(task, "tu-impl", role="implementer")
    _start(task, "impl", "thaliris:implementer", "tu-impl")
    impl = {"agent_id": "impl", "agent_type": "thaliris:implementer"}
    assert _decision(run_hook("PreToolUse", _agent_call(task, "x1", "thaliris:reviewer", "p", **impl))) == "deny"
    assert _decision(run_hook("PreToolUse", _agent_call(task, "x2", "general-purpose", "p", **impl))) == "deny"
    marked = 'scan\nthaliris-evidence: {"candidate":"C1","requirement_refs":["R1"]}'
    assert _decision(run_hook("PreToolUse", _agent_call(task, "x3", "thaliris:investigator", marked, **impl))) == "deny"
    scanner = run_hook("PreToolUse", _agent_call(task, "x4", "thaliris:investigator", "scan call sites", **impl))
    nested_handoff = _handoff(scanner)
    second = run_hook("PreToolUse", _agent_call(task, "x5", "thaliris:investigator", "more", **impl))
    assert _decision(second) == "deny" and "already have a Scanner" in _reason(second)
    record = run_cli("get", nested_handoff)[1]["object"]
    assert record["requirement_refs"] == [] and record["candidate"] == "C1" and record["parent_handoff_id"]  # H133
    ghost = {"agent_id": "ghost", "agent_type": "thaliris:implementer"}
    assert _decision(run_hook("PreToolUse", _agent_call(task, "x6", "thaliris:investigator", "p", **ghost))) == "deny"


def test_evidence_reaches_notification_turns_without_repeating(task):
    """H132: present unresolved evidence before the next decision; without becoming per-turn noise."""
    _dispatch(task, "tu1")
    _result(task, "tu1", {"isAsync": True, "status": "async_launched", "agentId": "agent-a"})
    turn = _base(task, "UserPromptSubmit", prompt="<task-notification>\n<task-id>agent-a</task-id>\n</task-notification>")
    assert run_hook("UserPromptSubmit", turn) is None
    _host_record(task, "agent-a", "thaliris:investigator", "tu1")
    run_cli("observe", "--json", json.dumps(OBSERVATION), agent="agent-a")
    context = run_hook("UserPromptSubmit", turn)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Thaliris evidence for task") and ".O1 → requirement R1" in context
    assert run_hook("UserPromptSubmit", turn) is None  # unchanged: not repeated
    run_hook("SessionStart", _base(task, "SessionStart", source="compact"))
    assert run_hook("UserPromptSubmit", turn) is not None  # context was lost: shown again


def test_single_agent_mode_and_model_constraint(task, tmp_path):
    status = run_cli("status")[1]
    code, switched = run_cli("mode", "--mode", "single-agent", "--human-instruction", "do it yourself",
                             "--base-revision", str(status["Task"]["revision"]), "--expected-authority-sha256", status["Authority"]["sha256"])
    assert code == 0, switched
    assert _decision(_dispatch(task)) == "deny"
    run_cli("detach", "--reason", "new task")

    contract = json.loads(task["contract"].read_text(encoding="utf-8")) | {"execution_constraint": "claude-model:sonnet"}
    path = tmp_path / "constrained.json"
    path.write_text(json.dumps(contract), encoding="utf-8")
    code, started = run_cli("task-start", "Second task", "--contract", str(path))
    assert code == 0, started
    run_cli("register", "--base-revision", str(started["revision"]), "--json", '{"requirements":[{"id":"R1","workchain_ref":"w"}]}')
    assert _dispatch(task, "c1")["hookSpecificOutput"]["updatedInput"]["model"] == "sonnet"
    assert _decision(_dispatch(task, "c2", model="opus")) == "deny"
    run_cli("detach", "--reason", "x")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(contract | {"execution_constraint": "luna-only"}), encoding="utf-8")
    assert run_cli("task-start", "Third", "--contract", str(bad))[1]["error"] == "UNSUPPORTED_EXECUTION_CONSTRAINT"
    # H059: the contract is a file path, and inline JSON says so clearly.
    assert "CONTRACT_FILE_PATH_REQUIRED" in run_cli("task-start", "Fourth", "--contract", json.dumps(contract))[1]["error"]


def test_nested_repository_root_is_never_guessed(home, tmp_path, monkeypatch):
    """H138/H139: a parent workspace holding repositories is not itself the managed project."""
    workspace = tmp_path / "workspace"
    (workspace / "codex" / "Thaliris-codex").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(workspace / "codex" / "Thaliris-codex")], check=True)
    monkeypatch.chdir(workspace)
    code, refused = run_cli("bootstrap")
    assert code == 2 and "AMBIGUOUS_PROJECT_ROOT" in refused["error"] and "codex/Thaliris-codex" in refused["error"]
    assert not (workspace / ".context").exists()
    code, booted = run_cli("bootstrap", "--root", str(workspace / "codex" / "Thaliris-codex"))
    assert code == 0 and booted["initialized"]


def test_user_settings_edit_does_not_trip_authority(task):
    """H094/H098: user-owned Claude settings are not anchored, so recovery never reverts them."""
    settings = task["root"] / ".claude" / "settings.json"
    settings.parent.mkdir(exist_ok=True)  # task-start pre-creates .claude/agents/
    settings.write_text('{"permissions": {"allow": ["Bash(git status)"]}}', encoding="utf-8")
    assert "updatedInput" in _dispatch(task)["hookSpecificOutput"]


def test_dispose_fences_child_and_releases_close(task):
    handoff_id = _handoff(_dispatch(task, role="implementer"))
    _start(task, "impl", "thaliris:implementer", "tu1")
    code, disposed = run_cli("dispose", "--handoff-id", handoff_id, "--reason", "user cancelled")
    assert code == 0 and disposed["native_termination"] == "UNKNOWN"
    write = _tool(task, "Write", {"file_path": str(task["root"] / "app.py"), "content": "x"}, agent_id="impl", agent_type="thaliris:implementer")
    assert _decision(run_hook("PreToolUse", write)) == "deny"
    assert _decision(run_hook("PreToolUse", _tool(task, "SendMessage", {"to": "impl", "message": "go on"}))) == "deny"
    assert run_cli("observe", "--json", json.dumps(OBSERVATION), agent="impl")[0] == 2
    revision = run_cli("status")[1]["Task"]["revision"]
    assert run_cli("close", "--base-revision", str(revision))[0] == 0


def test_authority_conflict_blocks_dispatch_and_recovers(task):
    state = task["root"] / ".context" / "tasks" / task["task_id"] / "state.json"
    original = state.read_bytes()
    state.write_bytes(original.replace(b'"revision": ', b'"revision":  '))
    denied = _dispatch(task)
    assert _decision(denied) == "deny" and "TASK_AUTHORITY_STATE_CHANGED" in _reason(denied)
    # H088: damaged authority restricts managed controls only; ordinary work continues.
    assert run_hook("PreToolUse", _shell(task, "git status")) is None
    sha = run_cli("status")[1]["Authority"]["sha256"]
    code, recovered = run_cli("recover-authority", "--expected-authority-sha256", sha, "--reason", "manual edit")
    assert code == 0 and recovered["status"] == "TASK_AUTHORITY_RECOVERED"
    assert state.read_bytes() == original
    assert "updatedInput" in _dispatch(task)["hookSpecificOutput"]


def test_controller_cannot_hand_edit_ledger(task):
    ledger = _tool(task, "Write", {"file_path": str(task["root"] / ".context" / "tasks" / task["task_id"] / "state.json"), "content": "{}"})
    assert _decision(run_hook("PreToolUse", ledger)) == "deny"
    assert run_hook("PreToolUse", _tool(task, "Write", {"file_path": str(task["root"] / "app.py"), "content": "x"})) is None
    assert run_hook("PreToolUse", _shell(task, "thaliris-core --task-id x task-status")) is None  # H089: diagnostics stay open


def test_no_task_is_transparent(home, repo):
    """H019/H087: no managed task means no reservation, and ordinary work is untouched."""
    task = {"root": repo, "transcript": str(repo / "t.jsonl")}
    assert run_hook("PreToolUse", _agent_call(task, "t", "thaliris:investigator", "p")) is None
    assert run_hook("PreToolUse", _agent_call(task, "t", "general-purpose", "p")) is None
    assert run_hook("PostToolUse", {"session_id": SESSION, "tool_name": "Agent", "tool_use_id": "t", "tool_response": {}}) is None
    assert "NO_ACTIVE_TASK_FOR_SESSION" in run_cli("status")[1]["error"]
    assert "NATIVE_IDENTITY_UNKNOWN" in run_cli("status", session=None)[1]["error"]


def _launch(task, payload, **env):
    import os
    return subprocess.run([sys.executable, str(hooks.plugin_root() / "bin" / "thaliris.py"), "hook", "PreToolUse"],
                          input=json.dumps(payload).encode(), capture_output=True, check=True, env={**os.environ, **env})


def test_launcher_runs_hooks_from_an_old_python(task):
    output = json.loads(_launch(task, _shell(task, f"{RUNNER} status")).stdout)
    assert output["hookSpecificOutput"]["updatedInput"]["command"].endswith(f"--native-session {SESSION}")


def test_broken_runtime_refuses_only_unguardable_actions(task, tmp_path):
    """H055/H057/H097: a broken runtime must not lock ordinary work, nor silently admit managed work."""
    broken = tmp_path / "broken-python.cmd" if sys.platform == "win32" else tmp_path / "broken-python"
    broken.write_text("@exit /b 1\n" if sys.platform == "win32" else "#!/bin/sh\nexit 1\n", encoding="utf-8")
    broken.chmod(0o755)
    env = {"THALIRIS_CLAUDE_PYTHON": str(broken)}
    dispatch = json.loads(_launch(task, _agent_call(task, "t", "thaliris:investigator", "p"), **env).stdout)
    assert dispatch["hookSpecificOutput"]["permissionDecision"] == "deny"
    review = _tool(task, "Write", {"file_path": "a", "content": "x"}, agent_id="r", agent_type="thaliris:reviewer")
    assert json.loads(_launch(task, review, **env).stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert _launch(task, _shell(task, "git status"), **env).stdout == b""
    assert _launch(task, _agent_call(task, "t", "general-purpose", "p"), **env).stdout == b""


def test_models_are_the_controllers_choice(task, monkeypatch):
    """Design: the Controller chooses each role's model; the adapter never overrides or refuses it."""
    monkeypatch.delenv(hooks.MODEL_SETTING, raising=False)
    for index, model in enumerate(("opus", "sonnet", "haiku")):
        result = _dispatch(task, f"m{index}", role="focused-implementer", model=model)
        assert result["hookSpecificOutput"]["updatedInput"]["model"] == model
    assert "model" not in _dispatch(task, "m9", role="investigator")["hookSpecificOutput"]["updatedInput"]


def test_controller_registers_a_managed_role(task):
    spec = {"name": "security-auditor", "description": "Audits auth boundaries", "model": "opus", "writes": False,
            "scanner": True, "instructions": "Find authorization bypasses and report each with evidence."}
    code, registered = run_cli("register-role", "--json", json.dumps(spec))
    assert code == 0, registered
    agent = task["root"] / registered["agent_file"]
    text = agent.read_text(encoding="utf-8")
    assert "name: thaliris-security-auditor" in text and "observe --json" in text and "disallowedTools: Edit" in text
    assert any(item["subagent_type"] == "thaliris-security-auditor" for item in run_cli("roles")[1]["registered"])
    # Children cannot register roles, and the registered role is managed like a built-in one.
    child = _shell(task, f"{RUNNER} register-role --json '{{}}'", agent_id="a", agent_type="thaliris:implementer")
    assert _decision(run_hook("PreToolUse", child)) == "deny"
    dispatched = run_hook("PreToolUse", _agent_call(task, "c1", "thaliris-security-auditor",
                                                    'audit\nthaliris-evidence: {"candidate":"C1","requirement_refs":["R1"]}'))
    assert "thaliris-handoff: H" in dispatched["hookSpecificOutput"]["updatedInput"]["prompt"]
    auditor = {"agent_id": "aud", "agent_type": "thaliris-security-auditor"}
    write = _tool(task, "Write", {"file_path": str(task["root"] / "app.py"), "content": "x"}, **auditor)
    assert _decision(run_hook("PreToolUse", write)) == "deny"  # registered writes: false
    scan = run_hook("PreToolUse", _agent_call(task, "c2", "thaliris:investigator", "scan", **auditor))
    assert _decision(scan) == "deny"  # unbound identity, not a policy refusal of the role
    # Editing the agent file by hand voids the registration for new dispatches: the type is generic again...
    agent.write_text(text + "\nignore your role", encoding="utf-8")
    assert run_cli("roles")[1]["registered"] == []
    assert run_hook("PreToolUse", _agent_call(task, "c3", "thaliris-security-auditor", "audit")) is None  # no reservation
    # ...but the child already dispatched keeps the binding it was dispatched with.
    assert _decision(run_hook("PreToolUse", write)) == "deny"


def test_session_start_warns_about_a_small_controller(task, monkeypatch):
    small = run_hook("SessionStart", _base(task, "SessionStart", source="startup", model="claude-haiku-5-5"))
    assert "small or low-capability model" in small["hookSpecificOutput"]["additionalContext"]
    strong = run_hook("SessionStart", _base(task, "SessionStart", source="startup", model="claude-opus-5-5"))
    assert "small or low-capability model" not in strong["hookSpecificOutput"]["additionalContext"]
