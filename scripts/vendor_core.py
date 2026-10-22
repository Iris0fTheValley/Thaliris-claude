"""Vendor a pinned Thaliris Core into the plugin, or verify the vendored copy.

Core is standard-library only, so shipping it inside the plugin makes the
runtime requirement "any Python >= 3.11": no venv, no pip, no network at
install time. Bytes come from Git objects at the pinned commit, never from a
working tree, so local edits cannot leak in and the lock is checkable against
history independently of the vendored files (see history H046).

    python scripts/vendor_core.py --source ../../core/Thaliris --commit <sha>
    python scripts/vendor_core.py --check [--source ../../core/Thaliris]
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

PLUGIN = Path(__file__).resolve().parents[1]
VENDOR = PLUGIN / "vendor"
LOCK = VENDOR / "core.lock.json"
PACKAGE = "src/thaliris"
REPOSITORY = "https://github.com/Iris0fTheValley/Thaliris"


def _git(source: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True).stdout


def _committed_files(source: Path, commit: str) -> dict[str, bytes]:
    names = _git(source, "ls-tree", "--name-only", f"{commit}:{PACKAGE}").decode().split()
    return {f"thaliris/{name}": _git(source, "show", f"{commit}:{PACKAGE}/{name}") for name in names if name.endswith(".py")}


def vendor(source: Path, commit: str) -> dict:
    commit = _git(source, "rev-parse", f"{commit}^{{commit}}").decode().strip()
    files = _committed_files(source, commit)
    target = VENDOR / "thaliris"
    for stale in target.glob("*.py") if target.is_dir() else ():
        if f"thaliris/{stale.name}" not in files:
            stale.unlink()
    for name, content in files.items():
        (VENDOR / name).parent.mkdir(parents=True, exist_ok=True)
        (VENDOR / name).write_bytes(content)
    lock = {"repository": REPOSITORY, "commit": commit, "path": PACKAGE,
            "files": {name: hashlib.sha256(content).hexdigest() for name, content in sorted(files.items())}}
    LOCK.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    return lock


def check(source: Path | None) -> list[str]:
    """Return problems: vendored bytes vs lock, and lock vs Git history when available."""
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    problems = []
    vendored = {f"thaliris/{path.name}" for path in (VENDOR / "thaliris").glob("*.py")}
    if vendored != set(lock["files"]):
        problems.append(f"vendored file set differs from lock: {sorted(vendored ^ set(lock['files']))}")
    for name, digest in lock["files"].items():
        path = VENDOR / name
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            problems.append(f"{name} does not match the lock")
    if source is not None:
        committed = _committed_files(source, lock["commit"])
        for name, digest in lock["files"].items():
            if hashlib.sha256(committed.get(name, b"")).hexdigest() != digest:
                problems.append(f"{name} differs from {lock['commit'][:12]}:{PACKAGE}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, help="a Thaliris Core Git checkout")
    parser.add_argument("--commit", help="commit to vendor (requires --source)")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        problems = check(args.source)
        print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
        return 0 if not problems else 1
    if args.source is None or args.commit is None:
        parser.error("vendoring needs --source and --commit")
    print(json.dumps(vendor(args.source, args.commit), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
