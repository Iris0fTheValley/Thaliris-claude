"""Core 0.6.0 wiring: declared checks, the withholding gate, typed claims, dispatch usage and the adaptive lane."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from thaliris import core

from thaliris_claude import hooks, store

from .conftest import SESSION, run_cli, run_hook
from .test_flow import _base, _dispatch, _handoff, _host_record, _result, _shell, _start

ROOT = Path(__file__).resolve().parents[1]
SH = shutil.which("sh")
PASS = [sys.executable, "-c", "import sys; sys.exit(0)"]
FAIL = [sys.executable, "-c", "import sys; sys.exit(1)"]


def _state(task):
    core.select_task(task["root"], task["task_id"])
    return core._load_state(task["root"])


def _revision(task):
    return _state(task)["revision"]


def _check(task, command=PASS, check_id="t1", **extra):
    payload = {"id": check_id, "requirement_refs": ["R1"], "command": command, **extra}
    return run_cli("check", "--base-revision", str(_revision(task)), "--json", json.dumps(payload))


def _observation(task):
    """One registered observation of candidate C1 for R1 (a Controller import on a real handoff)."""
    handoff = _handoff(_dispatch(task, "tu-obs"))
    code, result = run_cli("observe", "--handoff-id", handoff, "--text", "greeting at app.py:1", "--source", "app.py:1")
    assert code == 0, result
    return result["observation_id"]


def _judge(task, outcome="satisfied", *extra, requirement="R1"):
    observations = [item["id"] for item in _state(task)["evidence_continuity"]["observations"]] or [_observation(task)]
    return run_cli("judge", "--base-revision", str(_revision(task)), "--json", json.dumps(
        {"id": f"J{_revision(task)}", "candidate": "C1", "requirement_ref": requirement, "observation_refs": observations[:1],
         "outcome": outcome, "reasoning": "checked"}), *extra)


# ------------------------------------------------------------------ checks and the gate

def test_check_is_controller_only_and_children_are_denied(task):
    code, result = _check(task)
    assert code == 0 and result["declared"] == ["t1"] and result["checks"][0]["requirement_refs"] == ["R1"]
    code, again = _check(task)
    assert code == 2 and again["code"] == "CHECK_ALREADY_DECLARED"
    code, superseded = run_cli("check", "--base-revision", str(_revision(task)), "--supersede", "--json",
                               json.dumps({"id": "t1", "requirement_refs": ["R1"], "command": FAIL}))
    assert code == 0 and superseded["checks"][0]["declaration"] == 2
    # A child's shell call is denied by the hook, and the runner refuses a child identity anyway.
    child = run_hook("PreToolUse", _shell(task, f"{hooks.runner_command()} check --base-revision 1 --json '{{}}'",
                                          agent_id="a1", agent_type="thaliris:implementer"))
    assert child["hookSpecificOutput"]["permissionDecision"] == "deny"
    code, refused = run_cli("check", "--base-revision", str(_revision(task)), "--json", "{}", agent="a1")
    assert code == 2 and "CONTROLLER_ONLY" in refused["error"]


def test_judge_passes_root_so_the_gate_withholds_and_an_override_is_recorded(task):
    _check(task, FAIL)
    code, withheld = _judge(task)
    assert code == 2 and withheld["code"] == "VERIFICATION_WITHHELD"
    assert withheld["details"]["withheld"][0]["checks"][0]["status"] == "unrun"
    code, red = run_cli("verify", "--check", "t1")
    assert code == 0 and red["result"]["outcome"] == "FAIL" and red["result"]["status"] == "red"
    code, withheld = _judge(task)
    assert code == 2 and withheld["details"]["withheld"][0]["checks"][0]["status"] == "red"
    code, empty = _judge(task, "satisfied", "--override-reason", " ")
    assert code == 2 and empty["code"] == "VERIFICATION_OVERRIDE_INVALID"
    code, overridden = _judge(task, "satisfied", "--override-reason", "flaky runner; verified by hand")
    assert code == 0 and list(overridden["overridden_checks"].values()) == [["t1"]]
    stored = _state(task)["evidence_continuity"]["judgments"][-1]
    assert stored["override_reason"] == "flaky runner; verified by hand"


def test_a_green_check_on_the_current_tree_lifts_the_gate_and_grants_nothing(task):
    _check(task, PASS)
    code, green = run_cli("verify", "--check", "t1")
    assert code == 0 and green["result"]["status"] == "green" and green["result"]["fresh"] is True
    before = len(_state(task)["evidence_continuity"]["judgments"])
    assert before == 0  # a PASS adds no judgment
    code, judged = _judge(task)
    assert code == 0 and "overridden_checks" not in judged
    # A changed tree makes the result stale, so the gate withholds again.
    (task["root"] / "app.py").write_text("print('hello')\n", encoding="utf-8")
    code, stale = _judge(task)
    assert code == 2 and stale["details"]["withheld"][0]["checks"][0]["reason"] == "tree_changed"


def test_compiled_requirement_without_a_check_is_withheld(task):
    code, registered = run_cli("register", "--base-revision", str(_revision(task)), "--requirement", "R2=parser",
                               "--kind", "R2=compiled", "--requirement", "R3=api", "--contract-surface", "R3")
    assert code == 0, registered
    requirements = {item["id"]: item for item in _state(task)["evidence_continuity"]["requirements"]}
    assert requirements["R2"]["kind"] == "compiled" and requirements["R3"]["surface"] == "contract"
    assert "kind" not in requirements["R1"]  # absent means judgment-only
    code, bad = run_cli("register", "--base-revision", str(_revision(task)), "--requirement", "R4=x", "--kind", "R5=compiled")
    assert code == 2 and "INVALID_REQUIREMENT" in bad["error"]
    code, withheld = _judge(task, requirement="R2")
    assert code == 2 and withheld["details"]["withheld"][0]["checks"][0]["reason"] == "no-declared-check"


def test_close_goes_through_close_state(task):
    _check(task, PASS)
    code, withheld = run_cli("close", "--base-revision", str(_revision(task)))
    assert code == 2 and withheld["code"] == "VERIFICATION_WITHHELD"
    assert _state(task)["status"] == "ACTIVE"
    code, closed = run_cli("close", "--base-revision", str(_revision(task)), "--override-reason", "user accepted without the run")
    assert code == 0, closed
    assert closed["closure"]["gate"] == "overridden" and closed["closure"]["overridden_checks"][0]["id"] == "t1"
    assert closed["closure"]["by"] == "claude:controller"
    assert [item["requirement"] for item in closed["surfaced"]] == ["R1"] and closed["surfaced"][0]["kind"] == "judgment-only"
    assert _state(task)["status"] == "DONE"


def test_close_with_green_checks_is_clear_and_surfaces_judgment_only(task):
    _check(task, PASS)
    run_cli("verify", "--check", "t1")
    code, closed = run_cli("close", "--base-revision", str(_revision(task)))
    assert code == 0 and closed["closure"]["gate"] == "clear" and closed["surfaced_total"] == 1


def test_verify_by_a_bound_child_records_its_handoff_producer(task):
    _check(task, PASS)
    handoff = _handoff(_dispatch(task, "tu1", role="implementer"))
    _start(task, "agent-c", "thaliris:implementer", "tu1")
    code, result = run_cli("verify", "--check", "t1", agent="agent-c")
    assert code == 0 and result["result"]["status"] == "green"
    assert set(result["evidence"]) <= {key for key in result["evidence"] if key.endswith("_total")}  # child sees totals only
    assert _state(task)["verification_results"][-1]["observed_by"] == f"handoff:{handoff}"
    code, unbound = run_cli("verify", "--check", "t1", agent="agent-unknown")
    assert code == 2 and "NATIVE_IDENTITY_UNBOUND" in unbound["error"]
    child = run_hook("PreToolUse", _shell(task, f"{hooks.runner_command()} verify --check t1",
                                          agent_id="agent-c", agent_type="thaliris:implementer"))
    assert child["hookSpecificOutput"].get("permissionDecision") != "deny"


# ------------------------------------------------------------------ claims at SubagentStop

def _claims(*claims):
    return "**Conclusion** done\n\n```thaliris-claims\n" + json.dumps({"claims": list(claims)}) + "\n```\n"


def _stop_with(task, agent_id, message, agent_type="thaliris:implementer"):
    return run_hook("SubagentStop", _base(task, "SubagentStop", agent_id=agent_id, agent_type=agent_type,
                                          stop_hook_active=False, last_assistant_message=message))


def _handoff_record(task, handoff):
    return next(item for item in _state(task)["evidence_continuity"]["handoffs"] if item["id"] == handoff)


def _running_child(task, tool_use_id, agent_id):
    handoff = _handoff(_dispatch(task, tool_use_id, role="implementer"))
    _start(task, agent_id, "thaliris:implementer", tool_use_id)
    return handoff


def test_claims_are_ingested_before_the_stop(task):
    handoff = _running_child(task, "tu1", "agent-c")
    message = _claims({"requirement": "R1", "polarity": "contradicts", "text": "greeting still says hi", "evidence": "app.py:1",
                       "basis": "read"},
                      {"requirement": "R1", "polarity": "supports", "text": "test passes", "evidence": "t1", "basis": "check"})
    _stop_with(task, "agent-c", message)
    record = _handoff_record(task, handoff)
    assert record["claims"]["status"] == "present" and record["claims"]["supports"][0]["evidence"] == "t1"
    observation = next(item for item in _state(task)["evidence_continuity"]["observations"] if item["id"] == f"{handoff}.c1")
    assert observation["claim"]["polarity"] == "contradicts" and observation["requirement_refs"] == ["R1"]
    entry = next(item for item in store.read_lifecycle(task["root"], task["task_id"])["dispatches"] if item["handoff_id"] == handoff)
    assert entry["native_status"] == "stopped" and entry["events"][-1]["claims"] == "present"
    # The adverse claim is open evidence for the Controller's next turn.
    assert f"{handoff}.c1" in hooks.evidence_state(task["root"], task["task_id"])["text"]


@pytest.mark.parametrize(("message", "status"), [
    ("done\n\n```thaliris-claims\nnone\n```", "present"),
    ("done, and no block at all", "missing"),
    ("done\n\n```thaliris-claims\n{not json\n```", "malformed"),
    (None, "missing"),
])
def test_none_missing_and_malformed_blocks_leave_only_the_marker(task, message, status):
    handoff = _running_child(task, "tu1", "agent-c")
    observations = len(_state(task)["evidence_continuity"]["observations"])
    _stop_with(task, "agent-c", message)
    assert _handoff_record(task, handoff)["claims"]["status"] == status
    assert len(_state(task)["evidence_continuity"]["observations"]) == observations


def test_claims_ingest_is_idempotent_and_never_after_the_stop(task):
    handoff = _running_child(task, "tu1", "agent-c")
    message = _claims({"requirement": "R1", "polarity": "unknown", "text": "Windows path untested", "evidence": "app.py:1",
                       "basis": "inference"})
    association = (task["root"], task["task_id"])
    payload = _base(task, "SubagentStop", agent_id="agent-c", agent_type="thaliris:implementer")
    assert hooks._ingest_claims(association, "agent-c", payload, message) == ("present", None)
    revision = _revision(task)
    assert hooks._ingest_claims(association, "agent-c", payload, message) == ("present", None)
    assert _revision(task) == revision  # already ingested: nothing written
    _stop_with(task, "agent-c", message)
    _stop_with(task, "agent-c", _claims({"requirement": "R1", "polarity": "contradicts", "text": "late", "evidence": "x:1",
                                         "basis": "read"}))
    assert _revision(task) == revision
    assert [item["id"] for item in _state(task)["evidence_continuity"]["observations"] if "claim" in item] == [f"{handoff}.c1"]


def test_unbound_child_claims_are_not_ingested(task):
    handoff = _handoff(_dispatch(task, "tu1", role="implementer"))
    revision = _revision(task)
    # No Host record: the child cannot be bound exactly, so nothing is attributed to the handoff.
    _stop_with(task, "agent-x", _claims({"requirement": "R1", "polarity": "contradicts", "text": "x", "evidence": "a:1",
                                         "basis": "read"}))
    assert "claims" not in _handoff_record(task, handoff) and _revision(task) == revision


def test_a_failed_ingest_is_a_bounded_diagnostic_and_the_stop_is_still_recorded(task, monkeypatch):
    _running_child(task, "tu1", "agent-c")

    def broken(*args, **kwargs):
        raise ValueError("boom " + "x" * 500)

    monkeypatch.setattr(core, "ingest_claims", broken)
    assert _stop_with(task, "agent-c", _claims()) is None
    entry = store.read_lifecycle(task["root"], task["task_id"])["dispatches"][-1]
    assert entry["native_status"] == "stopped"
    failure = next(event for event in entry["events"] if event["event"] == "claims-not-ingested")
    assert failure["error"].startswith("ValueError: boom") and len(failure["error"]) <= 240


# ------------------------------------------------------------------ dispatch usage

def _notification(agent, status="completed", usage="<usage><subagent_tokens>1234</subagent_tokens><tool_uses>7</tool_uses>"
                  "<duration_ms>9000</duration_ms></usage>"):
    return f"<task-notification>\n<task-id>{agent}</task-id>\n<status>{status}</status>\n{usage}\n</task-notification>"


def test_background_notification_usage_is_recorded_once(task):
    handoff = _handoff(_dispatch(task, "tu1", model="sonnet", effort="high"))
    _result(task, "tu1", {"isAsync": True, "status": "async_launched", "agentId": "agent-a"})
    context = run_hook("UserPromptSubmit", _base(task, "UserPromptSubmit", prompt=_notification("agent-a")))
    assert handoff in context["hookSpecificOutput"]["additionalContext"]
    usage = _handoff_record(task, handoff)["usage"]
    assert usage == {"model": "sonnet", "effort": "high", "input_tokens": None, "output_tokens": None, "total_tokens": 1234,
                     "cost_usd": None, "duration_ms": 9000, "tool_uses": 7, "outcome": "completed"}
    revision = _revision(task)
    run_hook("UserPromptSubmit", _base(task, "UserPromptSubmit", prompt=_notification("agent-a")))
    assert _revision(task) == revision  # the same report again costs no revision


@pytest.mark.parametrize("prompt", [
    "<task-notification><task-id>agent-a</task-id><task-id>agent-b</task-id><status>completed</status></task-notification>",
    "<task-notification><task-id>bad id!</task-id><status>completed</status></task-notification>",
    "<task-notification><task-id>agent-a</task-id></task-notification>",
    "<task-id>agent-a</task-id><status>completed</status>",
    "<task-notification><task-id>agent-zzz</task-id><status>completed</status></task-notification>",
])
def test_malformed_or_foreign_notifications_are_ignored(task, prompt):
    handoff = _handoff(_dispatch(task, "tu1"))
    _result(task, "tu1", {"isAsync": True, "status": "async_launched", "agentId": "agent-a"})
    revision = _revision(task)
    run_hook("UserPromptSubmit", _base(task, "UserPromptSubmit", prompt=prompt))
    assert "usage" not in _handoff_record(task, handoff) and _revision(task) == revision


def test_unreadable_counts_stay_null():
    parsed = hooks.background_notifications(_notification("agent-a", "failed", "<usage><subagent_tokens>12a</subagent_tokens>"
                                                          "<tool_uses>3</tool_uses></usage>"))
    assert parsed == [{"agent_id": "agent-a", "outcome": "failed", "measured": {"tool_uses": 3}, "phase": "unknown"}]


def test_foreground_usage_is_read_best_effort(task):
    handoff = _handoff(_dispatch(task, "tu1"))
    _result(task, "tu1", {"status": "completed", "agentId": "agent-f", "totalTokens": 500, "totalDurationMs": 1200,
                          "totalToolUseCount": 4, "usage": {"input_tokens": 300, "output_tokens": "x"}})
    usage = _handoff_record(task, handoff)["usage"]
    assert (usage["total_tokens"], usage["duration_ms"], usage["tool_uses"], usage["input_tokens"], usage["output_tokens"]) == (
        500, 1200, 4, 300, None)


# ------------------------------------------------------------------ packet

def test_packet_shows_verification_lines_without_open_observations_and_they_join_the_digest(task):
    assert hooks.evidence_state(task["root"], task["task_id"]) is None
    _check(task, PASS)
    unrun = hooks.evidence_state(task["root"], task["task_id"])
    assert unrun and "check t1: unrun" in unrun["text"] and "check t1: unrun" in unrun["digest"]
    run_cli("verify", "--check", "t1")
    assert hooks.evidence_state(task["root"], task["task_id"]) is None  # green needs no attention
    (task["root"] / "app.py").write_text("print('changed')\n", encoding="utf-8")
    stale = hooks.evidence_state(task["root"], task["task_id"])
    assert "stale (tree_changed)" in stale["text"] and stale["digest"] != unrun["digest"]


def test_packet_shows_handoffs_without_typed_claims(task):
    handoff = _running_child(task, "tu1", "agent-c")
    _stop_with(task, "agent-c", "no block")
    text = hooks.evidence_state(task["root"], task["task_id"])["text"]
    assert "Claims:" in text and handoff in text and "missing 1" in text


def test_status_carries_the_new_sections(task):
    _check(task, PASS)
    code, status = run_cli("status")
    assert code == 0 and status["Verification"]["unrun"] == 1 and status["Verification"]["live"] is True
    assert "Advisories" not in status  # delegated mode


# ------------------------------------------------------------------ adaptive mode and the lane counter

@pytest.fixture
def adaptive(home, repo, monkeypatch):
    monkeypatch.chdir(repo)
    code, started = run_cli("task-start", "Fix the greeting", "--human-instruction", "fix it", "--acceptance", "prints hello",
                            "--execution-mode", "adaptive", "--write-surface", "app.py")
    assert code == 0, started
    return {"root": repo, "task_id": started["task_id"], "started": started, "home": home,
            "transcript": str(repo.parent / "transcripts" / f"{SESSION}.jsonl")}


def test_adaptive_quick_admission_records_unspecified_fields(adaptive):
    contract = store.authority(adaptive["root"], adaptive["task_id"]).read()["contract"]
    assert contract["boundary"] == contract["invariants"] == "unspecified" and contract["write_surface"] == ["app.py"]
    assert "lane" in adaptive["started"]
    code, status = run_cli("status")
    assert status["Advisories"]["blocking"] is False and status["Advisories"]["contract_unspecified"] == ["boundary", "invariants"]
    run_cli("detach", "--reason", "try a delegated admission")
    code, refused = run_cli("task-start", "x", "--human-instruction", "x", "--acceptance", "x", "--execution-mode", "delegated")
    assert code == 2 and "FLAG_FORM_INCOMPLETE" in refused["error"]  # quick admission is adaptive only


def test_adaptive_mode_is_a_standing_choice(monkeypatch):
    monkeypatch.setenv(hooks.MODE_SETTING, "adaptive")
    assert "**adaptive**" in hooks._mode_preference()


def _lane_payload(task, chars, tool="Read", **extra):
    return _base(task, "PostToolUse", tool_name=tool, tool_use_id="r1", tool_input={},
                 tool_response={"file": {"content": "x" * chars}, "numLines": 1}, **extra)


def _lane_tokens(task):
    return core.lane_counters(task["root"], task["task_id"])["controller_tool_output_tokens"]


def test_lane_counter_counts_controller_output_only_in_adaptive_mode(adaptive):
    marker = store.lane_marker(SESSION)
    assert marker.is_file()
    run_hook("PostToolUse", _lane_payload(adaptive, 401))
    assert _lane_tokens(adaptive) == 101  # ceil(401 / 4)
    run_hook("PostToolUse", _lane_payload(adaptive, 400, agent_id="agent-c", agent_type="thaliris:implementer"))
    assert _lane_tokens(adaptive) == 101  # a child's output is not the Controller's
    revision = _state(adaptive)["revision"]
    run_hook("PostToolUse", _lane_payload(adaptive, 4, tool="Bash"))
    assert _lane_tokens(adaptive) == 102 and _state(adaptive)["revision"] == revision  # no ledger revision
    code, closed = run_cli("close", "--base-revision", str(revision))
    assert code == 0, closed
    assert not marker.exists()


def test_lane_counter_is_inert_outside_adaptive_mode(task):
    assert not store.lane_marker(SESSION).exists()
    run_hook("PostToolUse", _lane_payload(task, 4000))
    assert _lane_tokens(task) == 0


def test_mode_switch_and_detach_maintain_the_marker(adaptive):
    marker = store.lane_marker(SESSION)
    code, status = run_cli("status")
    sha = store.authority(adaptive["root"], adaptive["task_id"]).read()  # noqa: F841 - read succeeds
    from thaliris import authority as core_authority
    digest = core_authority.digest(store.authority(adaptive["root"], adaptive["task_id"]).path())
    code, switched = run_cli("mode", "--mode", "delegated", "--human-instruction", "delegate it", "--base-revision",
                             str(status["Task"]["revision"]), "--expected-authority-sha256", digest)
    assert code == 0, switched
    assert not marker.exists()
    run_cli("detach", "--reason", "done here")
    assert not marker.exists()


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh (Git Bash on Windows)")
def test_launcher_fast_path_starts_python_only_with_a_marker(adaptive):
    home = Path(os.environ["THALIRIS_CLAUDE_HOME"])
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    (home / "runtime" / "python").write_text(Path(sys.executable).as_posix() + "\n", encoding="utf-8")
    environment = {**os.environ, "THALIRIS_CLAUDE_HOME": str(home)}

    def launch(chars):
        subprocess.run([SH, str(ROOT / "bin" / "thaliris-hook"), "PostToolUse", "--lane"],
                       input=json.dumps(_lane_payload(adaptive, chars)).encode(), capture_output=True, env=environment, check=True)

    launch(40)
    assert _lane_tokens(adaptive) == 10
    marker = store.lane_marker(SESSION)
    marker.unlink()
    launch(40)
    # Python would have counted and re-created the marker (the task is still adaptive): the launcher exited first.
    assert _lane_tokens(adaptive) == 10 and not marker.exists()


def test_hooks_register_the_lane_matcher_behind_the_fast_path():
    groups = json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]["PostToolUse"]
    lane = [group for group in groups if "--lane" in group["hooks"][0]["command"]]
    assert len(lane) == 1 and set(lane[0]["matcher"].split("|")) == set(hooks.LANE_TOOLS)
