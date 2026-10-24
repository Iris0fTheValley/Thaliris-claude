"""A smoke of the Controller lifecycle through the real launcher and hook entry points.

Every runner call goes the way Claude Code sends it: the Bash command is first offered to the PreToolUse hook (which
appends the native identity), then the rewritten command is executed. The project is a Git repository whose path
contains Chinese and a space. Deeper lifecycle behavior (judging, conflicts, closure) is covered in-process elsewhere.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from thaliris_claude import hooks

from .test_flow import _host_record

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = str(ROOT / "bin" / "thaliris.py")
SESSION = "aaaaaaaa-bbbb-cccc-dddd-000000000042"
_PLAIN = re.compile(r"[A-Za-z0-9_./:=,@%+-]+")


def _quote(value: str) -> str:
    if _PLAIN.fullmatch(value):
        return value
    return '"' + re.sub(r'(["\\$`])', r"\\\1", value) + '"'


class Session:
    """The Host side of one Controller session."""

    def __init__(self, tmp_path: Path):
        self.repo = tmp_path / "项目 仓库" / "登录修复"
        self.repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
        self.transcript = str(tmp_path / "transcripts" / f"{SESSION}.jsonl")
        self.env = {**os.environ, "THALIRIS_CLAUDE_HOME": str(tmp_path / "home"), "PYTHONUTF8": "1"}
        for name in ("THALIRIS_CLAUDE_PYTHON", "THALIRIS_CLAUDE_PLUGIN_ROOT"):
            self.env.pop(name, None)

    # -- hooks
    def hook(self, event: str, **fields) -> dict | None:
        payload = {"session_id": SESSION, "transcript_path": self.transcript, "cwd": str(self.repo),
                   "hook_event_name": event, **fields}
        done = subprocess.run([sys.executable, LAUNCHER, "hook", event], input=json.dumps(payload).encode("utf-8"),
                              capture_output=True, env=self.env, cwd=self.repo, check=True)
        result = json.loads(done.stdout) if done.stdout.strip() else None
        return result

    # -- runner
    def run(self, label: str, *args: str, agent: tuple[str, str] | None = None) -> tuple[int, dict]:
        command = f"{hooks.runner_command()} " + " ".join(_quote(arg) for arg in args)
        who = {"agent_id": agent[0], "agent_type": agent[1]} if agent else {}
        decision = self.hook("PreToolUse", tool_name="Bash", tool_use_id=f"bash-{label}",
                             tool_input={"command": command, "description": label}, **who)
        output = (decision or {}).get("hookSpecificOutput", {})
        assert output.get("permissionDecision") != "deny", output.get("permissionDecisionReason")
        rewritten = output["updatedInput"]["command"]
        done = subprocess.run(shlex.split(rewritten), capture_output=True, env=self.env, cwd=self.repo, check=False)
        assert done.stdout, done.stderr
        result = json.loads(done.stdout.decode("utf-8"))
        return done.returncode, result

    def must(self, label: str, *args: str, **kwargs) -> dict:
        code, result = self.run(label, *args, **kwargs)
        assert code == 0, (label, result)
        return result

    # -- Agent tool
    def dispatch(self, tool_use_id: str, requirement: str) -> str:
        prompt = f"调查登录跳转 {tool_use_id}\nthaliris-evidence: " + json.dumps(
            {"candidate": "C1", "requirement_refs": [requirement]})
        result = self.hook("PreToolUse", tool_name="Agent", tool_use_id=tool_use_id,
                           tool_input={"description": "work", "prompt": prompt, "subagent_type": "thaliris:investigator",
                                       "run_in_background": True})
        return result["hookSpecificOutput"]["updatedInput"]["prompt"].rsplit("thaliris-handoff: ", 1)[1]

    def start(self, agent_id: str, tool_use_id: str) -> None:
        _host_record({"transcript": self.transcript}, agent_id, "thaliris:investigator", tool_use_id)
        self.hook("SubagentStart", agent_id=agent_id, agent_type="thaliris:investigator")

    def launched(self, tool_use_id: str, agent_id: str) -> str:
        result = self.hook("PostToolUse", tool_name="Agent", tool_use_id=tool_use_id, tool_input={},
                           tool_response={"isAsync": True, "status": "async_launched", "agentId": agent_id})
        return (result or {}).get("hookSpecificOutput", {}).get("additionalContext", "")

    def finished(self, tool_use_id: str, agent_id: str) -> str:
        self.hook("SubagentStop", agent_id=agent_id, agent_type="thaliris:investigator", stop_hook_active=False,
                  last_assistant_message="**Conclusion** done")
        result = self.hook("PostToolUse", tool_name="Agent", tool_use_id=tool_use_id, tool_input={},
                           tool_response={"status": "completed", "agentId": agent_id})
        return (result or {}).get("hookSpecificOutput", {}).get("additionalContext", "")


def test_controller_lifecycle_through_real_entry_points(tmp_path):
    host = Session(tmp_path)

    # Admission with Chinese text in a Chinese, space-containing project path.
    assert host.must("bootstrap", "bootstrap")["initialized"] is True
    started = host.must("task-start", "task-start", "修复登录跳转", "--human-instruction", "修复登录后的跳转，别动别的页面",
                        "--boundary", "只改 app.py", "--invariants", "保持输出为 ASCII", "--acceptance", "登录后跳转到首页",
                        "--execution-mode", "delegated")
    registered = host.must("register", "register", "--base-revision", str(started["revision"]),
                           "--requirement", "R1=login-redirect")
    assert registered["appended"] == ["R1"]

    # One dispatch, one child observation, the packet on the next turn, and status.
    handoff = host.dispatch("tu-a", "R1")
    host.launched("tu-a", "agent-a")
    host.start("agent-a", "tu-a")
    host.must("child observe", "observe", "--text", "登录后重定向写在 app.py:1", "--source", "app.py:1",
              agent=("agent-a", "thaliris:investigator"))
    packet = host.hook("UserPromptSubmit", prompt="继续")["hookSpecificOutput"]["additionalContext"]
    assert packet.count("- observation") == 1
    stored = host.must("get", "get", f"{handoff}.O1")["object"]
    assert stored["handoff_id"] == handoff and stored["requirement_refs"] == ["R1"]
    status = host.must("status", "status")
    assert status["Task"]["id"] == started["task_id"] and status["Counts"]["observations"] == 1
