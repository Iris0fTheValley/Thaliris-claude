"""Controller management overhead: compact output, structured errors, batches, one-lock writes, evidence dedupe."""
from __future__ import annotations

import json
import threading

from thaliris import core
from thaliris.authority import AuthorityStore

from thaliris_claude import hooks

from .conftest import SESSION, run_cli, run_hook
from .test_flow import _base, _dispatch, _handoff, _result, _start, _stop


def _revision() -> int:
    return run_cli("status")[1]["Task"]["revision"]


def _observed(task, agent_id="agent-a", tool_use_id="tu1", text="greeting printed at app.py:1"):
    """Dispatch one investigator, let it observe once, and return (handoff id, observation id)."""
    handoff = _handoff(_dispatch(task, tool_use_id))
    _start(task, agent_id, "thaliris:investigator", tool_use_id)
    code, observed = run_cli("observe", "--text", text, "--source", "app.py:1", agent=agent_id)
    assert code == 0, observed
    return handoff, observed["observation_id"]


# ------------------------------------------------------------ structured errors

def test_revision_conflict_is_structured_everywhere(task):
    stale = task["revision"] - 1
    for command in (("register", "--requirement", "R2=x"),
                    ("judge", "--requirement", "R1", "--candidate", "C1", "--outcome", "unknown", "--observations", "O1",
                     "--reasoning", "r"),
                    ("task-update", "--milestone", "m"),
                    ("close",)):
        code, error = run_cli(*command, "--base-revision", str(stale))
        assert code == 2 and error["ok"] is False and error["error"] == "task revision conflict", (command, error)
        assert error["code"] == "TASK_REVISION_CONFLICT" and "--base-revision" in error["recovery"]
        details = error["details"]
        assert details["current_revision"] == task["revision"] and details["base_revision"] == stale
        assert details["changes_since"]["exact"] is False and "evidence" in details["changes_since"]
    assert _revision() == task["revision"]  # nothing was written


def test_adapter_errors_carry_a_code_and_keep_their_text(task):
    code, error = run_cli("status", session=None)
    assert code == 2 and error["code"] == "NATIVE_IDENTITY_UNKNOWN" and error["error"].startswith("NATIVE_IDENTITY_UNKNOWN:")
    code, error = run_cli("close", "--base-revision", "not-a-number")
    assert code == 2 and "code" not in error  # argparse text: no invented code
    code, error = run_cli("observe", "--handoff-id", "H0000000", "--text", "x", "--source", "y")
    assert code == 2 and error["code"] == "HANDOFF_NOT_FOUND"


def test_runner_output_is_compact_utf8(task, capsys):
    from thaliris_claude import cli
    assert cli.main(["task-update", "--base-revision", str(task["revision"]), "--record", "decision:保留解析器",
                     "--native-session", SESSION]) == 0
    text = capsys.readouterr().out
    assert text.count("\n") == 1 and "\n  " not in text and ": " not in text
    assert json.loads(text)["appended"] == ["D1"]


# ------------------------------------------------------------ compact status and write acknowledgements

def test_status_is_compact_by_default_and_full_on_request(task):
    handoff, _ = _observed(task)
    for index in range(15):
        assert run_cli("task-update", "--base-revision", str(_revision()), "--record", f"fact:{'x' * 400} {index}")[0] == 0
    code, compact = run_cli("status")
    assert code == 0 and compact["view"] == "compact"
    assert set(compact) >= {"Task", "Counts", "Evidence", "Authority", "Dispatches", "Records", "Active Work", "Pending Results"}
    assert not {"Evidence Continuity", "Claude Dispatches", "Current Records"} & set(compact)
    assert set(compact["Authority"]) == {"status", "execution_mode", "sha256"}
    assert compact["Dispatches"]["total"] == 1 and compact["Dispatches"]["by_native_status"] == {"running": 1}
    assert [item["handoff_id"] for item in compact["Dispatches"]["open"]] == [handoff]
    assert set(compact["Dispatches"]["open"][0]) == {"handoff_id", "role", "native_status", "requested_model", "background"}
    records = compact["Records"]
    assert records["total"] == 15 and len(records["items"]) == 12
    assert all(set(item) == {"id", "kind", "text"} and len(item["text"]) <= 201 for item in records["items"])
    assert compact["Evidence"]["unincorporated_total"] == 1
    full = run_cli("status", "--view", "full")[1]
    assert {"Evidence Continuity", "Claude Dispatches", "Current Records", "Current Record Count", "Claude Dispatch Count",
            "Authority"} <= set(full)
    assert full["Current Record Count"] == 15 and len(full["Current Records"]) == 12
    assert len(json.dumps(full)) > 1.5 * len(json.dumps(compact))
    since = run_cli("status", "--since-revision", "1")[1]["Changes Since"]
    assert since["exact"] is False and since["current_revision"] == _revision()
    assert "Changes Since" in run_cli("status", "--view", "full", "--since-revision", "1")[1]


def test_compact_status_pages_open_evidence_without_dropping_any(task):
    handoff = _handoff(_dispatch(task, "tu1"))
    _start(task, "agent-a", "thaliris:investigator", "tu1")
    observations = [{"id": f"O{index}", "text": "t", "source_refs": ["S%d" % index],
                     "sources": [{"id": "S%d" % index, "kind": "local", "locator": "app.py:1", "summary": "s" * 200}]}
                    for index in range(1, 31)]
    code, batch = run_cli("observe", "--json", json.dumps({"observations": observations}), agent="agent-a")
    assert code == 0 and len(batch["appended"]) == 30 and "observation_id" not in batch
    seen, offset = [], 0
    while offset is not None:
        evidence = run_cli("status", "--evidence-offset", str(offset))[1]["Evidence"]
        assert evidence["unincorporated_total"] == 30
        seen += [item["observation_id"] for item in evidence["unincorporated"]]
        offset = evidence["next_offset"]
    assert seen == [f"{handoff}.O{index}" for index in range(1, 31)]


def test_writes_answer_with_ack_and_evidence_summary_not_the_ledger(task):
    handoff, observation = _observed(task)
    code, judged = run_cli("judge", "--base-revision", str(_revision()), "--requirement", "R1", "--candidate", "C1",
                           "--outcome", "repair", "--observations", observation, "--reasoning", "still wrong")
    assert code == 0 and judged["appended"] == ["J1"] and judged["revision"] == _revision() and judged["changed"] == ["evidence_continuity"]
    assert judged["evidence"]["unresolved_judgments_total"] == 1 and judged["evidence"]["unincorporated_total"] == 0
    assert not {"Evidence Continuity", "Task Context"} & set(judged)
    code, updated = run_cli("task-update", "--base-revision", str(_revision()), "--record", "fact:x")
    assert set(updated) == {"ok", "task_id", "revision", "status", "changed", "appended", "evidence"}
    code, artifact = run_cli("artifact", "--base-revision", str(_revision()), "--id", "A1", "--path", "app.py", "--summary", "the app")
    assert code == 0 and artifact["changed"] == ["artifact_refs"] and "evidence" in artifact
    # A child's observation reports totals, not the Controller's open-item list.
    _handoff(_dispatch(task, "tu2"))
    _start(task, "agent-b", "thaliris:investigator", "tu2")
    code, child = run_cli("observe", "--text", "t", "--source", "s", agent="agent-b")
    assert code == 0 and set(child["evidence"]) == {"unincorporated_total", "unresolved_judgments_total", "latest_judgments_total"}
    code, closed = run_cli("close", "--base-revision", str(_revision()))
    assert code == 2  # agent-b is still running: closure refuses
    _stop(task, "agent-a", "thaliris:investigator")
    _stop(task, "agent-b", "thaliris:investigator")
    code, closed = run_cli("close", "--base-revision", str(_revision()))
    assert code == 0 and closed["status"] == "DONE" and closed["evidence"]["unresolved_judgments_total"] == 1
    assert closed["revision"] == core.task_status(task["root"])["Task"]["revision"]


# ------------------------------------------------------------ batches and references

def test_sibling_observations_with_the_same_id_stay_distinct_and_judge_resolves_bare_refs(task):
    first = _handoff(_dispatch(task, "tu1"))
    second = _handoff(_dispatch(task, "tu2"))
    _start(task, "agent-a", "thaliris:investigator", "tu1")
    _start(task, "agent-b", "thaliris:investigator", "tu2")
    one = run_cli("observe", "--text", "a", "--source", "a.py:1", agent="agent-a")[1]
    two = run_cli("observe", "--text", "b", "--source", "b.py:1", agent="agent-b")[1]
    only = run_cli("observe", "--text", "b2", "--source", "b.py:2", "--id", "O2", agent="agent-b")[1]
    assert (one["observation_id"], two["observation_id"], only["observation_id"]) == (f"{first}.O1", f"{second}.O1", f"{second}.O2")
    revision = str(_revision())
    # Bare O1 is ambiguous: refused, nothing written.
    code, refused = run_cli("judge", "--base-revision", revision, "--requirement", "R1", "--candidate", "C1", "--outcome", "unknown",
                            "--observations", "O1", "--reasoning", "r")
    assert code == 2 and _revision() == int(revision)
    # Bare O2 is unique and resolves; qualified ids always work.
    code, judged = run_cli("judge", "--base-revision", revision, "--requirement", "R1", "--candidate", "C1", "--outcome", "satisfied",
                           "--observations", f"O2,{first}.O1", "--reasoning", "r")
    assert code == 0, judged
    assert run_cli("get", "J1")[1]["object"]["observation_refs"] == [f"{second}.O2", f"{first}.O1"]


def _observation_for(task, tool_use_id, candidate, requirements):
    """A fresh investigator, handed `candidate`/`requirements`, makes one observation; returns its qualified id."""
    from .test_flow import _agent_call
    prompt = "Look.\nthaliris-evidence: " + json.dumps({"candidate": candidate, "requirement_refs": requirements})
    handoff = _handoff(run_hook("PreToolUse", _agent_call(task, tool_use_id, "thaliris:investigator", prompt)))
    _start(task, f"agent-{tool_use_id}", "thaliris:investigator", tool_use_id)
    code, observed = run_cli("observe", "--text", "x", "--source", "app.py:1", agent=f"agent-{tool_use_id}")
    assert code == 0 and observed["observation_id"] == f"{handoff}.O1", observed
    return observed["observation_id"]


def test_bare_observation_ids_resolve_only_within_the_judged_candidate_and_requirement(task):
    run_cli("register", "--base-revision", str(_revision()), "--requirement", "R2=second")
    other_candidate = _observation_for(task, "tu1", "C2", ["R1"])
    judge = ["judge", "--candidate", "C1", "--requirement", "R1", "--outcome", "unknown", "--reasoning", "r"]
    # The only suffix match belongs to another candidate: it is not chosen, Core refuses the bare id.
    revision = _revision()
    code, refused = run_cli(*judge[:1], "--base-revision", str(revision), *judge[1:], "--observations", "O1")
    assert code == 2 and refused.get("code") and _revision() == revision
    first = _observation_for(task, "tu2", "C1", ["R1"])
    other_requirement = _observation_for(task, "tu3", "C1", ["R2"])
    # Three suffix matches, one for this candidate and requirement: it resolves, and the response echoes it per judgment.
    revision = _revision()
    code, judged = run_cli("judge", "--base-revision", str(revision), *judge[1:], "--observations", "O1")
    assert code == 0, judged
    assert judged["observation_refs"] == {"J1": [first]}
    # Judging R2 with a bare id finds nothing scoped to R2 for C1 other than its own: resolved; for C2 it finds none: refused.
    code, scoped = run_cli("judge", "--base-revision", str(judged["revision"]), "--candidate", "C1", "--judgment", "R2|unknown|O1|r")
    assert code == 0 and scoped["observation_refs"] == {"J2": [other_requirement]}
    code, none = run_cli("judge", "--base-revision", str(scoped["revision"]), "--candidate", "C2", "--judgment", "R2|unknown|O1|r")
    assert code == 2 and none.get("code") and _revision() == scoped["revision"]
    assert other_candidate not in {first, other_requirement}


def test_batch_judge_by_flags_and_by_json_is_atomic_and_one_revision(task):
    handoff, observation = _observed(task)
    run_cli("register", "--base-revision", str(_revision()), "--requirement", "R2=second")
    revision = _revision()
    common = ["judge", "--base-revision", str(revision), "--candidate", "C1"]
    code, bad = run_cli(*common, "--judgment", "R1|satisfied|O1|fine", "--judgment", "R2|maybe|O1|fine")
    assert code == 2 and bad["code"] == "INVALID_JUDGMENT" and _revision() == revision
    code, mixed = run_cli(*common, "--judgment", "R1|satisfied|O1|fine", "--outcome", "repair")
    assert code == 2 and mixed["code"] == "CONFLICTING_FLAGS"
    code, none = run_cli("judge", "--base-revision", str(revision), "--judgment", "R1|satisfied|O1|fine")
    assert code == 2 and none["code"] == "FLAG_FORM_INCOMPLETE"
    # A later judgment in the batch that Core rejects leaves the earlier one unwritten.
    code, partial = run_cli(*common, "--judgment", "R1|satisfied|O1|fine", "--judgment", "R2|satisfied|NOPE|fine")
    assert code == 2 and _revision() == revision
    code, judged = run_cli(*common, "--judgment", "R1|satisfied|O1|a reason, with | pipes and 中文", "--judgment", f"R2|unknown|{observation}|b")
    assert code == 0 and judged["appended"] == ["J1", "J2"] and judged["revision"] == revision + 1
    assert run_cli("get", "J1")[1]["object"]["reasoning"] == "a reason, with | pipes and 中文"
    payload = {"judgments": [{"id": "J3", "candidate": "C1", "requirement_ref": "R1", "observation_refs": [observation],
                              "outcome": "repair", "reasoning": "x"},
                             {"id": "J4", "candidate": "C1", "requirement_ref": "R2", "observation_refs": [observation],
                              "outcome": "no-current-impact", "reasoning": "y"}]}
    code, batch = run_cli("judge", "--base-revision", str(revision + 1), "--json", json.dumps(payload))
    assert code == 0 and batch["appended"] == ["J3", "J4"] and batch["revision"] == revision + 2


# ------------------------------------------------------------ one lock span

def test_authority_checkpoint_happens_inside_the_lock(task, monkeypatch):
    """task-update used to re-anchor authority after releasing the task lock: a concurrent hook then saw new state bytes
    against the old anchor (TASK_AUTHORITY_STATE_CHANGED)."""
    real_lock, real_checkpoint, outcome = core._lock, AuthorityStore.checkpoint, {}

    def checkpoint(self):
        contender = threading.Thread(target=lambda: _hold(real_lock, task["root"], outcome))
        contender.start()
        contender.join(0.6)
        outcome["blocked_at_checkpoint"] = contender.is_alive()
        outcome["thread"] = contender
        real_checkpoint(self)

    monkeypatch.setattr(AuthorityStore, "checkpoint", checkpoint)
    code, updated = run_cli("task-update", "--base-revision", str(task["revision"]), "--record", "fact:x")
    assert code == 0, updated
    outcome["thread"].join(5)
    assert outcome["blocked_at_checkpoint"] is True and outcome.get("acquired") is True
    assert core._lock is real_lock  # the patched lock never outlives the call


def _hold(lock, root, outcome):
    with lock(root):
        outcome["acquired"] = True


def test_state_tampered_before_a_write_is_not_anchored(task):
    """The authority check and Core's write share one lock span, so the checkpoint cannot bless foreign bytes."""
    state_path = core._state_path(task["root"], task["task_id"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["goal"] = "a different goal"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    code, refused = run_cli("task-update", "--base-revision", str(task["revision"]), "--record", "fact:x")
    assert code == 2 and "TASK_AUTHORITY" in refused["error"] and refused["code"].startswith("TASK_AUTHORITY")


# ------------------------------------------------------------ evidence packets for parallel launches

def test_parallel_background_launches_present_the_packet_once(task):
    _observed(task, "agent-a", "tu1")
    _dispatch(task, "tu2")
    _dispatch(task, "tu3")
    launch = {"isAsync": True, "status": "async_launched"}

    def context(result):
        return result["hookSpecificOutput"]["additionalContext"]

    first = context(_result(task, "tu2", {**launch, "agentId": "agent-b"}))
    assert "Thaliris evidence for task" in first and "1 observation(s)" in first and "task revision now" in first
    second = context(_result(task, "tu3", {**launch, "agentId": "agent-c"}))
    assert "Thaliris evidence for task" not in second and "Evidence unchanged: 1 unincorporated, 0 unresolved" in second
    assert "task revision now " in second and len(second) < len(first) // 2 + 200
    # The Controller already saw it: a new turn does not repeat it either.
    assert run_hook("UserPromptSubmit", _base(task, "UserPromptSubmit", prompt="continue")) is None
    # A child's final report is where the Controller judges: the full packet comes with it.
    _stop(task, "agent-a", "thaliris:investigator")
    final = context(_result(task, "tu1", {"status": "completed", "agentId": "agent-a"}))
    assert "Thaliris evidence for task" in final
    # New evidence changes the digest, so the next launch acknowledgement carries the packet again.
    _start(task, "agent-b", "thaliris:investigator", "tu2")
    assert run_cli("observe", "--text", "more", "--source", "b.py:1", agent="agent-b")[0] == 0
    _dispatch(task, "tu4")
    again = context(_result(task, "tu4", {**launch, "agentId": "agent-d"}))
    assert "Thaliris evidence for task" in again and "2 observation(s)" in again
    # Compaction forgets what was shown, so the next launch re-presents it even though nothing changed.
    run_hook("SessionStart", _base(task, "SessionStart", source="compact"))
    _dispatch(task, "tu5")
    assert "Thaliris evidence for task" in context(_result(task, "tu5", {**launch, "agentId": "agent-e"}))


def test_evidence_digest_ignores_the_revision(task):
    _observed(task)
    before = hooks.evidence_state(task["root"], task["task_id"])
    assert run_cli("task-update", "--base-revision", str(_revision()), "--record", "fact:x")[0] == 0
    after = hooks.evidence_state(task["root"], task["task_id"])
    assert after["revision"] == before["revision"] + 1 and after["digest"] == before["digest"]
    assert f"task revision now {after['revision']}" in after["text"]
