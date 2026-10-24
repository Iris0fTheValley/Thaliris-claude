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


# ---------------------------------------------- dialect-aware classification (PowerShell is not POSIX)

def _run(text: str) -> str:
    return f'python "{RUNNER}" {text}'


# Each of these is one thing to the POSIX reader and something else to PowerShell (which has no backslash escape).
POWERSHELL_AMBIGUOUS = [
    (r'observe --text "abc\" ; Write-Output x"', "escape"),          # the reviewed repro: `\"` ends the string in PowerShell
    (r"observe --text a\; Write-Output x", "escape"),                 # `\;` is a backslash and a statement separator
    (r"observe --text a\| Write-Output x", "escape"),
    (r"""observe --text \'"x' ; Write-Output y; " """, "escape"),      # PowerShell opens a single quote the POSIX reader skips
    (r'observe --text "q\\" ; Write-Output x ; "b"', "escape"),       # `\\"`: still a backslash before a closing quote
    (r'observe --text "\$(Write-Output x)"', "expansion"),            # `\$(` is literal to POSIX, evaluated by PowerShell
    ('observe --text "a \u201c ; Write-Output x ; \u201d b"', "smart_quote"),   # curly quotes end a double-quoted string
    ("observe --text 'it\u2019s' ; Write-Output x ; ''", "smart_quote"),
    ("observe --text x --% y", "stop_parsing"),
]


@pytest.mark.parametrize("text, problem", POWERSHELL_AMBIGUOUS)
def test_powershell_ambiguity_is_never_direct(text, problem):
    found = guards.runner_invocation(_run(text), RUNNER, powershell=True)
    assert found["direct"] is False and problem in found["problems"], found


def test_the_reviewed_powershell_repro_was_direct_under_the_posix_reader():
    command = _run(r'observe --text "abc\" ; Write-Output x"')
    assert guards.runner_invocation(command, RUNNER)["direct"] is True  # POSIX balances the quotes (Bash tool: harmless)
    assert guards.runner_invocation(command, RUNNER, powershell=True)["direct"] is False


@pytest.mark.parametrize("powershell", [False, True])
@pytest.mark.parametrize("text", [
    'observe --text "中文说明：这是一段普通的引号文本，包含 # 与 | 和 100%"',
    'judge --base-revision 3 --judgment "R1|satisfied|O1|a | b"',
    'judge --base-revision 3 --judgment "R1|satisfied|O1|中文 | 理由" --judgment "R2|unknown|O1|b"',
    r"observe --text 'literal $name, ${x}, $(y), `z` and C:\dir\'",
    "observe --text 'a \u201cquoted\u201d phrase'",         # curly double quotes inside single quotes are plain data
    'observe --text "it\u2019s ok, \u2018x\u2019"',        # curly single quotes inside double quotes are plain data
    'observe --text "price $ 5 and a trailing $"',         # `$` before a space or at the end is a literal dollar
    r'observe --text "path C:\dir\file and \n"',           # backslashes that precede no quote are literal
    "observe --text 'it''s fine'",                         # PowerShell's doubled apostrophe (POSIX reads adjacent strings)
])
def test_ordinary_quoted_text_stays_direct_on_both_shells(text, powershell):
    found = guards.runner_invocation(_run(text), RUNNER, powershell=powershell)
    assert found["direct"] is True, found


@pytest.mark.parametrize("fragment", ['"cost $5"', '"hello $name"', '"a ${HOME} b"', '"exit $? now"', '"x_$_y"'])
@pytest.mark.parametrize("powershell", [False, True])
def test_dollar_variables_in_double_quotes_are_refused_on_both_shells(fragment, powershell):
    found = guards.runner_invocation(_run(f"observe --text {fragment}"), RUNNER, powershell=powershell)
    assert found["direct"] is False and "variable" in found["problems"], found


def test_powershell_only_automatic_variables_are_refused_there():
    command = _run('observe --text "pid $$ and $^"')
    assert "variable" in guards.runner_invocation(command, RUNNER, powershell=True)["problems"]
    assert guards.runner_invocation(command, RUNNER)["direct"] is True  # not variables in the Bash rule set


@pytest.mark.parametrize("powershell", [False, True])
def test_single_quoted_dollars_keep_working(powershell):
    assert guards.runner_invocation(_run("observe --text 'cost $5 and $name'"), RUNNER, powershell=powershell)["direct"] is True


def test_bash_escaped_dollar_is_literal():
    assert guards.runner_invocation(_run(r'observe --text "cost \$5"'), RUNNER)["direct"] is True


def test_powershell_ambiguity_counts_as_a_write_for_non_runner_commands():
    command = r'Write-Output "a\" ; Set-Content f x ; \"b"'
    assert not guards.obvious_write(command)  # the POSIX reading sees one harmless string (the gap this closes)
    assert guards.obvious_write(command, powershell=True)
    assert guards.direct_core_call(r'Write-Output "a\" ; thaliris-core task-close ; \"b"', powershell=True)
    assert not guards.obvious_write("Get-Content app.py -Tail 5", powershell=True)
    assert not guards.obvious_write("git log --oneline -5", powershell=True)
