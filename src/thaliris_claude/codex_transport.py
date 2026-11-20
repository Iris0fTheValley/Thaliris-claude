"""One Codex app-server turn with a launcher-owned source inspection surface."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from queue import Empty, Full, Queue
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time


SOURCE_TOOLS = (
    ("read_file", "Read a UTF-8 source file with line numbers", {"start_line": {"type": "integer", "minimum": 1}}, ["path"]),
    ("list_files", "List the entries in a repository directory", {}, []),
    ("search_files", "Find a literal string in UTF-8 sources under a file or directory", {"query": {"type": "string"}}, ["query"]),
)
DYNAMIC_TOOLS = [
    {"type": "function", "name": name, "description": description, "deferLoading": False,
     "inputSchema": {"type": "object", "additionalProperties": False, "required": required,
                     "properties": {"path": {"type": "string", "description": "Repository-relative path"}, **properties}}}
    for name, description, properties, required in SOURCE_TOOLS
]
_PROTECTED = frozenset({".git", ".context", ".project-identity", ".thaliris"})
_MAX_SOURCE_BYTES = 512 * 1024
_MAX_RESULT_BYTES = 1024 * 1024


def _source_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or len(value) > 1024 or '\x00' in value or ':' in value:
        raise ValueError("Expected a repository-relative path")
    path = Path(value.replace('\\', '/'))
    if path.is_absolute() or path.anchor or '..' in path.parts:
        raise ValueError("Paths must stay in the repository")
    target = (root / path).resolve(strict=True)
    for relative in (path, target.relative_to(root)):
        parts = tuple(part.casefold() for part in relative.parts)
        if _PROTECTED.intersection(parts) or any(
            part == '.claude' and name in {'settings.json', 'settings.local.json'}
            for part, name in zip(parts, parts[1:])
        ):
            raise ValueError("That path contains protected task, Git or security state")
    return target


def _directory(root: Path, path: Path):
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                yield _source_path(root, Path(entry.path).relative_to(root).as_posix())
            except (OSError, ValueError):
                continue


def _read_lines(path: Path) -> list[str]:
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Expected a regular source file")
    with path.open('rb') as stream:
        data = stream.read(_MAX_SOURCE_BYTES + 1)
    if len(data) > _MAX_SOURCE_BYTES or b'\0' in data:
        raise ValueError("The file is large or binary")
    return data.decode('utf-8-sig').splitlines()


def _search(root: Path, path: Path, query: str, check_live) -> str:
    pending, matches, visited = [path], [], 0
    while pending and visited < 4096 and len(matches) < 100:
        check_live()
        target = pending.pop()
        visited += 1
        if target.is_dir():
            for child in _directory(root, target):
                if visited + len(pending) >= 4096:
                    break
                pending.append(child)
            continue
        try:
            lines = _read_lines(target)
        except (OSError, ValueError):
            continue
        for number, line in enumerate(lines, 1):
            if query in line:
                matches.append(f"{target.relative_to(root).as_posix()}:{number}: {line[:300]}")
                if len(matches) == 100:
                    break
    note = '\nSearch limit reached. Narrow the path.' if pending or len(matches) == 100 else ''
    return ('\n'.join(matches) or 'No matches') + note


def inspect_source(root: Path, tool: object, arguments: object, check_live) -> dict:
    try:
        if not isinstance(arguments, dict) or tool not in ('read_file', 'list_files', 'search_files'):
            raise ValueError("Unknown source inspection tool or invalid arguments")
        properties = next(item['inputSchema']['properties'] for item in DYNAMIC_TOOLS if item['name'] == tool)
        if arguments.keys() - properties.keys():
            raise ValueError("Unknown source inspection arguments")
        path = _source_path(root, arguments.get('path', None if tool == 'read_file' else '.'))
        if tool == 'read_file':
            start = arguments.get('start_line', 1)
            if type(start) is not int or start < 1:
                raise ValueError("start_line must be a positive integer")
            lines = _read_lines(path)
            end = min(start + 199, len(lines))
            text = '\n'.join(f'{number}: {lines[number - 1][:500]}' for number in range(start, end + 1))
            text += f'\nLines {start}–{end} of {len(lines)}.' if text else '\nNo lines in that range.'
        elif tool == 'list_files':
            names = []
            for entry in _directory(root, path):
                names.append(entry.name + ('/' if entry.is_dir() else ''))
                if len(names) == 300:
                    break
            text = '\n'.join(sorted(names)) or 'Directory is empty'
        else:
            query = arguments.get('query')
            if not isinstance(query, str) or not query.strip() or len(query) > 256:
                raise ValueError("query must be a literal string of 1–256 characters")
            text = _search(root, path, query, check_live)
    except (OSError, ValueError) as exc:
        text = str(exc) if type(exc) is ValueError else 'Source path could not be read as UTF-8'
        return {"success": False, "contentItems": [{"type": "inputText", "text": text[:300]}]}
    return {"success": True, "contentItems": [{"type": "inputText", "text": text}]}


def _windows_job(pid_handle):
    """Assign before initialize. Closing the only Job handle kills all descendants."""
    import ctypes as c
    from ctypes import wintypes as w

    class BasicLimits(c.Structure):
        _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong), ('flags', w.DWORD),
                    ('min_working_set', c.c_size_t), ('max_working_set', c.c_size_t), ('active_processes', w.DWORD),
                    ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]

    class ExtendedLimits(c.Structure):
        _fields_ = [('basic', BasicLimits), ('io', c.c_ulonglong * 6), ('process_memory', c.c_size_t),
                    ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]

    kernel = c.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = (w.LPVOID, w.LPCWSTR), w.HANDLE
    kernel.SetInformationJobObject.argtypes = (w.HANDLE, c.c_int, w.LPVOID, w.DWORD)
    kernel.AssignProcessToJobObject.argtypes = (w.HANDLE, w.HANDLE)
    kernel.CloseHandle.argtypes = (w.HANDLE,)
    job = kernel.CreateJobObjectW(None, None)
    if not job:
        raise c.WinError(c.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, pid_handle):
        error = c.get_last_error()
        kernel.CloseHandle(job)
        raise c.WinError(error)
    return lambda: kernel.CloseHandle(job)


_WATCHDOG = "import os, signal, sys; os.read(int(sys.argv[1]), 1); os.killpg(0, signal.SIGKILL)"


@contextmanager
def _process_tree(argv: list[str], root: Path, env: dict):
    # On POSIX a pipe-backed sentinel in the process group covers abrupt launcher death as well as normal cleanup.
    windows = os.name == 'nt'
    proc = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, **({'creationflags': subprocess.CREATE_NO_WINDOW} if windows
                                                        else {'process_group': 0}))
    release, sentinel = None, None
    try:
        if windows:
            release = _windows_job(proc._handle)
        else:
            read_fd, write_fd = os.pipe()
            release = lambda: os.close(write_fd)
            try:
                sentinel = subprocess.Popen([sys.executable, '-I', '-c', _WATCHDOG, str(read_fd)],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                            pass_fds=(read_fd,), process_group=proc.pid)
            finally:
                os.close(read_fd)
        yield proc
    finally:
        if release is not None:
            release()
        if sentinel is not None:
            sentinel.wait(timeout=5)
        elif not windows:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if proc.poll() is None and release is None:
            proc.kill()  # no messages were sent if containment failed
        proc.wait(timeout=5)
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.stdout.close()


class CodexSession:
    def __init__(self, proc, check_live):
        self.proc = proc
        self.check_live = check_live
        self.incoming = Queue(maxsize=64)
        self.pending = []
        self.text = None
        self.usage = {}
        self.tool_ids = set()
        self.thread_id = None
        self.turn_id = None
        self.model = None
        self.effort = None
        self.closed = False
        self.outgoing = Queue(maxsize=1)
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._write, daemon=True).start()

    def _read(self):
        try:
            while line := self.proc.stdout.readline(2 * _MAX_RESULT_BYTES):
                if not line.endswith(b'\n'):
                    break
                message = json.loads(line)
                if (isinstance(message, dict) and 'id' not in message
                        and message.get('method') not in ('item/completed', 'thread/tokenUsage/updated', 'turn/completed')):
                    continue
                self.incoming.put_nowait(message)
        except (ValueError, OSError, TypeError, Full):
            pass
        finally:
            # A full queue must also terminate the consumer when it drains.
            self.closed = True

    def _write(self):
        while True:
            pending = self.outgoing.get()
            if pending is None:
                return
            data, sent, failures = pending
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (OSError, ValueError) as exc:
                failures.append(exc)
            finally:
                sent.set()

    def close(self):
        try:
            self.outgoing.put_nowait(None)
        except Full:
            pass

    def send(self, message: dict):
        self.check_live()
        data = (json.dumps({"jsonrpc": "2.0", **message}, ensure_ascii=True) + '\n').encode('ascii')
        sent, failures = threading.Event(), []
        self.outgoing.put_nowait((data, sent, failures))
        while not sent.wait(timeout=0.3):
            self.check_live()
        if failures:
            raise RuntimeError('Codex app-server input closed') from failures[0]

    def next_message(self) -> dict:
        while True:
            self.check_live()
            try:
                message = self.incoming.get(timeout=0.3)
            except Empty:
                if self.closed:
                    raise RuntimeError("Codex app-server output closed before completion") from None
                continue
            if not isinstance(message, dict):
                raise RuntimeError("Codex app-server returned an invalid message")
            return message

    def call(self, number: int, method: str, params: dict) -> dict:
        self.send({"id": number, "method": method, "params": params})
        while True:
            message = self.next_message()
            if message.get('id') == number and 'method' not in message:
                if not isinstance(message.get('result'), dict):
                    raise RuntimeError(f"Codex app-server refused {method}")
                return message['result']
            if 'id' in message and message.get('method') != 'item/tool/call':
                self.send({"id": message['id'], "error": {"code": -32601, "message": "Request refused by read-only launcher"}})
            elif method == 'turn/start':
                if len(self.pending) >= 256:
                    raise RuntimeError("Codex app-server sent too many events before acknowledging the turn")
                self.pending.append(message)

    def start_thread(self, root: Path, model: str | None, effort: str | None) -> str:
        self.call(1, 'initialize', {"clientInfo": {"name": "thaliris-claude-xhost", "version": "1"},
                                    "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized"})
        config = self.call(2, 'config/read', {"cwd": str(root), "includeLayers": False}).get('config')
        if not isinstance(config, dict) or not isinstance(config.get('mcp_servers', {}), dict):
            raise RuntimeError("Codex app-server did not return readable MCP configuration")
        overrides = {"agents": {"enabled": False}, "web_search": "disabled",
                     "mcp_servers": {name: {"enabled": False} for name in config.get('mcp_servers', {})}}
        if effort is not None:
            overrides['model_reasoning_effort'] = effort
        result = self.call(3, 'thread/start', {"cwd": str(root), "model": model, "sandbox": "read-only",
                                              "approvalPolicy": "never", "config": overrides, "ephemeral": True,
                                              "dynamicTools": DYNAMIC_TOOLS})
        sandbox, thread = result.get('sandbox'), result.get('thread')
        if (result.get('approvalPolicy') != 'never' or not isinstance(sandbox, dict) or sandbox.get('type') != 'readOnly'
                or sandbox.get('networkAccess') not in (None, False) or not isinstance(thread, dict)
                or not isinstance(thread.get('id'), str) or not thread['id']
                or Path(result['cwd']).resolve() != root or (model is not None and model != result.get('model'))
                or (effort is not None and effort != result.get('reasoningEffort'))):
            raise RuntimeError("Codex app-server did not retain the requested read-only thread configuration")
        self.thread_id = thread['id']
        self.model, self.effort = result.get('model'), result.get('reasoningEffort')
        status = self.call(4, 'mcpServerStatus/list', {"threadId": self.thread_id, "detail": "toolsAndAuthOnly", "limit": 1000})
        entries = status.get('data')
        if (not isinstance(entries, list) or status.get('nextCursor') is not None
                or any(not isinstance(item, dict) or item.get('runtimeStatus') != 'disabled' or item.get('tools') for item in entries)
                or not set(overrides['mcp_servers']) <= {item.get('name') for item in entries}):
            raise RuntimeError('Codex app-server could not confirm that every MCP server is disabled')
        return self.thread_id

    def run_turn(self, root: Path, prompt: str) -> str:
        started = self.call(5, 'turn/start', {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt}]})
        turn = started.get('turn')
        if not isinstance(turn, dict) or not isinstance(turn.get('id'), str) or not turn['id']:
            raise RuntimeError('Codex app-server returned an invalid turn acknowledgment')
        self.turn_id = turn['id']
        early, self.pending = iter(self.pending), []
        while True:
            message = next(early, None) or self.next_message()
            params = message.get('params')
            params = params if isinstance(params, dict) else {}
            method = message.get('method')
            if method == 'item/tool/call' and 'id' in message:
                valid = (params.get('threadId') == self.thread_id and params.get('turnId') == self.turn_id
                         and params.get('namespace') is None and isinstance(params.get('callId'), str))
                if valid:
                    self.tool_ids.add(params['callId'])
                result = inspect_source(root, params.get('tool') if valid else None, params.get('arguments'), self.check_live)
                self.send({"id": message['id'], "result": result})
            elif 'id' in message and method:
                self.send({"id": message['id'], "error": {"code": -32601, "message": "Request refused by read-only launcher"}})
            elif params.get('threadId') == self.thread_id:
                outcome = self._turn_notification(method, params)
                if outcome is not None:
                    return outcome

    def _turn_notification(self, method: str, params: dict) -> str | None:
        turn = params.get('turn')
        if method == 'turn/completed' and isinstance(turn, dict) and turn.get('id') == self.turn_id:
            for item in turn.get('items', []):
                self._item(item)
            return 'completed' if turn.get('status') == 'completed' and self.text is not None else 'failed'
        if params.get('turnId') != self.turn_id:
            return None
        if method == 'item/completed':
            self._item(params['item'])
        elif method == 'thread/tokenUsage/updated':
            total = params['tokenUsage']['total']  # one ephemeral thread and exactly one turn
            self.usage = {name: total[key] for name, key in (('input_tokens', 'inputTokens'), ('output_tokens', 'outputTokens'),
                                                           ('total_tokens', 'totalTokens'))
                          if type(total.get(key)) is int and 0 <= total[key] <= 10**12}
        return None

    def _item(self, item: dict):
        if (isinstance(item, dict) and item.get('type') == 'agentMessage'
                and item.get('phase') in (None, 'final_answer') and isinstance(item.get('text'), str)):
            if len(item['text'].encode('utf-8')) > _MAX_RESULT_BYTES:
                raise RuntimeError("Codex final result exceeds the Core body limit")
            self.text = item['text']

    def report(self, began: float) -> dict:
        return {"model": self.model, "effort": self.effort, "cost_basis": "unknown",
                "duration_ms": int((time.monotonic() - began) * 1000), "tool_uses": len(self.tool_ids), **self.usage}


@contextmanager
def connect(root: Path, check_live):
    search = os.pathsep.join(directory for directory in os.get_exec_path() if Path(directory).is_absolute())
    found = shutil.which('codex', path=search)
    executable = Path(found).resolve() if found else None
    if executable is None or executable.is_relative_to(root) or (os.name == 'nt' and executable.suffix.lower() != '.exe'):
        raise RuntimeError("Codex executable is unavailable. Install a compatible native Codex CLI on PATH")
    argv = [str(executable), 'app-server', '--listen', 'stdio://']
    for feature in ('shell_tool', 'unified_exec', 'apps', 'plugins', 'skill_mcp_dependency_install', 'request_permissions_tool'):
        argv.extend(('--disable', feature))
    env = {**os.environ, 'THALIRIS_FOREIGN_CHILD': '1'}
    with _process_tree(argv, root, env) as proc:
        session = CodexSession(proc, check_live)
        try:
            yield session
        finally:
            session.close()
