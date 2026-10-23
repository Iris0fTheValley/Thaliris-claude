"""Mechanical classification of shell commands and file targets for hooks.

This is an obvious-write guard, not a sandbox: it catches ordinary writes by
ordinary agents and leaves interpreters, unknown tools and shared-OS access
outside its claim. Ambiguity about a known readonly child is refused.
"""
from __future__ import annotations

import os
from pathlib import Path
import posixpath
import re
import shlex

# Subcommands only the owning Controller (main thread) may run.
CONTROLLER_COMMANDS = frozenset({"bootstrap", "task-start", "attach", "detach", "register", "judge", "mode", "dispose",
                                 "close", "artifact", "recover-authority", "register-role", "unregister-role", "task-update",
                                 # Project identity mutations: the Controller's act on the human owner's decision.
                                 "identity-amend", "identity-challenge", "identity-relate"})
# Curator may also maintain the selected durable documents.
CURATOR_COMMANDS = frozenset({"promote", "document-maintain"})
IDENTITY_FLAGS = ("--native-session", "--native-agent")
# Runner subcommands that never write: safe to approve even in plan mode.
READ_ONLY_COMMANDS = frozenset({"guidance", "status", "get", "catalog", "document-get", "version", "roles",
                                "identity-status", "identity-get", "identity-trace"})

_WRITE_PROGRAMS = frozenset({
    "rm", "rmdir", "mv", "cp", "touch", "mkdir", "tee", "truncate", "dd", "ln", "chmod", "chown", "install", "patch", "unlink", "shred",
    "set-content", "add-content", "out-file", "new-item", "remove-item", "move-item", "copy-item", "rename-item", "clear-content",
    "ni", "ri", "mi", "cpi", "rni", "del", "erase", "sc", "ac", "md", "rd",
})
_GIT_WRITES = frozenset({"add", "commit", "checkout", "reset", "restore", "rm", "mv", "merge", "rebase", "cherry-pick", "revert",
                         "stash", "apply", "am", "push", "pull", "clean", "switch", "tag", "worktree", "init", "clone", "fetch", "gc", "prune", "update-index"})
_PACKAGE_WRITES = frozenset({"install", "add", "remove", "uninstall", "update", "upgrade", "sync", "ci", "i"})
_PACKAGE_MANAGERS = frozenset({"npm", "pnpm", "yarn", "pip", "pip3", "uv", "poetry", "cargo", "bun"})
_SAFE_REDIRECT_TARGETS = frozenset({"/dev/null", "nul", "$null", "&1", "&2"})
# Child-write protection only. Deliberately separate from the Task Authority `protected_paths` (store.PROTECTED_PATHS):
# anchoring the project identity there would make a legitimate in-task amendment trip TASK_AUTHORITY_SECURITY_CHANGED.
_PROTECTED_MARKERS = (".context", ".claude/settings", ".thaliris", "claude-lifecycle.json", ".project-identity")


def norm_path(value: str) -> str:
    """Normalize a path token for comparison across Git Bash, PowerShell and Windows."""
    text = value.strip().strip("'\"").replace("\\", "/")
    match = re.match(r"^/([a-zA-Z])/(.*)$", text)
    if match and os.name == "nt":
        text = f"{match.group(1)}:/{match.group(2)}"
    if text:
        # Lexical only (no symlinks): models sometimes spell the same path with `..` segments.
        text = posixpath.normpath(text)
    return text.casefold() if os.name == "nt" else text


def dynamic_expansion(command: str) -> bool:
    """True when backticks or $( are evaluated (outside single quotes)."""
    single = double = False
    index = 0
    while index < len(command):
        char = command[index]
        if char == "\\" and not single and index + 1 < len(command):
            index += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif not single and (char == "`" or command.startswith("$(", index)):
            return True
        index += 1
    return False


def tokens(command: str) -> list[str] | None:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return None


def _is_operator(token: str) -> bool:
    return bool(token) and all(char in "();<>|&" for char in token)


def shell_layout(command: str) -> set[str]:
    """Shell structure `shlex` hides: an unquoted `#` comment and unquoted line breaks.

    `shlex` drops comments and treats a newline as whitespace, so `runner status # note`
    and `runner status<newline>rm x` both tokenize as plain arguments. The shell, though,
    ends the first command at the comment (an appended identity flag would land inside
    it) and runs the second line as a separate command.
    """
    found: set[str] = set()
    text = command.strip()
    single = double = False
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and not single and index + 1 < len(text):
            nxt = text[index + 1]
            if nxt == "\r" and text[index + 2:index + 3] == "\n":
                index += 3  # line continuation
            else:
                index += 2
            continue
        if char == "'" and not double:
            single = not single
        elif char == '"' and not single:
            double = not double
        elif not single and not double:
            if char in "\r\n":
                found.add("newline")
            elif char == "#" and (index == 0 or text[index - 1] in " \t\r\n;&|()<>"):
                found.add("comment")
        index += 1
    return found


def runner_invocation(command: str, runner: str) -> dict | None:
    """Recognize the Thaliris runner; return its subcommand and whether it is direct.

    A direct invocation is optional interpreter tokens, the runner path and
    plain arguments: no operators, redirections, expansions, comments, extra
    lines or wrappers. `problems` names why a recognised invocation is not direct.
    """
    parts = tokens(command.strip().removeprefix("& ").removeprefix("&"))
    if parts is None:
        return {"direct": False, "subcommand": None, "problems": ["unparseable"]} if norm_path(runner) in norm_path(command) else None
    expected = norm_path(runner)
    indexes = [index for index, token in enumerate(parts) if norm_path(token) == expected]
    if not indexes:
        return None
    index = indexes[0]
    rest = parts[index + 1:]
    # Only an interpreter may precede the runner: `python`, `py -3`, or an absolute path to one.
    prefix_ok = all(re.fullmatch(r"-3(\.\d+)?", item) or re.fullmatch(r"(py|python(3(\.\d+)?)?)(\.exe)?",
                    item.replace("\\", "/").rsplit("/", 1)[-1], re.IGNORECASE) for item in parts[:index])
    problems = sorted(shell_layout(command))
    if len(indexes) > 1:
        problems.append("repeated")
    if not prefix_ok:
        problems.append("prefix")
    if any(_is_operator(item) for item in parts):
        problems.append("operator")
    if dynamic_expansion(command):
        problems.append("expansion")
    subcommand = next((item for item in rest if not item.startswith("-")), None)
    result = {"direct": not problems, "subcommand": subcommand,
              "forged_identity": any(item.split("=", 1)[0] in IDENTITY_FLAGS for item in rest)}
    if problems:
        result["problems"] = problems
    return result


_RUNNER_FILE = "/bin/thaliris.py"
_RUNNER_TEXT = re.compile(r"[^\s'\"`;&|<>()]*thaliris\.py", re.IGNORECASE)


def runner_misses(command: str, runner: str) -> list[dict]:
    """Thaliris Claude runner paths a command names that are not this plugin's active runner.

    Only a `<dir>/bin/thaliris.py` inside this plugin root, or beside it in the same plugin
    cache (another installed version), counts as "evidently a Thaliris runner"; any other
    `bin/thaliris.py` (a dev checkout run by tests, say) is not this adapter's concern.
    Each miss is `{"path", "kind"}` with kind `backslash`, `version` or `path`.
    """
    expected = norm_path(runner)
    root = expected[:-len(_RUNNER_FILE)]
    parent = root.rsplit("/", 1)[0] if "/cache/" in root + "/" else None
    candidates = [match.group(0) for match in _RUNNER_TEXT.finditer(command)]
    candidates += [item for item in (tokens(command) or []) if "thaliris.py" in item.casefold()]
    misses: list[dict] = []
    for raw in candidates:
        path = norm_path(raw)
        inside = path.startswith(root + "/")
        beside = parent is not None and path.startswith(parent + "/")
        if not path.endswith(_RUNNER_FILE) or not (inside or beside):
            continue
        if path != expected:
            miss = {"path": raw, "kind": "path" if inside else "version"}
        elif "\\" in raw:
            miss = {"path": raw, "kind": "backslash"}
        else:
            continue
        if miss not in misses:
            misses.append(miss)
    if not misses and "\\" in command:
        # A path with spaces and unquoted backslashes falls apart into fragments, but the whole runner path still shows up.
        def fold(text: str) -> str:
            text = text.replace("\\", "/")
            return text.casefold() if os.name == "nt" else text
        if fold(runner) in fold(command):
            misses.append({"path": runner.replace("/", "\\"), "kind": "backslash"})
    return misses


def _segments(parts: list[str]) -> list[list[str]]:
    segments, current = [], []
    for item in parts:
        if item in {";", "&&", "||", "|", "&", "(", ")"}:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(item)
    if current:
        segments.append(current)
    return segments


def obvious_write(command: str) -> bool:
    """Best-effort detection of shell commands that write files or Git state."""
    if dynamic_expansion(command):
        return True  # cannot see what runs; a readonly child gets the refusal
    parts = tokens(command)
    if parts is None:
        return True
    for index, item in enumerate(parts):
        if item in {">", ">>", ">|", "&>", "&>>"} or re.fullmatch(r"\d?>>?", item):
            target = parts[index + 1] if index + 1 < len(parts) else ""
            if target.casefold() not in _SAFE_REDIRECT_TARGETS and not target.startswith("&"):
                return True
    for segment in _segments(parts):
        words = [word for word in segment if not re.fullmatch(r"\w+=.*", word)]
        if not words:
            continue
        program = Path(words[0].replace("\\", "/")).name.casefold().removesuffix(".exe")
        args = [word.casefold() for word in words[1:]]
        if program in _WRITE_PROGRAMS:
            return True
        if program in {"sed", "perl"} and any(arg.startswith("-i") or arg == "--in-place" for arg in args):
            return True
        if program == "git":
            sub, skip = "", False
            for arg in args:  # global options such as `-C <dir>` take a value
                if skip:
                    skip = False
                elif arg in {"-c", "-C", "--git-dir", "--work-tree", "--namespace"}:
                    skip = True
                elif not arg.startswith("-"):
                    sub = arg
                    break
            if sub in _GIT_WRITES or (sub == "branch" and any(arg in {"-d", "-D", "-m", "-M", "--delete", "--move"} for arg in words[1:])):
                return True
        if program in _PACKAGE_MANAGERS and any(arg in _PACKAGE_WRITES for arg in args[:2]):
            return True
    return False


def mentions_protected(command: str) -> bool:
    text = command.replace("\\", "/").casefold()
    return any(marker.replace("\\", "/").casefold() in text for marker in _PROTECTED_MARKERS)


def direct_core_call(command: str) -> bool:
    """Children must go through the runner, never the raw Core CLI or adapter module."""
    parts = tokens(command) or command.split()
    for index, item in enumerate(parts):
        name = Path(item.replace("\\", "/")).name.casefold().removesuffix(".exe")
        if name == "thaliris-core" or (index and parts[index - 1] == "-m" and item.split(".")[0] in {"thaliris", "thaliris_claude"}):
            return True
    return False


def _folded_relative(path: Path, base: Path) -> str | None:
    """`path` relative to `base`, compared case-insensitively on every platform.

    A case variant of a protected path is the same file on Windows and macOS, and
    `Path.resolve()` does not canonicalize case everywhere (not on macOS), so
    exact matching would let `.CONTEXT/...` through. On a case-sensitive
    filesystem this only refuses paths that collide with a protected one by case.
    """
    folded, prefix = path.as_posix().casefold(), base.as_posix().casefold().rstrip("/")
    if folded == prefix:
        return "."
    return folded[len(prefix) + 1:] if folded.startswith(prefix + "/") else None


def protected_target(root: Path, target: str, *, controller: bool) -> bool:
    """File-tool target check. Children may not touch Controller/security state;
    the Controller may not hand-edit the ledger or lifecycle (it uses the runner)."""
    try:
        path = Path(target)
        path = (path if path.is_absolute() else root / path).resolve()
        relative = _folded_relative(path, root.resolve())
        thaliris_home = (Path.home() / ".thaliris").resolve()
    except (ValueError, OSError):
        return False
    if relative is None:
        return _folded_relative(path, thaliris_home) is not None
    if controller:
        return relative in {".context/state.json", ".context/claude-roles.json"} or relative.startswith((".context/tasks/", ".context/backups/"))
    return (relative == ".context" or relative.startswith(".context/") or relative in {".claude/settings.json", ".claude/settings.local.json"}
            or relative == ".project-identity" or relative.startswith(".project-identity/"))
