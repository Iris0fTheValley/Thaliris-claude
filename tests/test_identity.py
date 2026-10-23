"""Project identity plane: runner commands, Controller-only mutation, child-write protection, delivery."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from thaliris_claude import guards, hooks, store

from .conftest import SESSION, run_cli, run_hook

OTHER_SESSION = "99999999-8888-7777-6666-555555555555"
BASELINE = """# Example project identity

## INV-1 Output stays ASCII
The greeting program never prints non-ASCII bytes.

## P-1 Small surface
Prefer one file over a framework.
"""
BASELINE_2 = BASELINE.replace("never prints non-ASCII bytes", "prints ASCII unless the user asks for a locale")


def _write(tmp_path: Path, name: str, value: dict) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _revision() -> int:
    return run_cli("status")[1]["Task"]["revision"]


def _amend(task, tmp_path, text: str, **extra):
    status = run_cli("identity-status")[1]
    payload = {"base_version": status["version"] or 0, "base_sha256": status["on_disk_sha256"], "text": text,
               "summary": "record the identity", "rationale": "the owner decided it",
               "decision": {"decided_by": "owner", "human_instruction": "Yes, record this as the project identity."}, **extra}
    return run_cli("identity-amend", "--base-revision", str(_revision()), "--input", _write(tmp_path, "amend.json", payload))


def _hook_base(task, event, **fields):
    return {"session_id": SESSION, "transcript_path": task["transcript"], "cwd": str(task["root"]), "hook_event_name": event, **fields}


def _runner(task, subcommand, **who):
    payload = _hook_base(task, "PreToolUse", tool_name="Bash", tool_use_id="i1",
                         tool_input={"command": f"{hooks.runner_command()} {subcommand}"}, **who)
    return run_hook("PreToolUse", payload)


def _context(task, source="startup", session=SESSION, cwd=None):
    payload = {"session_id": session, "transcript_path": task["transcript"], "cwd": str(cwd or task["root"]),
               "hook_event_name": "SessionStart", "source": source}
    return run_hook("SessionStart", payload)["hookSpecificOutput"]["additionalContext"]


def test_identity_commands_end_to_end(task, tmp_path):
    code, absent = run_cli("identity-status")
    assert code == 0 and absent["state"] == "ABSENT" and absent["summary"] is None
    before = _revision()

    code, v1 = _amend(task, tmp_path, BASELINE)
    assert code == 0, v1
    assert v1["version"] == 1 and v1["amendment"] == "A-0001" and sorted(v1["items"]["added"]) == ["INV-1", "P-1"]
    assert (task["root"] / ".project-identity" / "IDENTITY.md").is_file()
    assert _revision() == before  # an amendment never bumps the task revision

    code, status = run_cli("identity-status")
    assert code == 0 and status["state"] == "CURRENT" and status["version"] == 1
    assert [item["id"] for item in status["items"]] == ["INV-1", "P-1"]
    code, whole = run_cli("identity-get")
    assert code == 0 and whole["text"] == BASELINE
    code, items = run_cli("identity-get", "--item", "INV-1")
    assert code == 0 and [item["id"] for item in items["items"]] == ["INV-1"] and "P-1" not in items["items"][0]["text"]
    code, record = run_cli("identity-get", "--amendment", "A-0001")
    assert code == 0 and record["kind"] == "amendment"

    code, challenged = run_cli("identity-challenge", "--base-revision", str(_revision()), "--json", json.dumps(
        {"targets": ["identity:INV-1"], "summary": "ASCII only blocks localisation", "text": "Users asked for a locale greeting."}))
    assert code == 0 and challenged["status"] == "open", challenged
    challenge_id = challenged["challenge"]
    assert run_cli("identity-status")[1]["open_challenges_total"] == 1

    code, related = run_cli("identity-relate", "--base-revision", str(_revision()), "--json", json.dumps(
        {"add": [{"from": "path:app.py", "rel": "serves", "to": "identity:INV-1", "note": "greeting"}]}))
    assert code == 0 and len(related["added"]) == 1, related
    code, traced = run_cli("identity-trace", "identity:INV-1", "--direction", "down", "--depth", "2")
    assert code == 0 and traced["edges"] and traced["edges"][0]["rel"] == "serves" and traced["notice"]

    code, v2 = _amend(task, tmp_path, BASELINE_2, resolves=[challenge_id], why_not_lower="the principle itself was wrong",
                      affected_refs=["path:app.py"])
    assert code == 0, v2
    assert v2["version"] == 2 and v2["challenges_resolved"] and v2["registered_dependents_total"] >= 1
    assert run_cli("identity-get", "--challenge", challenge_id)[1]["status"] == "amended"
    assert run_cli("identity-get", "--version", "1")[1]["text"] == BASELINE

    # The authority anchor stays intact: no TASK_AUTHORITY_SECURITY_CHANGED, and the Controller keeps working.
    code, updated = run_cli("task-update", "--base-revision", str(_revision()), "--json", json.dumps({"active_work": ["after amend"]}))
    assert code == 0, updated
    assert run_cli("status")[0] == 0
    assert all(".project-identity" not in path for path in store.PROTECTED_PATHS)


def test_amend_requires_the_controller_and_an_active_task(task, tmp_path, home):
    payload = _write(tmp_path, "x.json", {"text": BASELINE})
    code, refused = run_cli("identity-amend", "--base-revision", "1", "--input", payload, agent="agent-x")
    assert code == 2 and "CONTROLLER_ONLY" in refused["error"]
    code, refused = run_cli("identity-challenge", "--base-revision", "1", "--input", payload, session=OTHER_SESSION)
    assert code == 2 and "NO_ACTIVE_TASK_FOR_SESSION" in refused["error"]
    code, refused = run_cli("identity-relate", "--base-revision", "1", "--input", payload, session=None)
    assert code == 2 and "NATIVE_IDENTITY_UNKNOWN" in refused["error"]
    assert not (task["root"] / ".project-identity").exists()


def test_revision_conflict_and_missing_decision_are_refused_by_core(task, tmp_path):
    code, refused = run_cli("identity-amend", "--base-revision", str(_revision() + 5), "--input", _write(tmp_path, "a.json", {
        "base_version": 0, "base_sha256": None, "text": BASELINE, "summary": "s", "rationale": "r",
        "decision": {"decided_by": "owner", "human_instruction": "ok"}}))
    assert code == 2 and "revision conflict" in refused["error"]
    code, refused = _amend(task, tmp_path, BASELINE, decision={"decided_by": "", "human_instruction": ""})
    assert code == 2 and "error" in refused
    assert run_cli("identity-status")[1]["state"] == "ABSENT"


def test_guard_classification():
    reads = {"identity-status", "identity-get", "identity-trace"}
    mutations = {"identity-amend", "identity-challenge", "identity-relate"}
    assert reads <= guards.READ_ONLY_COMMANDS and not reads & guards.CONTROLLER_COMMANDS
    assert mutations <= guards.CONTROLLER_COMMANDS and not mutations & (guards.READ_ONLY_COMMANDS | guards.CURATOR_COMMANDS)


def test_hooks_deny_children_mutation_and_let_reads_through(task):
    for command in ("identity-amend", "identity-challenge", "identity-relate"):
        denied = _runner(task, f"{command} --base-revision 1 --input x.json", agent_id="a1", agent_type="thaliris:implementer")
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny" and "Controller" in denied["hookSpecificOutput"]["permissionDecisionReason"]
    for command in ("identity-status", "identity-get --item INV-1", "identity-trace identity --depth 2"):
        child = _runner(task, command, agent_id="a1", agent_type="thaliris:implementer")["hookSpecificOutput"]
        assert child.get("permissionDecision") != "deny" and "--native-agent a1" in child["updatedInput"]["command"]
    # Plan mode approves only the read-only identity commands.
    plan = {"permission_mode": "plan"}
    assert _runner(task, "identity-status", **plan)["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "permissionDecision" not in _runner(task, "identity-amend --base-revision 1 --input x.json", **plan)["hookSpecificOutput"]


def test_children_cannot_write_the_identity_but_the_controller_is_not_blocked(task):
    target = str(task["root"] / ".project-identity" / "IDENTITY.md")
    child = {"agent_id": "a1", "agent_type": "thaliris:implementer"}
    write = _hook_base(task, "PreToolUse", tool_name="Write", tool_use_id="w", tool_input={"file_path": target, "content": "x"}, **child)
    denied = run_hook("PreToolUse", write)["hookSpecificOutput"]
    assert denied["permissionDecision"] == "deny" and ".project-identity" in denied["permissionDecisionReason"]
    shell = _hook_base(task, "PreToolUse", tool_name="Bash", tool_use_id="s",
                       tool_input={"command": "echo x > .project-identity/IDENTITY.md"}, **child)
    assert run_hook("PreToolUse", shell)["hookSpecificOutput"]["permissionDecision"] == "deny"
    upper = _hook_base(task, "PreToolUse", tool_name="Edit", tool_use_id="e",
                       tool_input={"file_path": target.upper().replace("IDENTITY.MD", "IDENTITY.md"), "old_string": "a", "new_string": "b"}, **child)
    assert run_hook("PreToolUse", upper)["hookSpecificOutput"]["permissionDecision"] == "deny"
    # Reading is not a write, and ordinary project files stay writable by children.
    read = _hook_base(task, "PreToolUse", tool_name="Bash", tool_use_id="r", tool_input={"command": "cat .project-identity/IDENTITY.md"}, **child)
    assert run_hook("PreToolUse", read) is None
    ordinary = _hook_base(task, "PreToolUse", tool_name="Write", tool_use_id="o",
                          tool_input={"file_path": str(task["root"] / "app.py"), "content": "x"}, **child)
    assert run_hook("PreToolUse", ordinary) is None
    controller = _hook_base(task, "PreToolUse", tool_name="Write", tool_use_id="c", tool_input={"file_path": target, "content": "x"})
    assert run_hook("PreToolUse", controller) is None
    assert not guards.protected_target(task["root"], target, controller=True)
    assert guards.protected_target(task["root"], target, controller=False)
    assert guards.protected_target(task["root"], ".project-identity", controller=False)
    assert not guards.protected_target(task["root"], ".project-identity-notes.md", controller=False)


def test_session_start_and_attach_carry_only_the_summary_line(task, tmp_path):
    # Absent identity: nothing is added, for every source and without a task alike.
    for source in ("startup", "resume", "compact", "clear"):
        assert "Project identity v" not in _context(task, source)
    assert "Project Identity" not in run_cli("attach", "--task-id", task["task_id"], "--reason", "new session", session=OTHER_SESSION)[1]

    assert _amend(task, tmp_path, BASELINE)[0] == 0
    summary = run_cli("identity-status")[1]["summary"]
    assert summary.startswith("Project identity v1 (A-0001, CURRENT)")
    for source in ("startup", "resume", "compact", "clear"):
        text = _context(task, source)
        assert f"- {summary}" in text
        assert "Output stays ASCII" not in text and "never prints" not in text  # the line, never the identity text
    # Regardless of task association: another session in the same repository, with no task, sees it too.
    assert f"- {summary}" in _context(task, session=OTHER_SESSION)
    code, attached = run_cli("attach", "--task-id", task["task_id"], "--reason", "new session", session=OTHER_SESSION)
    assert code == 0 and attached["Project Identity"] == summary
    # A hand edit is an observation in the line, not adopted or reverted.
    (task["root"] / ".project-identity" / "IDENTITY.md").write_text(BASELINE_2, encoding="utf-8", newline="\n")
    assert "UNRECORDED_CHANGE" in _context(task)


def test_children_receive_no_identity_text(task, tmp_path):
    assert _amend(task, tmp_path, BASELINE)[0] == 0
    start = run_hook("SubagentStart", _hook_base(task, "SubagentStart", agent_id="a1", agent_type="thaliris:implementer"))
    text = start["hookSpecificOutput"]["additionalContext"]
    assert "Project identity" not in text and "INV-1" not in text and "ASCII" not in text


def test_unreadable_identity_never_breaks_session_start(task):
    directory = task["root"] / ".project-identity"
    directory.mkdir()
    (directory / "ledger.json").write_text("{not json", encoding="utf-8")
    text = _context(task)
    assert "Project identity INVALID" in text  # a damaged ledger is reported as an observation, never raised
    assert run_cli("identity-status")[1]["state"] == "INVALID"
    assert run_cli("identity-get")[0] == 2  # reads refuse while the chain is invalid
    assert store.identity_summary(Path("/definitely/not/a/repository")) is None
