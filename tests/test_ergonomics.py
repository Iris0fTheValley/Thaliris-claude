"""Controller ergonomics: no silent identity miss, flag forms instead of JSON files, tolerant input, visible revision."""
from __future__ import annotations

import codecs
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from thaliris_claude import guards, hooks

from .conftest import SESSION, run_cli, run_hook
from .test_flow import RUNNER, OBSERVATION, _base, _decision, _dispatch, _handoff, _reason, _result, _shell, _start, _stop

ROOT = Path(__file__).resolve().parents[1]
CHILD = {"agent_id": "agent-a", "agent_type": "thaliris:investigator"}


def _revision() -> str:
    return str(run_cli("status")[1]["Task"]["revision"])


def _hooked(task, command, **who):
    return run_hook("PreToolUse", _shell(task, command, **who))


# ------------------------------------------------------------ identity: no silent miss

@pytest.fixture
def cache_root(tmp_path, monkeypatch):
    """The hook runs from an installed plugin cache directory (version 0.4.0)."""
    root = tmp_path / "plugins" / "cache" / "thaliris" / "thaliris" / "0.4.0"
    monkeypatch.setenv("THALIRIS_CLAUDE_PLUGIN_ROOT", str(root))
    return root


def _quoted_runner() -> str:
    return hooks.runner_command()


def test_trailing_comment_is_denied_with_the_exact_form(task, cache_root):
    denied = _hooked(task, f"{_quoted_runner()} status  # check the ledger")
    assert _decision(denied) == "deny"
    reason = _reason(denied)
    assert "comment" in reason and hooks.runner_command() in reason


def test_leading_comment_and_extra_lines_are_denied(task):
    assert _decision(_hooked(task, f"# look first\n{RUNNER} status")) == "deny"
    denied = _hooked(task, f"{RUNNER} status\nrm -rf build")
    assert _decision(denied) == "deny" and "more than one line" in _reason(denied)


def test_quoted_hash_and_multiline_json_stay_direct(task):
    result = _hooked(task, f"{RUNNER} task-update --base-revision 1 --active-work 'fix #12\nthen test'")
    assert result["hookSpecificOutput"]["updatedInput"]["command"].endswith(f"--native-session {SESSION}")
    assert guards.runner_invocation(f'{RUNNER} status "$#"', hooks.runner_path())["direct"] is True


def test_unquoted_backslash_runner_path_is_denied(task, cache_root):
    raw = hooks.runner_path().replace("/", "\\")
    denied = _hooked(task, f"python {raw} status")
    assert _decision(denied) == "deny"
    assert "backslash" in _reason(denied) and hooks.runner_command() in _reason(denied)
    quoted = _hooked(task, f'python "{raw}" status')  # a quoted spelling is recognised and works
    assert quoted["hookSpecificOutput"]["updatedInput"]["command"].endswith(f"--native-session {SESSION}")


def test_other_plugin_version_path_is_denied(task, cache_root):
    old = (cache_root.parent / "0.3.9" / "bin" / "thaliris.py").as_posix()
    denied = _hooked(task, f'python "{old}" status')
    assert _decision(denied) == "deny"
    assert "another installed version" in _reason(denied) and hooks.runner_path() in _reason(denied)
    denied = _hooked(task, f'python "{old}" status', **CHILD)
    assert _decision(denied) == "deny"


def test_dev_checkout_runner_stays_transparent(task, cache_root, tmp_path):
    checkout = (tmp_path / "dev" / "Thaliris-claude" / "bin" / "thaliris.py").as_posix()
    assert _hooked(task, f'python "{checkout}" version') is None
    assert _hooked(task, "python bin/thaliris.py version") is None
    assert _hooked(task, "python -m pytest -q tests") is None
    assert _hooked(task, f'python "{checkout}" version', **CHILD) is None


def test_runner_misses_classification(tmp_path):
    root = (tmp_path / "plugins" / "cache" / "thaliris" / "thaliris" / "0.4.0").as_posix()
    runner = f"{root}/bin/thaliris.py"
    sibling = root.replace("0.4.0", "0.4.1") + "/bin/thaliris.py"
    assert guards.runner_misses(f'python "{sibling}" x', runner) == [{"path": sibling, "kind": "version"}]
    assert guards.runner_misses(f'python "{runner}" x', runner) == []
    assert guards.runner_misses(f"python {runner.replace('/', chr(92))} x", runner)[0]["kind"] == "backslash"
    assert guards.runner_misses("python /elsewhere/bin/thaliris.py x", runner) == []
    # Outside a plugin cache, a sibling directory is not "another version".
    plain = f"{tmp_path.as_posix()}/work/Thaliris-claude/bin/thaliris.py"
    assert guards.runner_misses(f"python {tmp_path.as_posix()}/work/other/bin/thaliris.py x", plain) == []


def test_shell_layout_sees_what_shlex_hides():
    assert guards.shell_layout("run x # note") == {"comment"}
    assert guards.shell_layout("run x;#note") == {"comment"}
    assert guards.shell_layout("run 'a # b' \"c # d\" e#f $#") == set()
    assert guards.shell_layout("run x\ny") == {"newline"}
    assert guards.shell_layout("run x\n") == set()  # trailing whitespace is stripped
    assert guards.shell_layout("run 'x\ny'") == set()
    assert guards.shell_layout("run x \\\ny") == set()  # line continuation


def test_runner_identity_error_names_causes_and_the_exact_form(home, repo, monkeypatch):
    monkeypatch.chdir(repo)
    code, error = run_cli("status", session=None)
    text = error["error"]
    assert code == 2 and text.startswith("NATIVE_IDENTITY_UNKNOWN")
    assert "comment" in text and "backslashes" in text and "plugin version" in text
    assert f"{hooks.runner_command()} status <arguments>" in text


# ------------------------------------------------------------ flag forms

def test_register_flag_form(task):
    code, registered = run_cli("register", "--base-revision", _revision(), "--requirement", "R2=login-redirect", "--requirement", "R3=api-limit")
    assert code == 0, registered
    state = run_cli("status")[1]
    assert registered["revision"] == state["Task"]["revision"]
    assert run_cli("register", "--base-revision", _revision(), "--requirement", "R4")[0] == 2


def test_judge_flag_form_with_chinese_and_default_ids(task):
    _dispatch(task)
    _start(task, "agent-a", "thaliris:investigator", "tu1")
    code, observed = run_cli("observe", "--json", json.dumps(OBSERVATION), agent="agent-a")
    assert code == 0, observed
    observation = observed["observation_id"]
    base = ["judge", "--requirement", "R1", "--candidate", "C1", "--outcome", "repair", "--observations", "O1",
            "--reasoning", "问候语仍然是英文，需要修复"]
    code, judged = run_cli(*base, "--base-revision", _revision())
    assert code == 0, judged
    assert run_cli("get", "J1")[1]["object"]["observation_refs"] == [observation]  # the unique suffix O1 was resolved
    assert run_cli("get", "J1")[1]["object"]["reasoning"] == "问候语仍然是英文，需要修复"
    code, again = run_cli(*base[:-2], "--reasoning", "现已修复", "--judgment-refs", "J1", "--outcome", "satisfied",
                          "--id", "FIX", "--base-revision", _revision())
    assert code == 0, again
    assert run_cli("get", "FIX")[1]["object"]["judgment_refs"] == ["J1"]
    code, third = run_cli(*base, "--base-revision", _revision())
    assert code == 0, third
    assert run_cli("get", "J2")[1]["object"]["id"] == "J2"


def test_judge_flag_form_is_validated_like_json(task):
    refused = run_cli("judge", "--requirement", "R1", "--candidate", "C1", "--outcome", "satisfied", "--observations", "NOPE",
                      "--reasoning", "x", "--base-revision", _revision())
    assert refused[0] == 2
    code, incomplete = run_cli("judge", "--requirement", "R1", "--base-revision", _revision())
    assert code == 2 and "FLAG_FORM_INCOMPLETE" in incomplete["error"] and "--outcome" in incomplete["error"]
    code, mixed = run_cli("judge", "--requirement", "R1", "--json", "{}", "--base-revision", _revision())
    assert code == 2 and "PAYLOAD_SOURCE_CONFLICT" in mixed["error"]
    code, empty = run_cli("judge", "--base-revision", _revision())
    assert code == 2 and "PAYLOAD_REQUIRED" in empty["error"]
    code, bad = run_cli("judge", "--requirement", "R1", "--candidate", "C1", "--outcome", "maybe", "--observations", "O1",
                        "--reasoning", "x", "--base-revision", _revision())
    assert code == 2 and "maybe" in bad["error"]


def test_child_observe_flag_form_assigns_unique_ids_and_keeps_locators(task):
    _dispatch(task)
    _start(task, "agent-a", "thaliris:investigator", "tu1")
    code, first = run_cli("observe", "--text", "问候语在 app.py:1 输出", "--source", "app.py:1", "--source", "https://example.test/doc?a=1&b=2",
                          agent="agent-a")
    assert code == 0, first
    handoff = first["handoff_id"]
    assert first["observation_id"] == f"{handoff}.O1"
    code, second = run_cli("observe", "--text", "第二条", "--source", "app.py:2", agent="agent-a")
    assert code == 0, second
    assert second["observation_id"] == f"{handoff}.O2"
    stored = run_cli("get", f"{handoff}.O1")[1]["object"]
    assert stored["text"] == "问候语在 app.py:1 输出" and stored["requirement_refs"] == ["R1"]
    sources = {item: run_cli("get", item)[1]["object"] for item in stored["source_refs"]}
    assert [value["locator"] for value in sources.values()] == ["app.py:1", "https://example.test/doc?a=1&b=2"]
    assert [value["kind"] for value in sources.values()] == ["local", "url"]
    assert run_cli("observe", "--text", "no source", agent="agent-a")[0] == 2
    assert run_cli("observe", "--text", "x", "--source", "a", "--json", json.dumps(OBSERVATION), agent="agent-a")[0] == 2


def test_controller_observe_import_flag_form(task):
    handoff = _handoff(_dispatch(task))
    code, imported = run_cli("observe", "--handoff-id", handoff, "--text", "导入的历史结果", "--source", "child report")
    assert code == 0, imported
    assert imported["observation_id"] == f"{handoff}.O1"
    code, next_one = run_cli("observe", "--handoff-id", handoff, "--text", "again", "--source", "child report 2", "--id", "X1")
    assert code == 0 and next_one["observation_id"] == "X1"


def test_task_update_flag_form(task):
    revision = _revision()
    code, updated = run_cli("task-update", "--base-revision", revision, "--record", "decision:保留解析器，修复调用方",
                            "--record", "D9=fact:app.py 只打印 hi", "--record", "open-item:确认国际化", "--milestone", "m-flags",
                            "--active-work", "写测试", "--active-work", "跑回归", "--pending", "审查结果")
    assert code == 0, updated
    context = updated["Task Context"]
    assert context["milestone"] == "m-flags" and context["Active Work"] == ["写测试", "跑回归"]
    assert context["Pending Results"] == ["审查结果"]
    by_id = {item["id"]: item for item in context["Current Records"]}
    assert by_id["D1"]["kind"] == "decision" and by_id["D1"]["text"] == "保留解析器，修复调用方"
    assert by_id["D9"]["kind"] == "fact" and by_id["OI1"]["kind"] == "open-item"
    # Only fields given change: a record-only update leaves Active Work as it was; ids stay unique.
    code, more = run_cli("task-update", "--base-revision", _revision(), "--record", "decision:换方案", "--supersedes", "D1")
    assert code == 0, more
    assert more["Task Context"]["Active Work"] == ["写测试", "跑回归"]
    assert {item["id"] for item in more["Task Context"]["Current Records"]} == {"D2", "D9", "OI1"}  # D1 is superseded
    code, replaced = run_cli("task-update", "--base-revision", _revision(), "--active-work", "只剩一项", "--clear-pending")
    assert code == 0 and replaced["Task Context"]["Active Work"] == ["只剩一项"] and replaced["Task Context"]["Pending Results"] == []
    code, cleared = run_cli("task-update", "--base-revision", _revision(), "--clear-active-work")
    assert code == 0 and cleared["Task Context"]["Active Work"] == []


def test_task_update_flag_form_refusals(task):
    base = ["task-update", "--base-revision", _revision()]
    assert "FLAG_FORM_EMPTY" not in run_cli(*base, "--milestone", "m")[1].get("error", "")
    base = ["task-update", "--base-revision", _revision()]
    for args, expected in (
            (["--record", "no kind here"], "INVALID_RECORD"),
            (["--record", "decision:one", "--record", "decision:two", "--supersedes", "D0"], "NEWID=OLD"),
            (["--supersedes", "D1"], "INVALID_SUPERSEDES"),
            (["--active-work", "a", "--clear-active-work"], "CONFLICTING_FLAGS"),
            (["--record", "D1=fact:x", "--record", "D1=fact:y"], "duplicate record id"),
            (["--record", "decision:x", "--json", "{}"], "PAYLOAD_SOURCE_CONFLICT")):
        code, refused = run_cli(*base, *args)
        assert code == 2 and expected in refused["error"], (args, refused)
    code, nothing = run_cli("task-update", "--base-revision", _revision())
    assert code == 2 and "PAYLOAD_REQUIRED" in nothing["error"]
    # Unknown superseded record: Core's own validation still applies.
    assert run_cli(*base, "--record", "decision:x", "--supersedes", "GHOST")[0] == 2


def test_task_update_supersedes_by_explicit_new_id(task):
    assert run_cli("task-update", "--base-revision", _revision(), "--record", "D1=decision:a")[0] == 0
    code, done = run_cli("task-update", "--base-revision", _revision(), "--record", "D2=decision:b", "--record", "fact:c",
                         "--supersedes", "D2=D1")
    assert code == 0, done
    ids = {item["id"] for item in done["Task Context"]["Current Records"]}
    assert ids == {"D2", "F1"}


def test_task_start_flag_form(home, repo, monkeypatch):
    monkeypatch.chdir(repo)
    flags = ["--human-instruction", "修复问候语，别动别的", "--boundary", "只改 app.py", "--invariants", "输出保持 ASCII",
             "--acceptance", "运行后打印 hello", "--execution-mode", "controller-direct"]
    code, started = run_cli("task-start", "修复问候语", *flags, "--execution-constraint", "claude-model:sonnet")
    assert code == 0, started
    authority = run_cli("status")[1]["Authority"]
    assert authority["execution_mode"] == "controller-direct" and authority["execution_constraint"] == "claude-model:sonnet"


def test_task_start_flag_form_refusals(home, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    contract = tmp_path / "c.json"
    contract.write_text("{}", encoding="utf-8")
    code, mixed = run_cli("task-start", "g", "--contract", str(contract), "--boundary", "b")
    assert code == 2 and "PAYLOAD_SOURCE_CONFLICT" in mixed["error"]
    code, partial = run_cli("task-start", "g", "--boundary", "b")
    assert code == 2 and "FLAG_FORM_INCOMPLETE" in partial["error"] and "--execution-mode" in partial["error"]
    code, nothing = run_cli("task-start", "g")
    assert code == 2 and "CONTRACT_REQUIRED" in nothing["error"]
    inline = run_cli("task-start", "g", "--contract", json.dumps({"boundary": "x"}))
    assert inline[0] == 2 and "CONTRACT_FILE_PATH_REQUIRED" in inline[1]["error"] and "--human-instruction" in inline[1]["error"]
    code, bad = run_cli("task-start", "g", "--human-instruction", "h", "--boundary", "b", "--invariants", "i", "--acceptance", "a",
                        "--execution-mode", "delegated", "--execution-constraint", "claude-model:nope")
    assert code == 2 and "UNSUPPORTED_EXECUTION_CONSTRAINT" in bad["error"]


def test_flags_survive_a_legacy_console_code_page(task, tmp_path):
    """Chinese flags through the real launcher with a cp1252 stdout: the runner still reports and records them."""
    environment = {**os.environ, "THALIRIS_CLAUDE_HOME": os.environ["THALIRIS_CLAUDE_HOME"], "PYTHONIOENCODING": "cp1252"}
    for name in ("PYTHONUTF8", "THALIRIS_CLAUDE_PYTHON"):
        environment.pop(name, None)
    out = subprocess.run([sys.executable, str(ROOT / "bin" / "thaliris.py"), "task-update", "--base-revision", _revision(),
                          "--record", "decision:选择方案甲", "--native-session", SESSION], capture_output=True, env=environment,
                         cwd=task["root"], check=False)
    assert out.returncode == 0, out.stderr
    assert "选择方案甲" in json.loads(out.stdout.decode("utf-8"))["Task Context"]["Current Records"][0]["text"]


# ------------------------------------------------------------ tolerant --input

def _write(path: Path, payload: dict, encoding: str) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    if encoding == "utf-8-sig":
        data = codecs.BOM_UTF8 + text.encode("utf-8")
    elif encoding == "utf-16":
        data = text.encode("utf-16")  # includes the BOM, as Windows PowerShell redirection writes it
    else:
        data = text.encode(encoding)
    path.write_bytes(data)
    return str(path)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
def test_input_files_accept_utf8_bom_and_utf16(task, tmp_path, encoding):
    path = _write(tmp_path / f"p-{encoding}.json", {"requirements": [{"id": "R9", "workchain_ref": "i18n"}]}, encoding)
    code, registered = run_cli("register", "--base-revision", _revision(), "--input", path)
    assert code == 0, registered
    assert run_cli("get", "R9")[1]["object"]["workchain_ref"] == "i18n"


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
def test_contract_files_accept_bom_and_utf16(home, repo, tmp_path, monkeypatch, encoding):
    monkeypatch.chdir(repo)
    path = _write(tmp_path / "contract.json", {"human_instruction": "修复问候语", "boundary": "app.py", "invariants": "ASCII",
                                                "acceptance": "hello", "execution_mode": "delegated"}, encoding)
    code, started = run_cli("task-start", "Fix", "--contract", path)
    assert code == 0, started


def test_undecodable_input_names_the_expected_encodings(task, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_bytes('{"requirements":[{"id":"R9","workchain_ref":"国际化"}]}'.encode("gbk"))
    code, refused = run_cli("register", "--base-revision", _revision(), "--input", str(bad))
    assert code == 2 and "INPUT_ENCODING" in refused["error"] and "UTF-16 with BOM" in refused["error"]
    bad.write_bytes(codecs.BOM_UTF32_LE + b"{}\x00\x00\x00")
    assert "INPUT_ENCODING" in run_cli("register", "--base-revision", _revision(), "--input", str(bad))[1]["error"]


# ------------------------------------------------------------ revision visibility

def test_dispatch_ack_and_evidence_packet_state_the_task_revision(task):
    handoff = _handoff(_dispatch(task))
    _start(task, "agent-a", "thaliris:investigator", "tu1")
    assert run_cli("observe", "--text", "found it", "--source", "app.py:1", agent="agent-a")[0] == 0
    _stop(task, "agent-a", "thaliris:investigator")
    delivered = _result(task, "tu1", {"status": "completed", "agentId": "agent-a"})
    context = delivered["hookSpecificOutput"]["additionalContext"]
    revision = run_cli("status")[1]["Task"]["revision"]
    assert f"Task revision now {revision}" in context and "--base-revision" in context
    assert f"task revision now {revision}" in context  # the evidence packet header
    assert handoff in context
