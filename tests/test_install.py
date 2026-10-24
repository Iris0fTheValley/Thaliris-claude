"""Install, update and session-start behavior: what a fresh user gets without setup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import thaliris

from thaliris_claude import __version__, hooks, store

from .conftest import SESSION, run_cli, run_hook

ROOT = Path(__file__).resolve().parents[1]
SH = shutil.which("sh")


def test_tests_and_runtime_use_the_vendored_core():
    assert Path(thaliris.__file__).resolve().is_relative_to(ROOT / "vendor")


def test_vendored_core_matches_its_lock_and_history():
    """The lock is checked against Git objects when the Core checkout is present (H046)."""
    source = Path(os.environ.get("THALIRIS_CORE_SOURCE") or ROOT.parents[1] / "core" / "Thaliris")
    args = [sys.executable, str(ROOT / "scripts" / "vendor_core.py"), "--check"]
    if (source / ".git").exists():
        args += ["--source", str(source)]
    result = json.loads(subprocess.run(args, capture_output=True, check=False).stdout)
    assert result == {"ok": True, "problems": []}


def test_versions_agree_across_manifest_package_and_runtime():
    manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    assert f'version = "{__version__}"' in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert [plugin["name"] for plugin in market["plugins"]] == [manifest["name"]]


def test_user_config_offers_exactly_the_core_modes():
    from thaliris.authority import MODES
    option = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["userConfig"]["default_execution_mode"]
    assert tuple(option["options"]) == MODES and option["default"] in MODES
    selection = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["userConfig"]["model_selection"]
    assert selection["default"] == "controller" and set(selection["options"]) == {"controller", "role-defaults"}


def test_hooks_use_the_sh_entry_convention():
    for groups in json.loads((ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert hook["command"].startswith('sh "${CLAUDE_PLUGIN_ROOT}/bin/thaliris-hook" ')


def test_session_start_reports_the_users_standing_mode(task, monkeypatch):
    def context():
        payload = {"session_id": SESSION, "transcript_path": task["transcript"], "cwd": str(task["root"]),
                   "hook_event_name": "SessionStart", "source": "startup"}
        return run_hook("SessionStart", payload)["hookSpecificOutput"]["additionalContext"]

    monkeypatch.delenv(hooks.MODE_SETTING, raising=False)
    unset = context()
    monkeypatch.setenv(hooks.MODE_SETTING, "delegated")
    assert context() != unset  # a standing mode changes what the Controller is told
    monkeypatch.setenv(hooks.MODE_SETTING, "not-a-mode")
    assert context() == unset  # an unknown value is not a choice


def test_runner_command_names_the_discovered_interpreter():
    command = hooks.runner_command()
    assert command.startswith(f'"{Path(sys.executable).as_posix()}"')
    from thaliris_claude import guards
    assert guards.runner_invocation(f"{command} status", hooks.runner_path())["direct"] is True
    assert guards.runner_invocation(f"& {command} status", hooks.runner_path())["direct"] is True


def test_newer_lifecycle_is_never_reinterpreted(task):
    path = store.lifecycle_path(task["root"], task["task_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 2, "task_id": task["task_id"], "dispatches": []}), encoding="utf-8")
    code, result = run_cli("status")
    assert code == 2 and "CLAUDE_LIFECYCLE_VERSION_UNSUPPORTED" in result["error"]


def _launcher(args, home, payload=None, **env):
    environment = {**os.environ, "THALIRIS_CLAUDE_HOME": str(home), **env}
    environment.pop("THALIRIS_CLAUDE_PYTHON", None) if "THALIRIS_CLAUDE_PYTHON" not in env else None
    return subprocess.run(args, input=json.dumps(payload or {}).encode(), capture_output=True, env=environment, check=True)


def test_launcher_discovers_and_caches_a_runtime(tmp_path):
    """First run discovers a Python >= 3.11 and caches it; no venv or pip is involved."""
    home = tmp_path / "home"
    old = shutil.which("python") or sys.executable
    out = _launcher([old, str(ROOT / "bin" / "thaliris.py"), "version"], home)
    assert json.loads(out.stdout)["core_version"] == thaliris.__version__
    cached = (home / "runtime" / "python").read_text(encoding="utf-8").strip() if (home / "runtime" / "python").exists() else None
    if sys.version_info >= (3, 11) and Path(old).resolve() == Path(sys.executable).resolve():
        assert cached is None  # already suitable: ran in-process, nothing to cache
    else:
        assert cached and Path(cached).is_file()


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh (Git Bash on Windows)")
def test_sh_hook_entry_uses_the_cached_runtime(task, tmp_path):
    home = Path(os.environ["THALIRIS_CLAUDE_HOME"])
    (home / "runtime").mkdir(parents=True, exist_ok=True)
    (home / "runtime" / "python").write_text(Path(sys.executable).as_posix() + "\n", encoding="utf-8")
    payload = {"session_id": SESSION, "transcript_path": task["transcript"], "cwd": str(task["root"]),
               "hook_event_name": "SessionStart", "source": "startup"}
    out = _launcher([SH, str(ROOT / "bin" / "thaliris-hook"), "SessionStart"], home, payload)
    text = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
    assert task["task_id"] in text and f"thaliris-claude {__version__}" in text


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh (Git Bash on Windows)")
def test_sh_hook_entry_without_any_python_is_transparent(tmp_path):
    home = tmp_path / "home"
    empty = tmp_path / "empty-path"
    empty.mkdir()
    sh_dir = str(Path(SH).parent)
    out = _launcher([SH, str(ROOT / "bin" / "thaliris-hook"), "PreToolUse"], home,
                    {"tool_name": "Bash", "tool_input": {"command": "ls"}}, PATH=os.pathsep.join([str(empty), sh_dir]))
    assert out.stdout == b""


def _runner_call(task, subcommand, **extra):
    return {"session_id": SESSION, "transcript_path": task["transcript"], "cwd": str(task["root"]), "hook_event_name": "PreToolUse",
            "tool_name": "PowerShell", "tool_use_id": "p1", "tool_input": {"command": f"& {hooks.runner_command()} {subcommand}"}, **extra}


def test_runner_is_approved_so_managed_work_does_not_stall(task, monkeypatch):
    """Live probe: an unapproved runner made the Controller stop and ask on its first step."""
    monkeypatch.delenv(hooks.APPROVE_SETTING, raising=False)
    output = run_hook("PreToolUse", _runner_call(task, "status"))["hookSpecificOutput"]
    assert output["permissionDecision"] == "allow" and output["updatedInput"]["command"].endswith(f"--native-session {SESSION}")
    # Plan mode keeps its promise: read-only runner calls only.
    assert run_hook("PreToolUse", _runner_call(task, "status", permission_mode="plan"))["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert "permissionDecision" not in run_hook("PreToolUse", _runner_call(task, "close --base-revision 2", permission_mode="plan"))["hookSpecificOutput"]
    # Anything chained to the runner is never approved; it is refused.
    chained = _runner_call(task, "status; rm -rf x")
    assert run_hook("PreToolUse", chained)["hookSpecificOutput"]["permissionDecision"] == "deny"
    monkeypatch.setenv(hooks.APPROVE_SETTING, "false")
    assert "permissionDecision" not in run_hook("PreToolUse", _runner_call(task, "status"))["hookSpecificOutput"]


def _through_sh(home, payload, *flags):
    environment = {**os.environ, "THALIRIS_CLAUDE_HOME": str(home)}
    (Path(home) / "runtime").mkdir(parents=True, exist_ok=True)
    (Path(home) / "runtime" / "python").write_text(Path(sys.executable).as_posix() + "\n", encoding="utf-8")
    return subprocess.run([SH, str(ROOT / "bin" / "thaliris-hook"), "PreToolUse", *flags], input=json.dumps(payload).encode(),
                          capture_output=True, env=environment, check=True).stdout


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh (Git Bash on Windows)")
def test_sh_filter_never_skips_a_call_the_guards_would_decide(task):
    """The fast path may only skip calls for which the full Python hook returns nothing."""
    home = os.environ["THALIRIS_CLAUDE_HOME"]
    base = {"session_id": SESSION, "transcript_path": task["transcript"], "cwd": str(task["root"]), "hook_event_name": "PreToolUse", "tool_use_id": "f"}
    ledger = str(task["root"] / ".context" / "tasks" / task["task_id"] / "state.json")
    cases = [
        ({"tool_name": "Bash", "tool_input": {"command": "git status"}}, None),  # skipped by the filter
        ({"tool_name": "Bash", "tool_input": {"command": "echo {} > .context/config.json"}, "agent_id": "g", "agent_type": "Explore"}, "deny"),
        ({"tool_name": "Bash", "tool_input": {"command": "echo x > a.txt"}, "agent_id": "r", "agent_type": "thaliris:reviewer"}, "deny"),
        ({"tool_name": "Write", "tool_input": {"file_path": ledger.upper(), "content": "{}"}}, "deny"),  # ledger write
        ({"tool_name": "Write", "tool_input": {"file_path": str(task["root"] / ".project-identity" / "IDENTITY.md"), "content": "x"},
          "agent_id": "g", "agent_type": "Explore"}, "deny"),
        ({"tool_name": "Bash", "tool_input": {"command": f"{hooks.runner_command()} status"}}, "allow"),
    ]
    for extra, expected in cases:
        payload = {**base, **extra}
        python = run_hook("PreToolUse", payload)
        python_decision = (python or {}).get("hookSpecificOutput", {}).get("permissionDecision")
        filtered = _through_sh(home, payload, "--filter")
        if python is not None:
            assert filtered, f"filter skipped a call the guards decide: {extra}"
        assert python_decision == expected, extra
        if filtered:
            assert json.loads(filtered)["hookSpecificOutput"].get("permissionDecision") == expected


@pytest.mark.skipif(SH is None, reason="needs a POSIX sh (Git Bash on Windows)")
def test_turn_hook_is_free_without_any_task(tmp_path):
    home = tmp_path / "home"
    environment = {**os.environ, "THALIRIS_CLAUDE_HOME": str(home), "THALIRIS_CLAUDE_PYTHON": "/nonexistent/python"}
    out = subprocess.run([SH, str(ROOT / "bin" / "thaliris-hook"), "UserPromptSubmit"], input=b"{}", capture_output=True, env=environment, check=True)
    assert out.stdout == b"" and out.stderr == b""
