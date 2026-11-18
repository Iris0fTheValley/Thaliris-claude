"""Upgrade continuity: an ACTIVE task keeps working across plugin, Core and model-alias updates.

Older lifecycle files migrate forward; only a newer one is refused (with an upgrade message). A renamed or versioned model
alias keeps resolving; an unresolvable one refuses managed dispatch only, never close, dispose or detach. Older anchor
shapes load without repair. None of this loosens an identity, ownership or fence refusal.
"""
from __future__ import annotations

import json

import pytest

from thaliris_claude import store

from .conftest import run_cli, run_hook
from .test_continuity import IMPL, _lifecycle_file, _running_implementer, _write
from .test_flow import _decision, _dispatch, _reason, _stop


def _read(task):
    return json.loads(_lifecycle_file(task).read_text(encoding="utf-8"))


def _anchor(task):
    return store.authority(task["root"], task["task_id"]).path()


def _freeze(task, constraint, drop=()):
    """Rewrite the anchored contract as an earlier plugin would have frozen it (test-only; the anchor is outside the repo)."""
    path = _anchor(task)
    record = json.loads(path.read_text(encoding="utf-8"))
    if constraint is not None:
        record["contract"]["execution_constraint"] = constraint
    for name in drop:
        record["contract"].pop(name, None)
        record.pop(name, None)
    path.write_text(json.dumps(record, sort_keys=True, indent=2), encoding="utf-8")


# ------------------------------------------------- lifecycle: older versions migrate, newer ones are refused

@pytest.mark.parametrize("old", [None, 0])
def test_older_lifecycle_version_migrates_and_is_persisted_by_the_next_transaction(task, old):
    handoff = _running_implementer(task)
    value = _read(task)
    if old is None:
        del value["version"]
    else:
        value["version"] = old
    _lifecycle_file(task).write_text(json.dumps(value), encoding="utf-8")

    # Reading migrates in memory without touching the file; the child keeps its identity and write grant.
    assert store.read_lifecycle(task["root"], task["task_id"])["version"] == store.LIFECYCLE_VERSION
    assert _read(task).get("version") == old
    assert run_hook("PreToolUse", _write(task, **IMPL)) is None
    assert run_cli("observe", "--text", "edited app.py", "--source", "app.py:1", agent="impl")[0] == 0

    # The next transaction persisted the new version, and everything else survived byte-for-meaning.
    migrated = _read(task)
    assert migrated["version"] == store.LIFECYCLE_VERSION and migrated["dispatches"][0]["handoff_id"] == handoff
    assert "updatedInput" in _dispatch(task, "tu2", role="reviewer")["hookSpecificOutput"]
    assert run_cli("dispose", "--handoff-id", handoff, "--reason", "x")[0] == 0


def test_migration_persists_even_through_an_observe_only_transaction(task):
    _running_implementer(task)
    value = _read(task)
    del value["version"]
    _lifecycle_file(task).write_text(json.dumps(value), encoding="utf-8")
    _stop(task, "impl", "thaliris:implementer")  # SubagentStop records native facts without an authority check
    assert _read(task)["version"] == store.LIFECYCLE_VERSION


def test_a_disposed_child_stays_fenced_across_migration(task):
    handoff = _running_implementer(task)
    assert run_cli("dispose", "--handoff-id", handoff, "--reason", "cancelled")[0] == 0
    value = _read(task)
    del value["version"]
    _lifecycle_file(task).write_text(json.dumps(value), encoding="utf-8")
    assert _decision(run_hook("PreToolUse", _write(task, **IMPL))) == "deny"
    assert store.read_lifecycle(task["root"], task["task_id"])["dispatches"][0]["disposed"] is True


def test_lifecycle_entries_without_optional_fields_load(task):
    handoff = _running_implementer(task)
    value = _read(task)
    value["version"] = 0
    for field in ("role_binding", "requested_effort", "observed_model", "observed_tools", "requested_model", "background"):
        value["dispatches"][0].pop(field, None)
    value["unknown_future_field"] = {"kept": True}  # a same-version addition by a newer adapter survives a rewrite
    _lifecycle_file(task).write_text(json.dumps(value), encoding="utf-8")
    full = run_cli("status", "--view", "full")[1]["Claude Dispatches"][0]
    assert full["handoff_id"] == handoff and full["role_binding"] is None
    assert run_cli("observe", "--text", "edited app.py", "--source", "app.py:1", agent="impl")[0] == 0
    assert _read(task)["unknown_future_field"] == {"kept": True}


@pytest.mark.parametrize("version", [store.LIFECYCLE_VERSION + 1, 99])
def test_newer_lifecycle_is_refused_with_an_upgrade_message(task, version):
    _running_implementer(task)
    value = _read(task)
    value["version"] = version
    before = json.dumps(value)
    _lifecycle_file(task).write_text(before, encoding="utf-8")

    code, error = run_cli("status")
    assert code == 2 and error["code"] == "CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED"
    assert "Upgrade the Thaliris plugin" in error["error"] and str(version) in error["error"]
    assert "plugin update" in error["recovery"] and "attach --task-id" in error["recovery"]
    denied = _dispatch(task, "tu2")
    assert _decision(denied) == "deny" and "CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED" in _reason(denied)
    assert _decision(run_hook("PreToolUse", _write(task, **IMPL))) == "deny"  # children fail closed
    assert _lifecycle_file(task).read_text(encoding="utf-8") == before  # never rewritten or reinterpreted


@pytest.mark.parametrize("version", ["1", 1.5, True, -1, None])
def test_a_non_version_value_is_invalid_not_migrated(task, version):
    _running_implementer(task)
    value = _read(task)
    value["version"] = version
    _lifecycle_file(task).write_text(json.dumps(value), encoding="utf-8")
    code, error = run_cli("status")
    assert code == 2 and error["code"] == "CLAUDE_LIFECYCLE_INVALID"


def test_migration_chain_runs_each_step_in_order(task, monkeypatch):
    monkeypatch.setattr(store, "LIFECYCLE_VERSION", 3)
    monkeypatch.setattr(store, "LIFECYCLE_MIGRATIONS", {
        0: lambda value: {**value, "version": 1},
        1: lambda value: {**value, "version": 2, "step_two": True},
        2: lambda value: {**value, "version": 3, "step_three": True}})
    path = _lifecycle_file(task)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "task_id": task["task_id"], "dispatches": []}), encoding="utf-8")
    value, migrated = store.load_lifecycle(task["root"], task["task_id"])
    assert migrated and value["version"] == 3 and value["step_two"] and value["step_three"]
    monkeypatch.setattr(store, "LIFECYCLE_MIGRATIONS", {0: lambda value: {**value, "version": 1}})
    with pytest.raises(Exception, match="CLAUDE_LIFECYCLE_INVALID"):
        store.load_lifecycle(task["root"], task["task_id"])  # a missing step is a coded error, never a guess


# ------------------------------------------------- model aliases: renames resolve, unknown ones refuse dispatch only

@pytest.mark.parametrize("name, current", [
    ("sonnet", "sonnet"), ("Opus", "opus"), ("claude-sonnet-4-5", "sonnet"), ("claude-opus-4-1", "opus"),
    ("sonnet-4.5", "sonnet"), ("claude-3-5-sonnet-20241022", "sonnet"), ("haiku-latest", "haiku"), ("opus[1m]", "opus"),
    ("claude-fable", "fable")])
def test_renamed_or_versioned_aliases_resolve_to_the_current_family(name, current):
    assert store.resolve_alias(name) == current
    assert store.validate_constraint({"execution_constraint": f"claude-model:{name}"}) == current


@pytest.mark.parametrize("name", ["inherit", "luna-only", "", "sonnetx", "gpt-5", "claude", "sonnet/opus", None])
def test_unresolvable_aliases_do_not_resolve(name):
    assert store.resolve_alias(name) is None
    with pytest.raises(ValueError, match="UNSUPPORTED_EXECUTION_CONSTRAINT"):
        store.validate_constraint({"execution_constraint": f"claude-model:{name}"})
    with pytest.raises(ValueError, match="UNSUPPORTED_EXECUTION_CONSTRAINT"):
        store.validate_constraint({"execution_constraint": "other-kind:sonnet"})


def test_explicit_rename_table_supports_a_retired_alias(monkeypatch):
    assert store.resolve_alias("mystery") is None
    monkeypatch.setitem(store.ALIAS_RENAMES, "mystery", "opus")
    assert store.resolve_alias("MYSTERY") == "opus"


def test_a_frozen_renamed_alias_keeps_dispatching(task):
    _freeze(task, "claude-model:claude-sonnet-4-5")
    allowed = _dispatch(task, "tu1")["hookSpecificOutput"]["updatedInput"]
    assert allowed["model"] == "sonnet"  # the frozen intent resolved to today's alias
    assert "updatedInput" in _dispatch(task, "tu2", model="sonnet")["hookSpecificOutput"]
    assert "updatedInput" in _dispatch(task, "tu3", model="claude-sonnet-4-5")["hookSpecificOutput"]
    refused = _dispatch(task, "tu4", model="opus")
    assert _decision(refused) == "deny" and "per-spawn overrides are refused" in _reason(refused)
    assert run_cli("status")[1]["Authority"]["execution_constraint"] == "claude-model:claude-sonnet-4-5"  # frozen text untouched


def test_an_unknown_frozen_alias_refuses_dispatch_but_not_close_or_dispose(task):
    handoff = _running_implementer(task)
    _freeze(task, "claude-model:no-such-family")
    denied = _dispatch(task, "tu2")
    assert _decision(denied) == "deny" and "UNSUPPORTED_EXECUTION_CONSTRAINT" in _reason(denied)
    assert "`dispose`" in _reason(denied) and "no-such-family" in _reason(denied)
    assert len(store.read_lifecycle(task["root"], task["task_id"])["dispatches"]) == 1  # no reservation was made

    assert run_cli("dispose", "--handoff-id", handoff, "--reason", "constraint unresolved")[0] == 0
    assert _decision(run_hook("PreToolUse", _write(task, **IMPL))) == "deny"  # still fenced
    revision = run_cli("status")[1]["Task"]["revision"]
    code, closed = run_cli("close", "--base-revision", str(revision))
    assert code == 0, closed
    assert run_cli("status", "--task-id", task["task_id"])[1]["Task"]["status"] == "DONE"


def test_an_unknown_frozen_alias_still_allows_detach(task):
    _freeze(task, "claude-model:no-such-family")
    assert run_cli("detach", "--reason", "x")[0] == 0


# ------------------------------------------------- anchors: an older shape loads without repair

def test_an_older_anchor_shape_loads_and_keeps_working(task):
    handoff = _running_implementer(task)
    # An earlier plugin wrote no adapter fields and no explicit execution mode.
    _freeze(task, None, drop=("claude_session_sha256", "host_actor_assurance", "execution_mode"))
    record = json.loads(_anchor(task).read_text(encoding="utf-8"))
    assert "claude_session_sha256" not in record and "host_actor_assurance" not in record
    assert "execution_mode" not in record["contract"]

    status = run_cli("status")[1]
    assert status["Authority"]["execution_mode"] == "delegated" and status["Authority"]["status"] == "ACTIVE"
    assert run_cli("status", "--view", "full")[0] == 0
    assert "updatedInput" in _dispatch(task, "tu2", role="reviewer")["hookSpecificOutput"]
    assert run_hook("PreToolUse", _write(task, **IMPL)) is None
    assert run_cli("observe", "--text", "edited app.py", "--source", "app.py:1", agent="impl")[0] == 0
    assert run_cli("dispose", "--handoff-id", handoff, "--reason", "x")[0] == 0
    attached = run_cli("attach", "--task-id", task["task_id"], "--reason", "resume")
    assert attached[0] == 0, attached


def test_an_older_anchor_still_honors_single_agent_mode_when_recorded(task):
    status = run_cli("status")[1]
    code, switched = run_cli("mode", "--mode", "single-agent", "--human-instruction", "do it yourself",
                             "--base-revision", str(status["Task"]["revision"]), "--expected-authority-sha256", status["Authority"]["sha256"])
    assert code == 0, switched
    _freeze(task, None, drop=("claude_session_sha256", "host_actor_assurance"))
    assert _decision(_dispatch(task)) == "deny"  # a tolerant read never turns a recorded restriction into a default
