from __future__ import annotations

from contextlib import contextmanager
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

# Test what ships: the adapter source and the vendored, pinned Core come first.
_PLUGIN = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(_PLUGIN / "src"), str(_PLUGIN / "vendor")]

from thaliris_claude import cli, hooks  # noqa: E402

SESSION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def thaliris_home(tmp_path, monkeypatch):
    """Keep every test off the real ~/.thaliris Core home."""
    location = tmp_path / "thaliris-home"
    monkeypatch.setenv("THALIRIS_HOME", str(location))
    return location


@pytest.fixture
def home(tmp_path, monkeypatch):
    location = tmp_path / "home"
    monkeypatch.setenv("THALIRIS_CLAUDE_HOME", str(location))
    return location


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return root.resolve()


def run_cli(*args: str, session: str | None = SESSION, agent: str | None = None) -> tuple[int, dict]:
    argv = list(args)
    if session:
        argv += ["--native-session", session]
    if agent:
        argv += ["--native-agent", agent]
    buffer = io.StringIO()
    previous = sys.stdout
    sys.stdout = buffer
    try:
        code = cli.main(argv)
    finally:
        sys.stdout = previous
    return code, json.loads(buffer.getvalue())


def run_hook(event: str, payload: dict) -> dict | None:
    stdin, stdout = sys.stdin, sys.stdout
    sys.stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()), encoding="utf-8")
    sys.stdout = io.StringIO()
    try:
        assert hooks.main(event) == 0
        text = sys.stdout.getvalue()
    finally:
        sys.stdin, sys.stdout = stdin, stdout
    return json.loads(text) if text else None


@pytest.fixture
def task(home, repo, tmp_path, monkeypatch):
    """An ACTIVE delegated task associated with SESSION, plus requirement R1."""
    monkeypatch.chdir(repo)
    with _memoized_git_root():
        return _start_task(repo, tmp_path)


@contextmanager
def _memoized_git_root():
    """Only while the fixture builds the task: the repository layout cannot change meanwhile, and Core asks
    ``git rev-parse`` for the same directory about eight times (each a process spawn). Tests themselves run unpatched."""
    from thaliris import core

    original, seen = core._git_root, {}

    def cached(root):
        key = Path(root)
        if key not in seen:
            seen[key] = original(root)
        return seen[key]

    core._git_root = cached
    try:
        yield
    finally:
        core._git_root = original


def _start_task(repo, tmp_path):
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({"human_instruction": "Fix the greeting", "boundary": "app.py only",
                                    "invariants": "keep output ASCII", "acceptance": "prints hello"}), encoding="utf-8")
    code, started = run_cli("task-start", "Fix the greeting", "--contract", str(contract))
    assert code == 0, started
    code, registered = run_cli("register", "--base-revision", str(started["revision"]),
                               "--json", json.dumps({"requirements": [{"id": "R1", "workchain_ref": "greeting"}]}))
    assert code == 0, registered
    return {"root": repo, "task_id": started["task_id"], "revision": registered["revision"], "contract": contract,
            "transcript": str(tmp_path / "transcripts" / f"{SESSION}.jsonl")}
