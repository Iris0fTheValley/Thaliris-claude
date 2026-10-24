#!/usr/bin/env python
"""A/B replay of one realistic Controller management workload against two Thaliris Claude adapter trees.

The same session is replayed against an OLD tree (default: the 0.4.0 commit 14ecc96) and the NEW tree (this checkout)
through the real launcher ``bin/thaliris.py`` and the real hook entry ``bin/thaliris-hook`` (``sh``, with the
``--filter`` the shipped ``hooks.json`` uses). Every Controller runner call is first offered to the PreToolUse hook (which
appends the native identity), then the rewritten command runs, exactly as Claude Code does. Each tree is driven the way
*its own* guidance tells the Controller to drive it:

* OLD: contract written to a file first (+1 Write call), ``task-update`` through a hand-written JSON file (+1 Write call
  each; ``--old-update inline`` uses ``--json`` instead), one ``judge`` call per judgment, ``register`` as inline JSON.
  The Controller does not learn the task revision from OLD acknowledgements after child activity, so
  - policy A "observed": write with the last revision seen anywhere (own ack, status, evidence packet); on
    ``task revision conflict`` run ``status`` and retry;
  - policy B "careful": run ``status`` before a write whenever child activity happened since the revision was learned.
* NEW: ``task-start``/``register``/``task-update`` as flags, one batch ``judge`` per decision point, the revision taken from
  the latest acknowledgement or "task revision now N" line, one retry from ``details.current_revision`` on a conflict.

The workload (derived from an audited real session): 1 task, 7 requirements, 18 child dispatches in 4 waves of
4/5/4/5 background launches, 12 child observations (siblings reuse the local id O1), 33 requirement judgments at 6 decision
points, 8 task-context updates with 2-4 records each (5 of 8 in Chinese), 6 explicit status reads, then ``close``. The
project is a Git repository whose path contains Chinese and a space; if a tree cannot even admit the task there (the 0.4.0
Chinese-Git-path bug) that is recorded as an error of that tree and the identical workload is replayed in an ASCII path.

What it cannot measure: real model latency, Host scheduling and permission prompts, prompt-cache effects. See
``benchmarks/README.md``.

Usage (Python >= 3.11):
    python benchmarks/controller_overhead.py [--old PATH] [--repeat N] [--out results.json] [--md report.md]
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
OLD_COMMIT = "14ecc96"
SESSION = "bbbbbbbb-1111-2222-3333-000000000001"
PROBE_SESSION = "bbbbbbbb-1111-2222-3333-000000000002"
# Calibration from the audited real session db23bba5 (71 runner calls): median model gap before a runner call and
# median Host/tool wait of a runner call, in seconds.
MODEL_GAP_S = 4.4
TOOL_WAIT_S = 0.67
MODEL_EVENTS = ("PostToolUse", "SubagentStart", "UserPromptSubmit", "SessionStart")
_PLAIN = re.compile(r"[A-Za-z0-9_./:=,@%+-]+")
_REVISION = re.compile(r"revision(?: now)? (\d+)")


# ----------------------------------------------------------------------------------------------------- workload

WAVES = (4, 5, 4, 5)  # background launches in one Controller message each: 18 dispatches
EARLY_OBSERVERS = (0, 0, 2, 3)  # children that already observed when the Controller writes its in-wave task-update
OBSERVATIONS = ((1, 1, 0, 1), (1, 0, 1, 1, 0), (2, 0, 1, 0), (1, 0, 1, 0, 1))  # 12 observations, O1 reused by siblings
# decision points: (requirements judged, candidate); after each wave, then two re-judgments of the repaired candidate C2
POINTS = ((("R1", "R2", "R3", "R4", "R5"), "C1"),
          (("R1", "R2", "R3", "R4", "R5", "R6"), "C1"),
          (("R3", "R4", "R5", "R6", "R7"), "C1"),
          (("R1", "R2", "R3", "R4", "R5", "R6"), "C1"),
          (("R2", "R3", "R4", "R5", "R6", "R7"), "C2"),
          (("R1", "R2", "R3", "R4", "R5"), "C2"))
OUTCOMES = ("repair", "satisfied", "unknown", "satisfied", "no-current-impact", "satisfied")
REQUIREMENTS = {"R1": "login-redirect", "R2": "session-expiry", "R3": "logout-cleanup", "R4": "csrf-token",
                "R5": "remember-me", "R6": "error-pages", "R7": "audit-log"}
# (kind, text) records per update; the update index is where it happens: 0 before wave 1, 1-4 during waves, 5-7 after decision
UPDATES = (
    (True, [("decision", "保留现有重定向实现，只补会话过期处理 (app.py:1)"), ("fact", "登录后重定向只写在 app.py 第 1 行"),
            ("open-item", "会话过期路径没有测试覆盖")], ["investigator: 登录跳转与会话过期"], ["第一批调查结果"], "M1 范围确认"),
    (False, [("fact", "redirect is a single call at app.py:1"), ("progress", "wave 1 dispatched: 4 investigators")],
     ["wave 1: 4 investigators"], ["wave 1 reports"], None),
    (True, [("progress", "第二批 5 个调查已派发，覆盖 CSRF 与 remember-me"), ("fact", "CSRF 令牌没有绑定会话"),
            ("decision", "CSRF 与 remember-me 合并到同一个修复候选")], ["wave 2: 5 investigators"], ["wave 2 reports"], None),
    (False, [("progress", "wave 3 dispatched: error pages and audit log"), ("fact", "audit log writes happen in two places"),
             ("open-item", "need a regression test for the logout cleanup"), ("decision", "fix both audit writers together")],
     ["wave 3: 4 investigators"], ["wave 3 reports", "candidate C2"], "M2 候选 C2"),
    (True, [("progress", "第四批 5 个实现与验证已派发"), ("fact", "错误页在未登录时返回 500")],
     ["wave 4: 5 workers"], ["wave 4 reports"], None),
    (False, [("decision", "C2 repairs R2 and R6; keep R7 as unknown until the audit test lands"),
             ("fact", "csrf token now bound to the session id (src/auth.py:88)")],
     ["review of C2"], ["reviewer result"], "M3 repair applied"),
    (True, [("progress", "C2 复核完成，只剩审计日志测试"), ("fact", "回归测试 18 项全部通过"), ("decision", "R7 以 unknown 结案并向用户说明")],
     [], ["最终验收"], "M4 验收"),
    (True, [("progress", "所有需求已逐项判断，准备关闭任务"), ("fact", "没有未完成的子任务派发")], [], [], "M5 关闭前"),
)
UPDATE_AFTER_WAVE = {0: 1, 1: 2, 2: 3, 3: 4}  # wave index -> update index done right after the launch (children running)
UPDATE_AFTER_POINT = {3: 5, 4: 6, 5: 7}  # decision point index -> update index done after its judgments

ZH_REASON = "已核对 {n} 项调查证据，{req} 的结论与候选 {cand} 一致，保留的风险已记录在任务上下文"
EN_REASON = "Reviewed {n} observation(s) for {req}; candidate {cand} matches the acceptance wording, residual risk is recorded"
OBS_ZH = ("登录后重定向写在 app.py:1，没有第二处", "会话过期没有清理 cookie，app.py:9", "测试目录没有登录相关用例")
OBS_EN = ("redirect is set once at app.py:1", "csrf token is not bound to the session id", "audit log is written in two places")
GOAL = "修复登录跳转与会话过期"
INSTRUCTION = "修复登录后的跳转，并处理会话过期；别动别的页面"
BOUNDARY = "只改 app.py 与 src/auth.py，不改 UI 页面"
INVARIANTS = "保持输出为 ASCII；不改公开接口"
ACCEPTANCE = "登录后跳转到首页，会话过期后回到登录页，回归测试全部通过"


def _quote(value: str) -> str:
    if _PLAIN.fullmatch(value):
        return value
    return '"' + re.sub(r'(["\\$`])', r"\\\1", value) + '"'


def _json_arg(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    assert "'" not in text, text
    return "'" + text + "'"


# ----------------------------------------------------------------------------------------------------- measurement

class Meter:
    """Everything the replay measures, one row per subprocess or Controller action."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.round_trips = 0
        self.messages: list[str] = []
        self.authored_bytes = 0  # what the Controller itself writes: runner command strings and written files
        self.active = True

    def add(self, **row) -> None:
        if self.active:
            self.rows.append(row)

    def summary(self) -> dict:
        rows = self.rows
        runner = [r for r in rows if r["kind"] == "runner" and r["who"] == "controller"]
        child_runner = [r for r in rows if r["kind"] == "runner" and r["who"] == "child"]
        hooks = [r for r in rows if r["kind"] == "hook"]
        writes = [r for r in rows if r["kind"] == "write"]
        agents = [r for r in rows if r["kind"] == "agent"]
        by_event: dict[str, int] = {}
        for row in hooks:
            if row["event"] in MODEL_EVENTS:
                by_event[row["event"]] = by_event.get(row["event"], 0) + row["ctx_bytes"]
        errors: dict[str, int] = {}
        for row in rows:
            if row.get("error"):
                errors[row["error"]] = errors.get(row["error"], 0) + 1
        by_sub: dict[str, int] = {}
        for row in runner:
            by_sub[row["sub"]] = by_sub.get(row["sub"], 0) + 1
        model_ctx = sum(by_event.values())
        stdout = sum(r["out_bytes"] for r in runner)
        wall = {"runner_s": sum(r["secs"] for r in rows if r["kind"] == "runner"),
                "hook_s": sum(r["secs"] for r in hooks)}
        wall["total_s"] = wall["runner_s"] + wall["hook_s"]
        wall["controller_s"] = sum(r["secs"] for r in rows if r["who"] == "controller" and r["kind"] in ("runner", "hook"))
        wall["child_s"] = wall["total_s"] - wall["controller_s"]
        return {"controller_runner_calls": len(runner), "controller_write_calls": len(writes), "agent_launches": len(agents),
                "controller_tool_calls": len(runner) + len(writes), "controller_calls_with_agents": len(runner) + len(writes) + len(agents),
                "round_trips": self.round_trips, "runner_stdout_bytes": stdout,
                "child_runner_stdout_bytes": sum(r["out_bytes"] for r in child_runner),
                "model_ctx_bytes": model_ctx, "model_ctx_by_event": by_event,
                "controller_visible_bytes": stdout + sum(by_event.get(e, 0) for e in ("PostToolUse", "UserPromptSubmit", "SessionStart")),
                "approx_tokens": (stdout + model_ctx) // 4, "errors": errors, "error_count": sum(errors.values()),
                "runner_by_subcommand": by_sub, "wall": wall, "controller_authored_bytes": self.authored_bytes,
                "hook_calls": len(hooks), "runner_subprocesses": len(runner) + len(child_runner)}


# ----------------------------------------------------------------------------------------------------- the Host

def _error_name(result: dict) -> str:
    if result.get("code"):
        return str(result["code"])
    text = str(result.get("error") or "unknown")
    return re.sub(r"\s+", " ", text.split(":")[0])[:60]


class Host:
    """Claude Code's side of one session: hooks around every tool call, the runner as a subprocess."""

    def __init__(self, plugin: Path, workdir: Path, repo: Path, python: str, meter: Meter, session: str = SESSION,
                 entry: str = "sh", utf8: bool = False):
        self.plugin, self.repo, self.python, self.meter, self.session, self.entry = plugin, repo, python, meter, session, entry
        self.transcript = workdir / "transcripts" / f"{session}.jsonl"
        self.home = workdir / "home"
        (self.home / "runtime").mkdir(parents=True, exist_ok=True)
        (self.home / "runtime" / "python").write_text(Path(python).as_posix() + "\n", encoding="utf-8")  # the launcher's cache
        self.env = {**os.environ, "THALIRIS_CLAUDE_HOME": str(self.home)}
        if utf8:  # a Host normally does not set it; without it OLD cannot read a Chinese Git path on Windows
            self.env["PYTHONUTF8"] = "1"
        else:
            self.env.pop("PYTHONUTF8", None)
        for name in ("THALIRIS_CLAUDE_PYTHON", "THALIRIS_CLAUDE_PLUGIN_ROOT"):
            self.env.pop(name, None)
        self.runner = f'"{Path(python).as_posix()}" "{(plugin / "bin" / "thaliris.py").as_posix()}"'
        self.sh = shutil.which("sh") if entry == "sh" else None
        if entry == "sh" and not self.sh:
            raise SystemExit("`sh` not found on PATH; use --hook-entry launcher")
        self.call_index = 0
        self.seen: list[str] = []  # model-visible text, in order (the Controller's knowledge is derived from it)

    # -- hooks
    def hook(self, event: str, who: str = "controller", filter_: bool = False, **fields) -> dict | None:
        payload = {"session_id": self.session, "transcript_path": str(self.transcript), "cwd": str(self.repo),
                   "hook_event_name": event, **fields}
        if self.entry == "sh":
            command = [self.sh, (self.plugin / "bin" / "thaliris-hook").as_posix(), event] + (["--filter"] if filter_ else [])
        else:
            command = [self.python, str(self.plugin / "bin" / "thaliris.py"), "hook", event]
        started = time.perf_counter()
        done = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False).encode("utf-8"), capture_output=True,
                              env=self.env, cwd=self.repo, check=False)
        secs = time.perf_counter() - started
        stdout = done.stdout.decode("utf-8", "replace").strip()
        result = None
        if stdout:
            try:
                result = json.loads(stdout)
            except ValueError:
                result = None
        output = (result or {}).get("hookSpecificOutput") or {}
        text = output.get("additionalContext") or ""
        self.meter.add(kind="hook", who=who, event=event, secs=secs, ctx_bytes=len(text.encode("utf-8")),
                       out_bytes=len(stdout.encode("utf-8")), error=None if done.returncode == 0 else f"hook-exit-{done.returncode}")
        if done.returncode != 0:
            raise RuntimeError(f"hook {event} exited {done.returncode}: {done.stderr.decode('utf-8', 'replace')[:300]}")
        if text and event in ("PostToolUse", "UserPromptSubmit", "SessionStart"):  # the Controller's own context
            self.seen.append(text)
        return result

    # -- runner (Bash tool)
    def run(self, label: str, *args: str, agent: tuple[str, str] | None = None, metered: bool = True) -> tuple[int, dict]:
        command = f"{self.runner} " + " ".join(args)
        who = {"agent_id": agent[0], "agent_type": agent[1]} if agent else {}
        role = "child" if agent else "controller"
        if metered:
            self.meter.authored_bytes += 0 if agent else len(command.encode("utf-8"))
        before = len(self.meter.rows)
        decision = self.hook("PreToolUse", role, True, tool_name="Bash", tool_use_id=f"bash-{self.call_index}",
                             tool_input={"command": command, "description": label}, **who)
        self.call_index += 1
        output = (decision or {}).get("hookSpecificOutput", {})
        if output.get("permissionDecision") == "deny":
            result = {"ok": False, "error": "HOOK_POLICY_DENY", "code": "HOOK_POLICY_DENY",
                      "message": output.get("permissionDecisionReason")}
            self.meter.add(kind="runner", who=role, sub=args[0], secs=0.0, out_bytes=0, error="HOOK_POLICY_DENY")
            return 2, result
        rewritten = output["updatedInput"]["command"] if "updatedInput" in output else command
        started = time.perf_counter()
        done = subprocess.run(shlex.split(rewritten), capture_output=True, env=self.env, cwd=self.repo, check=False)
        secs = time.perf_counter() - started
        stdout = done.stdout.decode("utf-8", "replace")
        try:
            result = json.loads(stdout)
        except ValueError:
            result = {"ok": False, "error": "NON_JSON_OUTPUT: " + (done.stderr.decode("utf-8", "replace")[-200:] or stdout[-200:])}
        failed = done.returncode != 0 or result.get("ok") is False
        self.meter.add(kind="runner", who=role, sub=args[0], secs=secs, out_bytes=len(done.stdout), exit=done.returncode,
                       error=_error_name(result) if failed else None)
        if not metered:  # unmetered verification calls: drop the rows just added (hook + runner)
            del self.meter.rows[before:]
        return done.returncode, result

    # -- Write tool
    def write_file(self, path: Path, content: str) -> None:
        self.hook("PreToolUse", "controller", True, tool_name="Write", tool_use_id=f"write-{self.call_index}",
                  tool_input={"file_path": str(path), "content": content})
        self.call_index += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.meter.authored_bytes += len(content.encode("utf-8"))
        self.meter.add(kind="write", who="controller", secs=0.0, out_bytes=0)

    # -- Agent tool and the child's lifecycle
    def dispatch(self, tool_use_id: str, requirements: list[str], candidate: str) -> str:
        prompt = ("调查并核对下列需求在当前代码中的实现位置，给出 path:line 证据；需要时记录 observation。\n"
                  + "背景: 登录成功后的跳转、会话过期处理、CSRF 令牌、remember-me、错误页与审计日志。\n" * 3
                  + "thaliris-evidence: " + json.dumps({"candidate": candidate, "requirement_refs": requirements}))
        result = self.hook("PreToolUse", "controller", False, tool_name="Agent", tool_use_id=tool_use_id,
                           tool_input={"description": "work", "prompt": prompt, "subagent_type": "thaliris:investigator",
                                       "run_in_background": True})
        self.meter.authored_bytes += len(prompt.encode("utf-8"))
        self.meter.add(kind="agent", who="controller", secs=0.0, out_bytes=0)
        return result["hookSpecificOutput"]["updatedInput"]["prompt"].rsplit("thaliris-handoff: ", 1)[1]

    def launched(self, tool_use_id: str, agent_id: str) -> None:
        self.hook("PostToolUse", "controller", False, tool_name="Agent", tool_use_id=tool_use_id, tool_input={},
                  tool_response={"isAsync": True, "status": "async_launched", "agentId": agent_id})

    def start(self, agent_id: str, tool_use_id: str) -> None:
        directory = self.transcript.with_suffix("") / "subagents"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"agent-{agent_id}.meta.json").write_text(json.dumps(
            {"agentType": "thaliris:investigator", "toolUseId": tool_use_id, "spawnDepth": 1, "model": "haiku", "finished": False}),
            encoding="utf-8")
        (directory / f"agent-{agent_id}.prefix.json").write_text(json.dumps(
            {"v": 1, "state": {"toolNames": ["Read", "Grep", "Glob", "Bash"], "model": "claude-haiku-5-5"}}), encoding="utf-8")
        self.hook("SubagentStart", "child", False, agent_id=agent_id, agent_type="thaliris:investigator")

    def stop(self, agent_id: str) -> None:
        self.hook("SubagentStop", "child", False, agent_id=agent_id, agent_type="thaliris:investigator",
                  stop_hook_active=False, last_assistant_message="**Conclusion** done")

    def prompt(self, text: str) -> None:
        self.hook("UserPromptSubmit", "controller", False, prompt=text)


# ----------------------------------------------------------------------------------------------------- Controllers

class Controller:
    """The Controller's side: which calls it makes, what it knows about the revision, how it recovers."""

    version = "?"

    def __init__(self, host: Host, policy: str = "observed", old_update: str = "file", workdir: Path | None = None):
        self.host, self.meter, self.policy, self.old_update = host, host.meter, policy, old_update
        self.workdir = workdir or Path(tempfile.gettempdir())
        self.known_rev: int | None = None
        self.stale = False
        self.cursor = 0  # how much of host.seen the Controller has already read
        self.expected: dict = {"observations": {}, "judgments": {}, "records": {}, "requirements": []}
        self.handoffs: dict[str, dict] = {}
        self.judgment_count = 0
        self.record_counts: dict[str, int] = {}
        self.acks: list[int] = []  # every revision an acknowledgement reported, in order
        self.task_id: str | None = None

    # -- knowledge of the revision
    def absorb(self) -> None:
        """Read the model-visible hook text produced since the last look; a revision stated there is now known."""
        for text in self.host.seen[self.cursor:]:
            found = _REVISION.findall(text)
            if found:
                self.learn(int(found[-1]))
        self.cursor = len(self.host.seen)

    def learn(self, revision: int) -> None:
        self.known_rev, self.stale = revision, False

    def child_activity(self) -> None:
        self.stale = True

    # -- messages (one assistant message = one sequential model round trip)
    @contextlib.contextmanager
    def message(self, label: str):
        self.meter.round_trips += 1
        self.meter.messages.append(label)
        yield

    def call(self, label: str, *args: str, ok: bool = True) -> tuple[int, dict]:
        """One runner call in its own message."""
        with self.message(label):
            return self._runner(label, *args, ok=ok)

    def _runner(self, label: str, *args: str, ok: bool = True) -> tuple[int, dict]:
        code, result = self.host.run(label, *args)
        if result.get("ok") is not False and isinstance(result.get("revision"), int):
            self.learn(result["revision"])
            self.acks.append(result["revision"])
        if ok and result.get("ok") is False:
            raise StepFailed(label, result)
        return code, result

    @staticmethod
    def is_conflict(result: dict) -> bool:
        return result.get("ok") is False and ("REVISION_CONFLICT" in str(result.get("code")) or "revision conflict" in str(result.get("error")))

    def status(self, label: str = "status") -> dict:
        _, result = self.call(label, "status")
        self.learn(result["Task"]["revision"])
        return result

    def write(self, label: str, build) -> dict:
        """A revision-guarded write. ``build(revision)`` returns the runner arguments."""
        raise NotImplementedError

    # -- hook text seen between calls
    def after_hooks(self) -> None:
        self.absorb()

    # -- operations (implemented per version)
    def admission(self) -> dict: raise NotImplementedError
    def update(self, index: int) -> None: raise NotImplementedError
    def judge_point(self, index: int, handoffs: list[dict]) -> None: raise NotImplementedError
    def close(self) -> dict: raise NotImplementedError
    def child_observe(self, agent: tuple[str, str], texts: list[str], handoff: dict) -> None: raise NotImplementedError
    def stale_probe(self) -> tuple[int, dict]: raise NotImplementedError

    # -- shared pieces
    def requirements_for(self, handoffs: list[dict], requirement: str) -> list[str]:
        mine = [obs for h in handoffs if requirement in h["refs"] for obs in h["observations"]]
        recent = [obs for h in handoffs for obs in h["observations"]]
        picked = (mine or recent)[-3:]
        return picked

    def judgment_plan(self, index: int, handoffs: list[dict]) -> list[dict]:
        requirements, candidate = POINTS[index]
        plan = []
        for number, requirement in enumerate(requirements):
            refs = self.requirements_for(handoffs, requirement)
            zh = (index + number) % 2 == 0
            reasoning = (ZH_REASON if zh else EN_REASON).format(n=len(refs), req=requirement, cand=candidate)
            self.judgment_count += 1
            plan.append({"id": f"J{self.judgment_count}", "requirement": requirement, "candidate": candidate,
                         "outcome": OUTCOMES[(self.judgment_count + index) % len(OUTCOMES)], "refs": refs, "reasoning": reasoning})
        return plan

    def note_judgments(self, plan: list[dict]) -> None:
        for item in plan:
            self.expected["judgments"][item["id"]] = item

    def update_spec(self, index: int) -> dict:
        zh, records, active, pending, milestone = UPDATES[index]
        return {"zh": zh, "records": records, "active": active, "pending": pending, "milestone": milestone}


class StepFailed(RuntimeError):
    def __init__(self, label: str, result: dict):
        super().__init__(f"{label}: {json.dumps(result, ensure_ascii=False)[:400]}")
        self.label, self.result = label, result


class OldController(Controller):
    """0.4.0: file contract, JSON payloads, one judgment per call, no revision in acknowledgements."""

    version = "old"

    def admission(self) -> dict:
        self.host.seen.clear()
        self.cursor = 0
        self.call("bootstrap", "bootstrap")
        contract = self.workdir / "contract.json"
        with self.message("write contract"):
            self.host.write_file(contract, json.dumps({
                "human_instruction": INSTRUCTION, "boundary": BOUNDARY, "invariants": INVARIANTS, "acceptance": ACCEPTANCE,
                "execution_mode": "delegated"}, ensure_ascii=False, indent=2))
        _, started = self.call("task-start", "task-start", _quote(GOAL), "--contract", _quote(contract.as_posix()))
        self.learn(started["revision"])
        self.task_id = started["task_id"]
        self.write("register", lambda rev: ["register", "--base-revision", str(rev), "--json", _json_arg(
            {"requirements": [{"id": key, "workchain_ref": ref} for key, ref in REQUIREMENTS.items()]})])
        self.expected["requirements"] = list(REQUIREMENTS)
        return started

    def write(self, label: str, build) -> dict:
        self.absorb()
        if self.policy == "careful" and self.stale:
            self.status(f"{label} (status first)")
        code, result = self.call(label, *build(self.known_rev), ok=False)
        if self.is_conflict(result):
            self.status(f"{label} (conflict recovery)")
            code, result = self.call(f"{label} (retry)", *build(self.known_rev), ok=False)
        if result.get("ok") is False:
            raise StepFailed(label, result)
        return result

    def update(self, index: int) -> None:
        spec = self.update_spec(index)
        records = []
        for kind, text in spec["records"]:
            prefix = {"decision": "D", "fact": "F", "progress": "P", "open-item": "Q"}[kind]
            self.record_counts[prefix] = self.record_counts.get(prefix, 0) + 1
            record_id = f"{prefix}{self.record_counts[prefix]}"
            records.append({"id": record_id, "kind": kind, "text": text, "source_refs": []})
            self.expected["records"][record_id] = text
        payload: dict = {"records": records}
        if spec["active"] or index == 0:
            payload["active_work"] = spec["active"]
        payload["pending_results"] = spec["pending"]
        if spec["milestone"]:
            payload["current_milestone"] = spec["milestone"]
        if self.old_update == "inline":
            self.write(f"task-update {index + 1}", lambda rev: ["task-update", "--base-revision", str(rev), "--json", _json_arg(payload)])
            return
        path = self.workdir / f"update{index + 1}.json"
        with self.message(f"write update{index + 1}.json"):
            self.host.write_file(path, json.dumps(payload, ensure_ascii=False, indent=2))
        self.write(f"task-update {index + 1}", lambda rev: ["task-update", "--base-revision", str(rev), "--input", _quote(path.as_posix())])

    def judge_point(self, index: int, handoffs: list[dict]) -> None:
        plan = self.judgment_plan(index, handoffs)
        for item in plan:
            self.write(f"judge {item['id']}", lambda rev, item=item: [
                "judge", "--base-revision", str(rev), "--json", _json_arg({
                    "id": item["id"], "candidate": item["candidate"], "requirement_ref": item["requirement"],
                    "observation_refs": item["refs"], "outcome": item["outcome"], "reasoning": item["reasoning"]})])
        self.note_judgments(plan)

    def close(self) -> dict:
        return self.write("close", lambda rev: ["close", "--base-revision", str(rev)])

    def child_observe(self, agent: tuple[str, str], texts: list[str], handoff: dict) -> None:
        for number, text in enumerate(texts, 1):
            code, result = self.host.run("child observe", "observe", "--json", _json_arg({
                "id": f"O{number}", "text": text, "source_refs": [f"S{number}"],
                "sources": [{"id": f"S{number}", "kind": "local", "locator": f"app.py:{number}", "summary": "exact supporting fact"}]}),
                agent=agent)
            if result.get("ok") is False:
                raise StepFailed("child observe", result)

    def stale_probe(self) -> tuple[int, dict]:
        stale = self.known_rev - 1
        return self.host.run("stale probe", "task-update", "--base-revision", str(stale), "--json",
                             _json_arg({"current_milestone": "stale probe"}), metered=False)


class NewController(Controller):
    """0.4.1: flags, batch judge, revision from the latest acknowledgement or packet line, one retry on conflict."""

    version = "new"

    def admission(self) -> dict:
        self.host.seen.clear()
        self.cursor = 0
        self.call("bootstrap", "bootstrap")
        _, started = self.call("task-start", "task-start", _quote(GOAL), "--human-instruction", _quote(INSTRUCTION),
                               "--boundary", _quote(BOUNDARY), "--invariants", _quote(INVARIANTS), "--acceptance", _quote(ACCEPTANCE),
                               "--execution-mode", "delegated")
        self.learn(started["revision"])
        self.task_id = started["task_id"]
        self.write("register", lambda rev: ["register", "--base-revision", str(rev)] + [
            part for key, ref in REQUIREMENTS.items() for part in ("--requirement", f"{key}={ref}")])
        self.expected["requirements"] = list(REQUIREMENTS)
        return started

    def write(self, label: str, build) -> dict:
        self.absorb()
        code, result = self.call(label, *build(self.known_rev), ok=False)
        if self.is_conflict(result):
            current = (result.get("details") or {}).get("current_revision")
            if not isinstance(current, int):
                self.status(f"{label} (conflict recovery)")
                current = self.known_rev
            self.learn(current)
            code, result = self.call(f"{label} (retry)", *build(current), ok=False)
        if result.get("ok") is False:
            raise StepFailed(label, result)
        return result

    def update(self, index: int) -> None:
        spec = self.update_spec(index)
        args = ["task-update"]
        for kind, text in spec["records"]:
            args += ["--record", _quote(f"{kind}:{text}")]
        for item in spec["active"]:
            args += ["--active-work", _quote(item)]
        if not spec["active"] and index == 0:
            args += ["--clear-active-work"]
        for item in spec["pending"]:
            args += ["--pending", _quote(item)]
        if spec["milestone"]:
            args += ["--milestone", _quote(spec["milestone"])]
        result = self.write(f"task-update {index + 1}", lambda rev: [args[0], "--base-revision", str(rev)] + args[1:])
        for record_id, (kind, text) in zip(result.get("appended") or [], spec["records"]):
            self.expected["records"][record_id] = text

    def judge_point(self, index: int, handoffs: list[dict]) -> None:
        plan = self.judgment_plan(index, handoffs)
        candidate = plan[0]["candidate"]
        args = ["judge", "--candidate", candidate]
        for item in plan:
            args += ["--judgment", _quote(f"{item['requirement']}|{item['outcome']}|{','.join(item['refs'])}|{item['reasoning']}")]
        result = self.write(f"judge batch {index + 1}", lambda rev: [args[0], "--base-revision", str(rev)] + args[1:])
        if result.get("appended") != [item["id"] for item in plan]:
            raise StepFailed("judge ids", result)
        self.note_judgments(plan)

    def close(self) -> dict:
        return self.write("close", lambda rev: ["close", "--base-revision", str(rev)])

    def child_observe(self, agent: tuple[str, str], texts: list[str], handoff: dict) -> None:
        if len(texts) == 1:
            code, result = self.host.run("child observe", "observe", "--text", _quote(texts[0]), "--source", "app.py:1",
                                         "--source", _quote("grep redirect"), agent=agent)
        else:
            code, result = self.host.run("child observe batch", "observe", "--json", _json_arg({"observations": [
                {"id": f"O{number}", "text": text, "source_refs": [f"S{number}"],
                 "sources": [{"id": f"S{number}", "kind": "local", "locator": f"app.py:{number}", "summary": "exact supporting fact"}]}
                for number, text in enumerate(texts, 1)]}), agent=agent)
        if result.get("ok") is False:
            raise StepFailed("child observe", result)

    def stale_probe(self) -> tuple[int, dict]:
        return self.host.run("stale probe", "task-update", "--base-revision", str(self.known_rev - 1), "--milestone", _quote("stale probe"),
                             metered=False)


# ----------------------------------------------------------------------------------------------------- the workload

def run_workload(controller: Controller, check: dict) -> dict:
    """Replay the session. Returns facts the correctness checks need."""
    host = controller.host
    host.hook("SessionStart", "controller", False, source="startup")
    host.prompt("修复登录跳转与会话过期，按 Thaliris 流程完成")
    controller.admission()
    controller.after_hooks()
    controller.update(0)

    handoffs: list[dict] = []
    child_number = 0
    for wave, size in enumerate(WAVES):
        members = []
        for slot in range(size):
            child_number += 1
            refs = [f"R{(child_number - 1) % 7 + 1}"] + ([f"R{child_number % 7 + 1}"] if child_number % 5 == 0 else [])
            members.append({"n": child_number, "tool_use_id": f"tu-{child_number}", "agent_id": f"agent-{child_number}",
                            "refs": refs, "texts": [], "observations": [], "handoff": None})
            count = OBSERVATIONS[wave][slot]
            members[-1]["texts"] = [(OBS_ZH if (child_number + k) % 2 == 0 else OBS_EN)[(child_number + k) % 3] for k in range(count)]
        # one message: all launches in parallel (every PreToolUse first, then the acknowledgements)
        with controller.message(f"wave {wave + 1}: {size} parallel background launches"):
            for member in members:
                member["handoff"] = host.dispatch(member["tool_use_id"], member["refs"], "C1")
                controller.child_activity()
            for member in members:
                host.launched(member["tool_use_id"], member["agent_id"])
        controller.absorb()
        for member in members:
            host.start(member["agent_id"], member["tool_use_id"])
            controller.child_activity()
        early = EARLY_OBSERVERS[wave]
        agents = {m["agent_id"]: (m["agent_id"], "thaliris:investigator") for m in members}

        def observe(member):
            if member["texts"]:
                controller.child_observe(agents[member["agent_id"]], member["texts"], member)
                member["observations"] = [f"{member['handoff']}.O{n}" for n in range(1, len(member["texts"]) + 1)]
                controller.child_activity()
                for number, text in enumerate(member["texts"], 1):
                    controller.expected["observations"][f"{member['handoff']}.O{number}"] = {
                        "text": text, "handoff": member["handoff"], "refs": member["refs"], "candidate": "C1"}

        for member in members[:early]:
            observe(member)
        if wave in UPDATE_AFTER_WAVE:  # the Controller records the wave while the children are running
            controller.update(UPDATE_AFTER_WAVE[wave])
        for member in members[:early]:
            host.stop(member["agent_id"])
        for member in members[early:]:
            observe(member)
            host.stop(member["agent_id"])
            controller.child_activity()
        handoffs.extend(members)
        host.prompt(f"<task-notification> wave {wave + 1} finished")  # the notification turn
        controller.absorb()
        decide(controller, wave, handoffs, check)
    decide(controller, 4, handoffs, check)
    decide(controller, 5, handoffs, check)
    check["pre_close_probe"] = controller.stale_probe()
    if check["pre_close_probe"][1].get("ok") is not False:
        check["pre_close_probe_wrote"] = True
    closed = controller.close()
    check["closed"] = closed
    return {"handoffs": handoffs}


def decide(controller: Controller, index: int, handoffs: list[dict], check: dict) -> None:
    controller.status(f"status {index + 1}")  # the explicit status read before a broad decision
    controller.judge_point(index, handoffs)
    if index in UPDATE_AFTER_POINT:
        controller.update(UPDATE_AFTER_POINT[index])


# ----------------------------------------------------------------------------------------------------- correctness

def verify(controller: Controller) -> dict:
    """Read the ledger back (unmetered) and compare it with what the Controller and the children wrote."""
    host, expected = controller.host, controller.expected
    problems: list[str] = []
    explicit = ["--task-id", controller.task_id, "--root", _quote(host.repo.as_posix())]  # the task is closed: read it explicitly
    stored_obs = 0
    for obs_id, want in expected["observations"].items():
        code, got = host.run("get", "get", obs_id, *explicit, metered=False)
        obj = got.get("object") or {}
        if not got.get("ok", True) or not obj:
            problems.append(f"observation {obs_id} missing: {str(got)[:120]}")
            continue
        stored_obs += 1
        if obj.get("handoff_id") != want["handoff"]:
            problems.append(f"{obs_id}: handoff {obj.get('handoff_id')} != {want['handoff']}")
        if obj.get("text") != want["text"]:
            problems.append(f"{obs_id}: text differs")
        if sorted(obj.get("requirement_refs") or []) != sorted(want["refs"]):
            problems.append(f"{obs_id}: requirement refs {obj.get('requirement_refs')} != {want['refs']}")
    stored_judgments = 0
    for judgment_id, want in expected["judgments"].items():
        code, got = host.run("get", "get", judgment_id, *explicit, metered=False)
        obj = got.get("object") or {}
        if not obj:
            problems.append(f"judgment {judgment_id} missing")
            continue
        stored_judgments += 1
        ref = obj.get("requirement_ref") or obj.get("requirement")
        if (obj.get("outcome") != want["outcome"] or ref != want["requirement"] or obj.get("candidate") != want["candidate"]
                or sorted(obj.get("observation_refs") or []) != sorted(want["refs"])):
            problems.append(f"{judgment_id}: stored fields differ from what was sent")
    stored_records = 0
    for record_id, text in expected["records"].items():
        code, got = host.run("get", "get", record_id, *explicit, metered=False)
        obj = got.get("object") or {}
        if obj.get("text") == text:
            stored_records += 1
        else:
            problems.append(f"record {record_id} missing or text differs")
    code, status = host.run("status", "status", *explicit, metered=False)
    counts = status.get("Counts") or {}
    if "observations" in counts and counts["observations"] != len(expected["observations"]):  # only newer status views count them
        problems.append(f"ledger counts {counts['observations']} observations, sent {len(expected['observations'])}")
    acks = controller.acks
    monotonic = all(b > a for a, b in zip(acks, acks[1:]))
    if not monotonic:
        problems.append("a write acknowledgement did not advance the revision")
    return {"observations_expected": len(expected["observations"]), "observations_stored_correctly": stored_obs,
            "judgments_expected": len(expected["judgments"]), "judgments_stored_correctly": stored_judgments,
            "records_expected": len(expected["records"]), "records_stored_correctly": stored_records,
            "ledger_counts": counts, "acks_monotonic": monotonic, "problems": problems,
            "ok": not problems and stored_obs == len(expected["observations"]) == 12 and stored_judgments == 33}


def probe_stale_and_close(plugin: Path, workdir: Path, python: str, entry: str, utf8: bool, make_controller) -> dict:
    """Two behaviours worth proving in each tree: a stale write never lands; close refuses while a dispatch is unresolved."""
    result: dict = {}
    repo = workdir / "probe-repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    meter = Meter()
    host = Host(plugin, workdir / "probe", repo, python, meter, PROBE_SESSION, entry, utf8)
    controller = make_controller(host)
    controller.admission()
    controller.update(0)
    before = host.run("status", "status", metered=False)[1]["Task"]["revision"]
    code, refused = controller.stale_probe()
    after = host.run("status", "status", metered=False)[1]["Task"]["revision"]
    result["stale_write_refused"] = refused.get("ok") is False and Controller.is_conflict(refused)
    result["stale_write_left_ledger_unchanged"] = before == after
    handoff = host.dispatch("probe-tu", ["R1"], "C1")
    host.launched("probe-tu", "probe-agent")
    host.start("probe-agent", "probe-tu")
    controller.learn(host.run("status", "status", metered=False)[1]["Task"]["revision"])
    code, early = host.run("close", "close", "--base-revision", str(controller.known_rev), metered=False)
    result["close_refused_with_open_dispatch"] = early.get("ok") is False
    result["close_refusal"] = _error_name(early) if early.get("ok") is False else None
    code, disposed = host.run("dispose", "dispose", "--handoff-id", handoff, "--reason", _quote("benchmark probe"), metered=False)
    controller.learn(host.run("status", "status", metered=False)[1]["Task"]["revision"])
    code, closed = host.run("close", "close", "--base-revision", str(controller.known_rev), metered=False)
    result["close_after_dispose_ok"] = closed.get("ok") is not False
    return result


# ----------------------------------------------------------------------------------------------------- running variants

def prepare_old(old: Path | None, scratch: Path) -> Path:
    if old:
        return old.resolve()
    target = scratch / "old-tree"
    target.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", OLD_COMMIT], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(target)
    return target


def new_repo(base: Path, ascii_path: bool) -> Path:
    repo = (base / "proj" / "login-fix") if ascii_path else (base / "项目 仓库" / "登录修复")
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return repo


def run_variant(name: str, plugin: Path, kind: str, policy: str, old_update: str, python: str, entry: str, scratch: Path,
                utf8: bool = False) -> dict:
    """Run one variant. A tree that cannot admit the task in the Chinese path is replayed again in an ASCII path."""
    attempts = []
    for ascii_path in (False, True):
        workdir = scratch / f"{name}-{'ascii' if ascii_path else 'zh'}"
        workdir.mkdir(parents=True)
        repo = new_repo(workdir, ascii_path)
        meter = Meter()
        host = Host(plugin, workdir, repo, python, meter, SESSION, entry, utf8)
        cls = OldController if kind == "old" else NewController
        controller = cls(host, policy, old_update, workdir / "payloads")
        check: dict = {}
        started = time.perf_counter()
        try:
            run_workload(controller, check)
            failure = None
        except StepFailed as exc:
            failure = {"step": exc.label, "error": _error_name(exc.result), "message": str(exc.result)[:300]}
        elapsed = time.perf_counter() - started
        record = {"path": "ascii" if ascii_path else "chinese+space", "failure": failure, "summary": meter.summary(),
                  "elapsed_s": elapsed, "messages": meter.messages}
        if failure is None:
            record["correctness"] = verify(controller)
            record["correctness"]["pre_close_stale_write_refused"] = (
                Controller.is_conflict(check.get("pre_close_probe", (0, {}))[1]) and "pre_close_probe_wrote" not in check)
            record["correctness"]["closed"] = check.get("closed", {}).get("status") in ("DONE", None) and check.get("closed", {}).get("ok", True)
            probe_root = workdir / "probe-root"
            probe_root.mkdir()
            record["probes"] = probe_stale_and_close(plugin, probe_root, python, entry, utf8, lambda h: cls(h, policy, old_update, probe_root / "payloads"))
        attempts.append(record)
        if failure is None:
            break
    final = attempts[-1]
    return {"name": name, "tree": str(plugin), "attempts": attempts, "result": final,
            "chinese_path_failure": attempts[0]["failure"] if attempts[0]["failure"] else None}


# ----------------------------------------------------------------------------------------------------- reporting

def pct(old: float, new: float) -> str:
    if old == 0:
        return "n/a" if new == 0 else "+inf"
    return f"{(new - old) / old * 100:+.0f}%"


def fmt_bytes(value: int) -> str:
    return f"{value:,}"


def variant_ok(result: dict) -> bool:
    c, p = result.get("correctness") or {}, result.get("probes") or {}
    return bool(c.get("ok") and c.get("closed") and c.get("pre_close_stale_write_refused") and p.get("stale_write_refused")
                and p.get("stale_write_left_ledger_unchanged") and p.get("close_refused_with_open_dispatch") and p.get("close_after_dispose_ok"))


def render(results: dict, args) -> str:
    """The Markdown report for ``results['variants']`` (OLD-A, OLD-B, NEW and optional OLD-*/other-form variants)."""
    variants = results["variants"]
    main = {key: value for key, value in variants.items() if "/" not in key}
    base = {key: value["result"]["summary"] for key, value in main.items()}
    new = base["NEW"]
    head = "| metric | " + " | ".join(main) + " | NEW vs OLD-A | NEW vs OLD-B |"
    lines = [head, "|---|" + "---:|" * (len(main) + 2)]

    def line(label, getter, fmt=str, delta=True):
        cells = [fmt(getter(base[key])) for key in main]
        extra = [pct(getter(base["OLD-A"]), getter(new)), pct(getter(base["OLD-B"]), getter(new))] if delta else ["", ""]
        lines.append(f"| {label} | " + " | ".join(cells) + " | " + " | ".join(extra) + " |")

    def seconds(value):
        return f"{value:.1f}"

    line("Controller runner calls", lambda s: s["controller_runner_calls"])
    line("Controller Write calls (file payloads)", lambda s: s["controller_write_calls"])
    line("**Controller tool calls (runner + Write)**", lambda s: s["controller_tool_calls"])
    line("Agent launches (identical)", lambda s: s["agent_launches"], delta=False)
    line("**sequential model-tool round trips**", lambda s: s["round_trips"])
    line("runner stdout bytes (Controller)", lambda s: s["runner_stdout_bytes"], fmt_bytes)
    line("hook additionalContext bytes (all four events)", lambda s: s["model_ctx_bytes"], fmt_bytes)
    for event in MODEL_EVENTS:
        line(f"&nbsp;&nbsp;{event}", lambda s, e=event: s["model_ctx_by_event"].get(e, 0), fmt_bytes)
    line("**model-visible bytes (stdout + hook ctx)**", lambda s: s["runner_stdout_bytes"] + s["model_ctx_bytes"], fmt_bytes)
    line("**approx tokens (bytes/4, CJK undercount)**", lambda s: s["approx_tokens"], fmt_bytes)
    line("Controller-visible bytes (excl. SubagentStart)", lambda s: s["controller_visible_bytes"], fmt_bytes)
    line("child runner stdout bytes (not Controller)", lambda s: s["child_runner_stdout_bytes"], fmt_bytes)
    line("errors (replayed run)", lambda s: s["error_count"])
    line("Controller-authored bytes (commands + files)", lambda s: s["controller_authored_bytes"], fmt_bytes)
    line("**mechanical wall s (runner + hooks, summed)**", lambda s: s["wall"]["total_s"], seconds)
    line("&nbsp;&nbsp;runner subprocesses s", lambda s: s["wall"]["runner_s"], seconds)
    line("&nbsp;&nbsp;hook subprocesses s", lambda s: s["wall"]["hook_s"], seconds)
    line("&nbsp;&nbsp;Controller-attributed s", lambda s: s["wall"]["controller_s"], seconds)
    line("hook invocations", lambda s: s["hook_calls"])
    zh = {key: value["chinese_path_failure"] for key, value in main.items()}
    lines.append("| Chinese+space Git path admission | " + " | ".join(
        f"FAILED at {zh[key]['step']} ({zh[key]['error']})" if zh[key] else "ok" for key in main) + " | | |")
    lines.append("| errors incl. the Chinese-path failure | " + " | ".join(
        str(base[key]["error_count"] + (1 if zh[key] else 0)) for key in main) + " | | |")
    text = ["\n".join(lines), ""]
    text.append("Errors by code (replayed run): " + "; ".join(f"{key} {json.dumps(base[key]['errors'], ensure_ascii=False)}" for key in main))
    text.append("")
    text.append("Runner calls by subcommand: " + "; ".join(f"{key} {json.dumps(base[key]['runner_by_subcommand'])}" for key in main))
    text.append("")
    if any(len(value.get("wall_total_s_all_runs", [])) > 1 for value in main.values()):
        text.append("Mechanical wall s, every repetition: " + "; ".join(
            f"{key} {[round(v, 1) for v in value['wall_total_s_all_runs']]}" for key, value in main.items()))
        text.append("")
    saved = {}
    for key in ("OLD-A", "OLD-B"):
        trips = base[key]["round_trips"] - new["round_trips"]
        calls = base[key]["controller_tool_calls"] - new["controller_tool_calls"]
        mech = base[key]["wall"]["total_s"] - new["wall"]["total_s"]
        saved[key] = {"round_trips_saved": trips, "model_reasoning_s": trips * args.model_gap, "tool_calls_saved": calls,
                      "host_tool_wait_s": calls * args.tool_wait, "mechanical_s": mech}
        text.append(f"{key} to NEW: {trips} round trips saved x {args.model_gap} s median model gap = {trips * args.model_gap:.0f} s model "
                    f"reasoning/turnaround (estimate). Separately, {calls} Controller tool calls saved x {args.tool_wait} s median "
                    f"Host/tool wait = {calls * args.tool_wait:.0f} s (estimate). Measured mechanical runner+hook time difference "
                    f"{mech:+.1f} s (summed, not overlapped).")
    results["estimates"] = saved
    extra = {key: value["result"]["summary"] for key, value in variants.items() if "/" in key}
    if extra:
        text += ["", "Sensitivity (OLD task-update through the other payload form):", "",
                 "| variant | tool calls | Write calls | round trips | runner stdout B | hook ctx B | errors | wall s |", "|---|---:|---:|---:|---:|---:|---:|---:|"]
        for key, s in extra.items():
            text.append(f"| {key} | {s['controller_tool_calls']} | {s['controller_write_calls']} | {s['round_trips']} | "
                        f"{fmt_bytes(s['runner_stdout_bytes'])} | {fmt_bytes(s['model_ctx_bytes'])} | {s['error_count']} | {s['wall']['total_s']:.1f} |")
    text += ["", "Correctness:"]
    for key, value in variants.items():
        res = value["result"]
        c, p = res.get("correctness") or {}, res.get("probes") or {}
        text.append(f"- {key}: observations {c.get('observations_stored_correctly')}/{c.get('observations_expected')}, judgments "
                    f"{c.get('judgments_stored_correctly')}/{c.get('judgments_expected')}, records {c.get('records_stored_correctly')}/"
                    f"{c.get('records_expected')}, acks monotonic {c.get('acks_monotonic')}, close ok {c.get('closed')}, stale write refused "
                    f"before close {c.get('pre_close_stale_write_refused')}; probe project: stale write refused {p.get('stale_write_refused')}, "
                    f"ledger unchanged {p.get('stale_write_left_ledger_unchanged')}, close with open dispatch refused "
                    f"{p.get('close_refused_with_open_dispatch')} ({p.get('close_refusal')}), close after dispose ok {p.get('close_after_dispose_ok')}"
                    + (f"; PROBLEMS {c['problems']}" if c.get("problems") else ""))
    return "\n".join(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--old", type=Path, help=f"OLD plugin tree (default: git archive of {OLD_COMMIT} from this checkout)")
    parser.add_argument("--new", type=Path, default=ROOT, help="NEW plugin tree (default: this checkout)")
    parser.add_argument("--python", default=sys.executable, help="Python >= 3.11 used for the runner and hooks")
    parser.add_argument("--hook-entry", choices=("sh", "launcher"), default="sh",
                        help="sh: bin/thaliris-hook as hooks.json runs it (default); launcher: bin/thaliris.py hook EVENT")
    parser.add_argument("--utf8", action="store_true", help="set PYTHONUTF8=1 for every subprocess (default: not set, as a Host does)")
    parser.add_argument("--old-update", choices=("file", "inline"), default="file",
                        help="OLD task-update through a hand-written JSON file (guidance; +1 Write) or inline --json")
    parser.add_argument("--repeat", type=int, default=1, help="repeat each variant; counts are deterministic, times use the median run")
    parser.add_argument("--sensitivity", action="store_true", help="also run OLD with the other task-update payload form")
    parser.add_argument("--model-gap", type=float, default=MODEL_GAP_S, help="seconds per saved round trip (real-session median)")
    parser.add_argument("--tool-wait", type=float, default=TOOL_WAIT_S, help="seconds per saved runner call (real-session median)")
    parser.add_argument("--scratch", type=Path, help="working directory (default: a new temp directory, removed afterwards)")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    parser.add_argument("--out", type=Path, help="write raw results as JSON")
    parser.add_argument("--md", type=Path, help="write the report as Markdown")
    parser.add_argument("--report-only", type=Path, help="re-render the report from a raw results JSON, run nothing")
    args = parser.parse_args(argv)
    if args.report_only:
        results = json.loads(args.report_only.read_text(encoding="utf-8"))
        text = render(results, args)
        print(text)
        if args.md:
            args.md.write_text(text + "\n", encoding="utf-8")
        return 0
    if sys.version_info < (3, 11) and args.python == sys.executable:
        raise SystemExit("Python >= 3.11 required")

    scratch = args.scratch or Path(tempfile.mkdtemp(prefix="ctrlbench-"))
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        old_tree = prepare_old(args.old, scratch)
        plans = [("OLD-A", old_tree, "old", "observed", args.old_update), ("OLD-B", old_tree, "old", "careful", args.old_update),
                 ("NEW", args.new.resolve(), "new", "observed", "file")]
        if args.sensitivity:
            other = "inline" if args.old_update == "file" else "file"
            plans += [(f"OLD-A/{other}", old_tree, "old", "observed", other), (f"OLD-B/{other}", old_tree, "old", "careful", other)]
        results: dict = {"old_commit": OLD_COMMIT, "hook_entry": args.hook_entry, "pythonutf8": args.utf8, "python": args.python,
                         "repeat": args.repeat, "variants": {}}
        for name, tree, kind, policy, old_update in plans:
            runs = []
            for number in range(args.repeat):
                print(f"[{name}] run {number + 1}/{args.repeat} ...", file=sys.stderr, flush=True)
                runs.append(run_variant(f"{name.replace('/', '-')}-{number}", tree, kind, policy, old_update, args.python,
                                        args.hook_entry, scratch, args.utf8))
            keep = sorted(runs, key=lambda r: r["result"]["summary"]["wall"]["total_s"])[len(runs) // 2]
            keep["wall_total_s_all_runs"] = [r["result"]["summary"]["wall"]["total_s"] for r in runs]
            keep["name"] = name
            results["variants"][name] = keep
        text = render(results, args)
        print(text)
        if args.out:
            args.out.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        if args.md:
            args.md.write_text(text + "\n", encoding="utf-8")
        failed = [n for n, v in results["variants"].items() if not variant_ok(v["result"])]
        return 1 if failed else 0
    finally:
        if not args.keep and not args.scratch:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
