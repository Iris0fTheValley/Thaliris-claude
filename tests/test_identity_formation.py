"""Natural identity formation (Core 0.5.2): identity-note, identity-suggest, the close-time offer."""
from __future__ import annotations

import pytest

from thaliris_claude import guards

from .conftest import run_cli
from .test_identity import OTHER_SESSION, _revision, _runner


@pytest.fixture(autouse=True)
def thaliris_home(tmp_path, monkeypatch):
    """Basis notes and preferences live under THALIRIS_HOME; never the real home."""
    location = tmp_path / "thaliris-home"
    monkeypatch.setenv("THALIRIS_HOME", str(location))
    return location


def _close():
    return run_cli("close", "--base-revision", str(_revision()))


def _restart(task):
    code, started = run_cli("task-start", "Another goal", "--contract", str(task["contract"]))
    assert code == 0, started


def test_note_list_retract_need_no_task_but_the_controller(repo, home, monkeypatch):
    monkeypatch.chdir(repo)
    code, added = run_cli("identity-note", "--quote", "This tool must stay offline.", "--kind", "constraint",
                          "--context", "stated while planning", "--ref", "path:docs/vision.md", session=OTHER_SESSION)
    assert code == 0, added
    assert added["note"]["id"] == "N-0001" and added["task_id"] is None and added["pending"] is True
    code, listed = run_cli("identity-note", "--list", session=OTHER_SESSION)
    assert code == 0 and [n["id"] for n in listed["notes"]] == ["N-0001"]
    code, retracted = run_cli("identity-note", "--retract", "N-0001", "--reason", "misheard", session=OTHER_SESSION)
    assert code == 0 and retracted["retracted"] == "N-0001"
    assert run_cli("identity-note", "--list", session=OTHER_SESSION)[1]["notes"] == []
    assert len(run_cli("identity-note", "--list", "--include-retracted", session=OTHER_SESSION)[1]["notes"]) == 1
    code, refused = run_cli("identity-note", "--quote", "x", session=OTHER_SESSION, agent="agent-x")
    assert code == 2 and "CONTROLLER_ONLY" in refused["error"]
    code, refused = run_cli("identity-note", "--quote", "x", session=None)
    assert code == 2 and "NATIVE_IDENTITY_UNKNOWN" in refused["error"]
    code, refused = run_cli("identity-note", "--retract", "N-0001", session=OTHER_SESSION)
    assert code == 2 and "FLAG_FORM_INCOMPLETE" in refused["error"]
    assert not (repo / ".project-identity").exists()  # nothing tracked is ever created


def test_note_inside_a_task_records_the_task(task):
    code, note = run_cli("identity-note", "--quote", "A tiny offline greeter.", "--kind", "purpose")
    assert code == 0 and note["task_id"] == task["task_id"]


def test_suggest_scopes_pass_the_session_key_and_refuse_children(repo, home, monkeypatch):
    monkeypatch.chdir(repo)
    assert run_cli("identity-suggest", "--off", "--scope", "session")[0] == 0
    status = run_cli("identity-status")[1]
    assert status["ok"] and status["state"] == "ABSENT" and status["suggestions"] == {"enabled": False, "source": "session"}
    assert run_cli("identity-status", session=OTHER_SESSION)[1]["suggestions"]["enabled"] is True  # per native session
    assert run_cli("identity-suggest", "--off", "--scope", "workspace", session=OTHER_SESSION)[0] == 0
    assert run_cli("identity-status", session=OTHER_SESSION)[1]["suggestions"] == {"enabled": False, "source": "workspace"}
    assert run_cli("identity-suggest", "--clear", "--scope", "workspace")[0] == 0
    assert run_cli("identity-suggest", "--on", "--scope", "project")[0] == 0
    code, refused = run_cli("identity-suggest", "--off", "--scope", "global", agent="agent-x")
    assert code == 2 and "CONTROLLER_ONLY" in refused["error"]


def test_hook_denies_children_and_allows_the_controller(task):
    assert {"identity-note", "identity-suggest"} <= guards.CONTROLLER_COMMANDS
    for sub in ("identity-note --list", "identity-suggest --off --scope session"):
        denied = _runner(task, sub, agent_id="child-1", agent_type="thaliris:implementer")
        assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"
        allowed = _runner(task, sub)
        assert not allowed or allowed.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"


def test_close_without_notes_has_no_offer(task):
    code, closed = _close()
    assert code == 0 and "identity_offer" not in closed


def test_close_offers_once_per_new_note_and_respects_off(task):
    run_cli("identity-note", "--quote", "Offline only.", "--kind", "constraint")
    code, closed = _close()
    assert code == 0 and "identity-note --list" in closed["identity_offer"]
    _restart(task)
    code, closed = _close()  # the same note was already offered
    assert code == 0 and "identity_offer" not in closed
    _restart(task)
    run_cli("identity-note", "--quote", "No cloud, ever.", "--kind", "non-goal")
    assert run_cli("identity-suggest", "--off", "--scope", "session")[0] == 0
    code, closed = _close()
    assert code == 0 and "identity_offer" not in closed
