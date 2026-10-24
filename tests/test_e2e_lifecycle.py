"""A realistic Controller lifecycle through the real launcher and hook entry points.

Every runner call goes the way Claude Code sends it: the Bash command is first offered to the PreToolUse hook (which
appends the native identity), then the rewritten command is executed. The project is a Git repository whose path
contains Chinese and a space. The test records how many runner calls and how many output bytes the Controller reads, so
the numbers can be compared with an earlier adapter version; they are printed and written to
``e2e-metrics.json`` under the test's temporary directory (and to ``$THALIRIS_E2E_METRICS`` when set).
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
        self.calls: list[dict] = []
        self.hook_bytes = 0
        self.model_bytes: dict[str, int] = {}  # hook text the model reads (additionalContext), by event

    # -- hooks
    def hook(self, event: str, **fields) -> dict | None:
        payload = {"session_id": SESSION, "transcript_path": self.transcript, "cwd": str(self.repo),
                   "hook_event_name": event, **fields}
        done = subprocess.run([sys.executable, LAUNCHER, "hook", event], input=json.dumps(payload).encode("utf-8"),
                              capture_output=True, env=self.env, cwd=self.repo, check=True)
        self.hook_bytes += len(done.stdout)
        result = json.loads(done.stdout) if done.stdout.strip() else None
        text = ((result or {}).get("hookSpecificOutput") or {}).get("additionalContext")
        if text:
            self.model_bytes[event] = self.model_bytes.get(event, 0) + len(text.encode("utf-8"))
        return result

    # -- runner
    def run(self, label: str, *args: str, agent: tuple[str, str] | None = None) -> tuple[int, dict]:
        command = f"{hooks.runner_command()} " + " ".join(_quote(arg) for arg in args)
        who = {"agent_id": agent[0], "agent_type": agent[1]} if agent else {}
        decision = self.hook("PreToolUse", tool_name="Bash", tool_use_id=f"bash-{len(self.calls)}",
                             tool_input={"command": command, "description": label}, **who)
        output = (decision or {}).get("hookSpecificOutput", {})
        assert output.get("permissionDecision") != "deny", output.get("permissionDecisionReason")
        rewritten = output["updatedInput"]["command"]
        done = subprocess.run(shlex.split(rewritten), capture_output=True, env=self.env, cwd=self.repo, check=False)
        assert done.stdout, done.stderr
        result = json.loads(done.stdout.decode("utf-8"))
        self.calls.append({"label": label, "exit": done.returncode, "bytes": len(done.stdout), "ok": result.get("ok", True)})
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
    metrics: dict = {}

    # Admission: bootstrap, then the contract as flags with Chinese text.
    assert host.must("bootstrap", "bootstrap")["initialized"] is True
    started = host.must("task-start", "task-start", "修复登录跳转", "--human-instruction", "修复登录后的跳转，别动别的页面",
                        "--boundary", "只改 app.py", "--invariants", "保持输出为 ASCII", "--acceptance", "登录后跳转到首页",
                        "--execution-mode", "delegated")
    task_id = started["task_id"]
    registered = host.must("register", "register", "--base-revision", str(started["revision"]),
                           "--requirement", "R1=login-redirect", "--requirement", "R2=session-expiry")
    assert registered["appended"] == ["R1", "R2"]

    # Three parallel background dispatches in one Controller message.
    handoffs = {name: host.dispatch(name, requirement) for name, requirement in (("tu-a", "R1"), ("tu-b", "R1"), ("tu-c", "R2"))}
    for name, agent in (("tu-a", "agent-a"), ("tu-b", "agent-b"), ("tu-c", "agent-c")):
        assert "Thaliris evidence" not in host.launched(name, agent)  # nothing to judge yet
    for name, agent in (("tu-a", "agent-a"), ("tu-b", "agent-b"), ("tu-c", "agent-c")):
        host.start(agent, name)

    # Siblings both call their first observation O1; one child records two at once.
    who = {name: (name, "thaliris:investigator") for name in ("agent-a", "agent-b", "agent-c")}
    host.must("child observe a", "observe", "--text", "登录后重定向写在 app.py:1", "--source", "app.py:1", agent=who["agent-a"])
    host.must("child observe b", "observe", "--text", "没有第二处重定向", "--source", "app.py:1", "--source", "grep redirect",
              agent=who["agent-b"])
    batch = host.must("child observe c (batch)", "observe", "--json", json.dumps({"observations": [
        {"id": "O1", "text": "会话过期未处理", "source_refs": ["S1"],
         "sources": [{"id": "S1", "kind": "local", "locator": "app.py:9", "summary": "no expiry"}]},
        {"id": "O2", "text": "测试缺失", "source_refs": ["S2"],
         "sources": [{"id": "S2", "kind": "local", "locator": "tests/", "summary": "none"}]}]}), agent=who["agent-c"])
    assert batch["appended"] == [f"{handoffs['tu-c']}.O1", f"{handoffs['tu-c']}.O2"]

    # The next turn shows the packet once; two more parallel launches after it only say the evidence is unchanged.
    first_turn = host.hook("UserPromptSubmit", prompt="继续")
    packet = first_turn["hookSpecificOutput"]["additionalContext"]
    assert packet.count("- observation") == 4
    notes = []
    for name, agent, requirement in (("tu-d", "agent-d", "R1"), ("tu-e", "agent-e", "R2")):
        host.dispatch(name, requirement)
        notes.append(host.launched(name, agent))
    for note in notes:
        assert "Evidence unchanged: 4 unincorporated, 0 unresolved, task revision now" in note, note
        assert "Thaliris evidence for task" not in note and len(note.splitlines()[-1]) < len(packet) // 4
    assert host.hook("UserPromptSubmit", prompt="还在吗") is None  # an unchanged packet is not repeated
    metrics["packet_bytes"], metrics["unchanged_note_bytes"] = len(packet), len(notes[0].splitlines()[-1])
    host.start("agent-d", "tu-d")
    host.start("agent-e", "tu-e")
    for name, agent in (("tu-d", "agent-d"), ("tu-e", "agent-e")):
        host.hook("SubagentStop", agent_id=agent, agent_type="thaliris:investigator", stop_hook_active=False)
    final = host.finished("tu-a", "agent-a")  # a child's final report: the full packet comes with it
    assert "Thaliris evidence for task" in final and final.count("- observation") == 4
    host.finished("tu-b", "agent-b")
    host.finished("tu-c", "agent-c")

    # No lost or mismatched observations: four, each filed under its own handoff.
    status = host.must("status", "status")
    assert status["Counts"]["observations"] == 4 and status["Counts"]["handoffs"] == 5
    ids = {handoffs["tu-a"]: [f"{handoffs['tu-a']}.O1"], handoffs["tu-b"]: [f"{handoffs['tu-b']}.O1"],
           handoffs["tu-c"]: [f"{handoffs['tu-c']}.O1", f"{handoffs['tu-c']}.O2"]}
    for handoff, observation_ids in ids.items():
        for observation_id in observation_ids:
            stored = host.must(f"get {observation_id}", "get", observation_id)["object"]
            assert stored["handoff_id"] == handoff and stored["producer"] == f"handoff:{handoff}"
            assert stored["candidate"] == "C1" and stored["requirement_refs"] == [("R2" if handoff == handoffs["tu-c"] else "R1")]
    first_a = host.must("get a", "get", f"{handoffs['tu-a']}.O1")["object"]
    assert first_a["text"] == "登录后重定向写在 app.py:1" and first_a["source_refs"] == [f"{handoffs['tu-a']}.S1"]

    # A bare O1 is ambiguous across siblings: refused with a code, nothing written.
    revision = status["Task"]["revision"]
    code, ambiguous = host.run("judge ambiguous", "judge", "--base-revision", str(revision), "--candidate", "C1",
                               "--judgment", "R1|satisfied|O1|ok")
    assert code == 2 and ambiguous["ok"] is False and ambiguous.get("code")
    assert host.must("status", "status")["Task"]["revision"] == revision

    # Batch judge: qualified ids for the ambiguous ones, a bare id where it is unique.
    a1, b1 = f"{handoffs['tu-a']}.O1", f"{handoffs['tu-b']}.O1"
    judged = host.must("judge batch", "judge", "--base-revision", str(revision), "--candidate", "C1",
                       "--judgment", f"R1|satisfied|{a1},{b1}|两处调查都指向同一个重定向",
                       "--judgment", f"R2|repair|{handoffs['tu-c']}.O1,O2|会话过期没有处理，测试也缺失")
    assert judged["appended"] == ["J1", "J2"] and judged["revision"] == revision + 1
    assert judged["evidence"]["unincorporated_total"] == 0 and judged["evidence"]["unresolved_judgments_total"] == 1
    assert run_get(host, "J2")["observation_refs"] == [f"{handoffs['tu-c']}.O1", f"{handoffs['tu-c']}.O2"]

    # Task context by flags, then a deliberately stale write and its recovery.
    updated = host.must("task-update", "task-update", "--base-revision", str(judged["revision"]),
                        "--record", "decision:保留重定向，补会话过期处理", "--record", "fact:app.py 只在第 1 行重定向",
                        "--milestone", "M1 调查完成", "--active-work", "implementer: 会话过期", "--pending", "审查结果")
    assert updated["appended"] == ["D1", "F1"] and updated["revision"] == judged["revision"] + 1
    code, conflict = host.run("stale task-update", "task-update", "--base-revision", str(judged["revision"]), "--record",
                              "fact:过期的写入")
    assert code == 2 and conflict["code"] == "TASK_REVISION_CONFLICT"
    assert conflict["details"]["current_revision"] == updated["revision"]
    metrics["conflict_bytes"] = next(call["bytes"] for call in host.calls if call["label"] == "stale task-update")
    retried = host.must("retry task-update", "task-update", "--base-revision", str(conflict["details"]["current_revision"]),
                        "--record", "fact:过期的写入")
    assert retried["appended"] == ["F2"]

    # Compact vs full status, then closure.
    compact = host.must("status compact", "status")
    full = host.must("status full", "status", "--view", "full")
    assert compact["Task"]["id"] == task_id and compact["Records"]["total"] == 3
    assert {item["id"] for item in compact["Records"]["items"]} == {"D1", "F1", "F2"}
    closed = host.must("close", "close", "--base-revision", str(retried["revision"]))
    assert closed["status"] == "DONE" and closed["evidence"]["unresolved_judgments_total"] == 1

    sizes = {call["label"]: call["bytes"] for call in host.calls}
    metrics.update(runner_calls=len(host.calls), runner_output_bytes=sum(call["bytes"] for call in host.calls),
                   hook_output_bytes=host.hook_bytes, hook_text_read_by_model_bytes=host.model_bytes, status_compact_bytes=sizes["status compact"],
                   status_full_bytes=sizes["status full"], write_response_bytes={
                       label: sizes[label] for label in ("register", "judge batch", "task-update", "retry task-update", "close",
                                                          "child observe a", "child observe c (batch)")},
                   calls=host.calls)
    text = json.dumps(metrics, ensure_ascii=False, indent=2)
    print("\nE2E-METRICS " + json.dumps({key: value for key, value in metrics.items() if key != "calls"}, ensure_ascii=False))
    (tmp_path / "e2e-metrics.json").write_text(text, encoding="utf-8")
    if os.environ.get("THALIRIS_E2E_METRICS"):
        Path(os.environ["THALIRIS_E2E_METRICS"]).write_text(text, encoding="utf-8")
    assert metrics["status_compact_bytes"] < metrics["status_full_bytes"]
    assert full["Claude Dispatch Count"] == 5


def run_get(host: Session, object_id: str) -> dict:
    return host.must(f"get {object_id}", "get", object_id)["object"]
