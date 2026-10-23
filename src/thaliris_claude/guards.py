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
                                 "close", "artifact", "recover-authority", "register-role"})
# Curator may also maintain the selected durable documents.
CURATOR_COMMANDS = frozenset({"promote", "document-maintain"})
IDENTITY_FLAGS = ("--native-session", "--native-agent")
# Runner subcommands that never write: safe to approve even in plan mode.
READ_ONLY_COMMANDS = frozenset({"guidance", "status", "get", "catalog", "document-get", "version", "roles"})

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
_PROTECTED_MARKERS = (".context", ".claude/settings", ".thaliris", "claude-lifecycle.json")


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


def runner_invocation(command: str, runner: str) -> dict | None:
    """Recognize the Thaliris runner; return its subcommand and whether it is direct.

    A direct invocation is optional interpreter tokens, the runner path and
    plain arguments: no operators, redirections, expansions or wrappers.
    """
    parts = tokens(command.strip().removeprefix("& ").removeprefix("&"))
    if parts is None:
        return {"direct": False, "subcommand": None} if norm_path(runner) in norm_path(command) else None
    expected = norm_path(runner)
    indexes = [index for index, token in enumerate(parts) if norm_path(token) == expected]
    if not indexes:
        return None
    index = indexes[0]
    rest = parts[index + 1:]
    # Only an interpreter may precede the runner: `python`, `py -3`, or an absolute path to one.
    prefix_ok = all(re.fullmatch(r"-3(\.\d+)?", item) or re.fullmatch(r"(py|python(3(\.\d+)?)?)(\.exe)?",
                    item.replace("\\", "/").rsplit("/", 1)[-1], re.IGNORECASE) for item in parts[:index])
    direct = (len(indexes) == 1 and prefix_ok and not any(_is_operator(item) for item in parts)
              and not dynamic_expansion(command))
    subcommand = next((item for item in rest if not item.startswith("-")), None)
    return {"direct": direct, "subcommand": subcommand, "forged_identity": any(item.split("=", 1)[0] in IDENTITY_FLAGS for item in rest)}


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


def protected_target(root: Path, target: str, *, controller: bool) -> bool:
    """File-tool target check. Children may not touch Controller/security state;
    the Controller may not hand-edit the ledger or lifecycle (it uses the runner)."""
    try:
        path = Path(target)
        path = (path if path.is_absolute() else root / path).resolve()
        relative = path.relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        relative = None
    if relative is None:
        thaliris_home = (Path.home() / ".thaliris").resolve()
        try:
            Path(target).resolve().relative_to(thaliris_home)
            return True
        except (ValueError, OSError):
            return False
    if controller:
        return relative in {".context/state.json", ".context/claude-roles.json"} or relative.startswith((".context/tasks/", ".context/backups/"))
    return relative == ".context" or relative.startswith(".context/") or relative in {".claude/settings.json", ".claude/settings.local.json"}
