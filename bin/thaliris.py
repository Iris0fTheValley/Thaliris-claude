"""Thaliris runner launcher for Claude Code.

Kept compatible with old interpreters on purpose: whatever `python` a hook or
Bash call resolves, this file finds a Python >= 3.11 and runs the adapter with
the Core vendored in this plugin (no venv, pip or network). The discovered
interpreter is cached so later calls skip discovery. Without one, hooks
degrade to transparent (SessionStart says how to fix it) and runner calls fail
with a JSON error.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

PLUGIN = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(PLUGIN, "src")
VENDOR = os.path.join(PLUGIN, "vendor")
MINIMUM = (3, 11)
_PROBE = "import sys; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 11) else 1)"


def _home():
    return os.environ.get("THALIRIS_CLAUDE_HOME") or os.path.join(os.path.expanduser("~"), ".thaliris", "claude")


def _cache_path():
    return os.path.join(_home(), "runtime", "python")


def _probe(command):
    try:
        completed = subprocess.run(command + ["-c", _PROBE], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    lines = completed.stdout.decode("utf-8", "replace").strip().splitlines()
    return lines[-1] if completed.returncode == 0 and lines and os.path.isfile(lines[-1]) else None


def _remember(executable):
    try:
        os.makedirs(os.path.dirname(_cache_path()), exist_ok=True)
        with open(_cache_path(), "w", encoding="utf-8") as stream:
            stream.write(executable.replace("\\", "/") + "\n")
    except OSError:
        pass


def _discover():
    """Return a Python >= 3.11, preferring explicit and cached choices."""
    selected = os.environ.get("THALIRIS_CLAUDE_PYTHON")
    if selected:
        return _probe([selected])  # an explicit choice is never silently replaced
    if sys.version_info >= MINIMUM:
        return sys.executable
    try:
        with open(_cache_path(), encoding="utf-8") as stream:
            cached = stream.read().strip()
        if cached and os.path.isfile(cached):
            return cached
    except OSError:
        pass
    candidates = [[os.path.join(PLUGIN, ".venv", "Scripts", "python.exe")], [os.path.join(PLUGIN, ".venv", "bin", "python")]]
    if os.name == "nt":
        candidates += [["py", "-3"]]
    candidates += [["python3.13"], ["python3.12"], ["python3.11"], ["python3"], ["python"]]
    for command in candidates:
        if os.path.isabs(command[0]) and not os.path.isfile(command[0]):
            continue
        found = _probe(command)
        if found:
            _remember(found)
            return found
    try:  # a uv-managed interpreter, if uv is installed
        completed = subprocess.run(["uv", "python", "find", ">=3.11"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=30)
        found = completed.stdout.decode("utf-8", "replace").strip()
        if completed.returncode == 0 and os.path.isfile(found):
            _remember(found)
            return found
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _associated(session_id):
    if not isinstance(session_id, str) or not session_id:
        return False
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return os.path.isfile(os.path.join(_home(), "sessions", key + ".json"))


def _degraded_decision(data):
    """Without a working runtime, ordinary work stays transparent (a broken guard must
    not lock the Host, H057/H088). Only actions that would silently lose their guard
    are refused: a managed Thaliris dispatch and a Reviewer file write."""
    try:
        payload = json.loads(data or b"{}")
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    tool, tool_input = payload.get("tool_name"), payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    reason = None
    if tool in ("Agent", "Task") and str(tool_input.get("subagent_type", "")).startswith("thaliris:") and _associated(payload.get("session_id")):
        reason = "Thaliris runtime is unavailable, so this managed dispatch cannot be reserved or bound. Repair the plugin runtime first; ordinary work is unaffected."
    elif payload.get("agent_type") == "thaliris:reviewer" and tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        reason = "The reviewer role is non-writing (Thaliris runtime unavailable; refusing conservatively)."
    if reason is None:
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}


_REMEDY = ("Install Python 3.11 or newer (python.org, `py` launcher, or `uv python install 3.11`), or set "
           "THALIRIS_CLAUDE_PYTHON to one, then start a new session. Nothing else needs installing: Thaliris Core ships with the plugin.")


def _unavailable(args, detail, data=None):
    if args[:1] == ["hook"]:
        if args[1:2] == ["SessionStart"]:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext":
                "The Thaliris plugin is enabled but cannot run (" + detail + "). Managed task controls are UNKNOWN and "
                "ordinary work is unaffected. Tell the user: " + _REMEDY}}))
        elif args[1:2] == ["PreToolUse"]:
            decision = _degraded_decision(data)
            if decision is not None:
                sys.stdout.write(json.dumps(decision))
        return 0
    sys.stdout.write(json.dumps({"ok": False, "error": "THALIRIS_RUNTIME_UNAVAILABLE: " + detail, "remedy": _REMEDY}) + "\n")
    return 2


def _in_process(args, data):
    sys.path[:0] = [SOURCE, VENDOR]  # the vendored, pinned Core wins over anything installed
    os.environ["THALIRIS_CLAUDE_PLUGIN_ROOT"] = PLUGIN
    os.environ["THALIRIS_CLAUDE_LAUNCHED_AT"] = repr(time.time())
    try:
        from thaliris_claude import cli
    except ImportError as exc:
        return _unavailable(args, "import failed: " + str(exc), data)
    if data is not None:
        import io
        sys.stdin = io.TextIOWrapper(io.BytesIO(data), encoding="utf-8")  # stdin was already consumed
    return cli.main(args)


def main():
    args = sys.argv[1:]
    hook = args[:1] == ["hook"]
    data = sys.stdin.buffer.read() if hook else None
    executable = _discover()
    if executable is None:
        return _unavailable(args, "no Python >= 3.11 found", data)
    if os.path.normcase(os.path.abspath(executable)) == os.path.normcase(os.path.abspath(sys.executable)):
        return _in_process(args, data)
    command = [executable, os.path.abspath(__file__)] + args
    try:
        if not hook:
            return subprocess.run(command).returncode
        completed = subprocess.run(command, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as exc:
        return _unavailable(args, "runtime " + executable + " failed to start: " + str(exc), data)
    if completed.returncode == 0:
        sys.stdout.buffer.write(completed.stdout)  # bytes: an old interpreter's console codec cannot re-encode
        return 0
    # The hook module never exits nonzero, so this runtime is broken.
    sys.stderr.buffer.write(completed.stderr[-2000:])
    return _unavailable(args, "runtime " + executable + " failed", data)


if __name__ == "__main__":
    sys.exit(main())
