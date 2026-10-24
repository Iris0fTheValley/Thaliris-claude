"""Output encoding on a legacy console code page.

A piped Windows stdout defaults to cp1252. Writing Chinese through it raised after the work was done: a hook then
exited non-zero with no output, so `--native-session` was never appended and the runner reported
NATIVE_IDENTITY_UNKNOWN; runner output with Chinese (identity text is often Chinese) crashed the same way.
These tests force the code page, so they fail the same way on every OS without the fix.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from thaliris_claude import hooks

from .conftest import SESSION, run_cli

ROOT = Path(__file__).resolve().parents[1]
GREETING = "修复问候语"


def _legacy_env(home) -> dict:
    environment = {**os.environ, "THALIRIS_CLAUDE_HOME": str(home), "PYTHONIOENCODING": "cp1252"}
    for name in ("PYTHONUTF8", "THALIRIS_CLAUDE_PYTHON"):
        environment.pop(name, None)
    return environment


def _pipe(args, home, payload=None, cwd=None):
    return subprocess.run([sys.executable, str(ROOT / "bin" / "thaliris.py"), *args], input=json.dumps(payload or {}).encode(),
                          capture_output=True, env=_legacy_env(home), cwd=cwd, check=False)


def test_hook_output_survives_a_legacy_code_page(task):
    home = Path(os.environ["THALIRIS_CLAUDE_HOME"])
    command = f"{hooks.runner_command()} task-update --base-revision 2 --json '{json.dumps({'active_work': [GREETING]}, ensure_ascii=False)}'"
    payload = {"session_id": SESSION, "transcript_path": task["transcript"], "cwd": str(task["root"]), "hook_event_name": "PreToolUse",
               "tool_name": "Bash", "tool_use_id": "c1", "tool_input": {"command": command}}
    out = _pipe(["hook", "PreToolUse"], home, payload)
    assert out.returncode == 0 and out.stderr == b"" and out.stdout
    assert out.stdout.isascii()  # valid for any decoder
    rewritten = json.loads(out.stdout)["hookSpecificOutput"]["updatedInput"]["command"]
    assert GREETING in rewritten and rewritten.endswith(f"--native-session {SESSION}")  # exact round trip

    # SessionStart: the Chinese task context reaches the Host intact.
    revision = run_cli("status")[1]["Task"]["revision"]
    code, updated = run_cli("task-update", "--base-revision", str(revision), "--json", json.dumps({"active_work": [GREETING]}))
    assert code == 0, updated
    start = {"session_id": SESSION, "transcript_path": task["transcript"], "cwd": str(task["root"]),
             "hook_event_name": "SessionStart", "source": "resume"}
    out = _pipe(["hook", "SessionStart"], home, start)
    assert out.returncode == 0 and out.stderr == b"" and out.stdout.isascii()
    assert GREETING in json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]


def test_runner_output_stays_readable_utf8_on_a_legacy_code_page(task, tmp_path):
    home = Path(os.environ["THALIRIS_CLAUDE_HOME"])
    text = "# 项目身份\n\n## INV-1 输出保持可读\n问候语不得丢失中文。\n"
    payload = {"base_version": 0, "base_sha256": None, "text": text, "summary": "建立项目身份", "rationale": "所有者决定",
               "decision": {"decided_by": "owner", "human_instruction": "好，记录为项目身份。"}}
    path = tmp_path / "amend.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    revision = run_cli("status")[1]["Task"]["revision"]
    out = _pipe(["identity-amend", "--base-revision", str(revision), "--input", str(path), "--native-session", SESSION],
                home, cwd=task["root"])
    assert out.returncode == 0, out.stdout + out.stderr
    assert json.loads(out.stdout.decode("utf-8"))["version"] == 1

    out = _pipe(["identity-get", "--item", "INV-1", "--native-session", SESSION], home, cwd=task["root"])
    assert out.returncode == 0 and out.stderr == b""
    assert "输出保持可读".encode("utf-8") in out.stdout  # human-readable, not \u-escaped
    assert json.loads(out.stdout.decode("utf-8"))["items"][0]["text"].startswith("## INV-1 输出保持可读")

    # The refusal path is encoded the same way, with the Chinese the caller supplied.
    out = _pipe(["identity-get", "--item", "无此条目-9", "--native-session", SESSION], home, cwd=task["root"])
    refusal = json.loads(out.stdout.decode("utf-8"))
    assert out.returncode == 2 and refusal["ok"] is False
