from __future__ import annotations

from pathlib import Path

import pytest

from thaliris_claude import guards

RUNNER = "/opt/plugins/Thaliris-claude/bin/thaliris.py"


@pytest.mark.parametrize("command, subcommand", [
    (f'python "{RUNNER}" status', "status"),
    (f"& python '{RUNNER}' observe --json '{{\"id\":\"O1\"}}'", "observe"),
    (f'"{RUNNER}" judge --base-revision 3 --input j.json', "judge"),
])
def test_direct_runner_invocations(command, subcommand):
    found = guards.runner_invocation(command, RUNNER)
    assert found == {"direct": True, "subcommand": subcommand, "forged_identity": False}


@pytest.mark.parametrize("command", [
    f'python "{RUNNER}" status && echo done',
    f'python "{RUNNER}" status | head',
    f'python "{RUNNER}" observe --json "$(cat o.json)"',
    f'X=1; python "{RUNNER}" status',
    f'python "{RUNNER}" status > out.json',
])
def test_compound_runner_invocations_are_not_direct(command):
    assert guards.runner_invocation(command, RUNNER)["direct"] is False


def test_backticks_inside_single_quoted_json_are_data():
    command = f"python \"{RUNNER}\" observe --json '{{\"id\":\"O1\",\"text\":\"`main` returns $(x)\"}}'"
    assert guards.runner_invocation(command, RUNNER)["direct"] is True


def test_forged_identity_and_unrelated_commands():
    assert guards.runner_invocation(f'python "{RUNNER}" status --native-session abc', RUNNER)["forged_identity"]
    assert guards.runner_invocation("python other.py status", RUNNER) is None


@pytest.mark.parametrize("command", [
    "echo hi > app.py", "sed -i s/a/b/ app.py", "git commit -m x", "git -C repo checkout main", "rm -rf build",
    "Set-Content app.py 'x'", "npm install left-pad", "cat a | tee b", "git branch -D old", "echo $(rm x)",
])
def test_obvious_writes(command):
    assert guards.obvious_write(command)


@pytest.mark.parametrize("command", [
    "git status", "git diff HEAD~1", "pytest -q 2>&1", "rg pattern src > /dev/null", "ls -la", "cat app.py",
    "python -m pytest tests 2>/dev/null", "git log --oneline -5", "Get-Content app.py",
])
def test_reads_are_not_writes(command):
    assert not guards.obvious_write(command)


def test_direct_core_calls():
    assert guards.direct_core_call("thaliris-core --task-id x task-close --base-revision 2")
    assert guards.direct_core_call("python -m thaliris_claude judge")
    assert not guards.direct_core_call("python -m pytest")


def test_protected_targets(tmp_path):
    root = tmp_path
    assert guards.protected_target(root, ".context/config.json", controller=False)
    assert guards.protected_target(root, str(root / ".claude" / "settings.json"), controller=False)
    assert not guards.protected_target(root, "src/app.py", controller=False)
    assert guards.protected_target(root, ".context/tasks/x/state.json", controller=True)
    assert not guards.protected_target(root, ".context/config.json", controller=True)
    assert guards.protected_target(root, str(Path.home() / ".thaliris" / "claude" / "x.json"), controller=False)


def test_equivalent_runner_paths_are_recognized():
    """Live probe: a Controller spelled the runner path with `..` segments and lost its identity."""
    spelled = "/opt/plugins/x/../Thaliris-claude/bin/thaliris.py"
    assert guards.runner_invocation(f'python "{spelled}" status', RUNNER)["direct"] is True
