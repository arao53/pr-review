"""Thin wrappers around the git CLI. Everything reads from git objects, not the
working tree, so results depend only on the two commits being compared."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(root: str | Path, *args: str, check: bool = True) -> bytes:
    proc = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=str(root),
        capture_output=True,
    )
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {err}")
    return proc.stdout


def git_text(root: str | Path, *args: str, check: bool = True) -> str:
    return git(root, *args, check=check).decode("utf-8", "replace")


def repo_root(cwd: str | Path) -> Path:
    try:
        return Path(git_text(cwd, "rev-parse", "--show-toplevel").strip())
    except GitError as exc:
        raise GitError(f"{cwd} is not inside a git repository") from exc


def try_rev_parse(root: Path, ref: str) -> str | None:
    out = git_text(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).strip()
    return out or None


def rev_parse(root: Path, ref: str) -> str:
    sha = try_rev_parse(root, ref)
    if not sha:
        raise GitError(f"cannot resolve ref '{ref}'")
    return sha


def current_branch(root: Path) -> str | None:
    out = git_text(root, "rev-parse", "--abbrev-ref", "HEAD", check=False).strip()
    return None if out in ("", "HEAD") else out


def default_base(root: Path) -> str:
    """Pick the branch a PR would usually target: origin/HEAD, then main/master."""
    candidates = []
    origin_head = git_text(root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", check=False).strip()
    if origin_head:
        candidates.append(origin_head)
    candidates += ["origin/main", "origin/master", "main", "master"]
    for ref in candidates:
        if try_rev_parse(root, ref):
            return ref
    raise GitError("could not find a base branch (tried origin/HEAD, main, master); pass --base")


def merge_base(root: Path, a: str, b: str) -> str:
    out = git_text(root, "merge-base", a, b, check=False).strip()
    if not out:
        raise GitError(f"{a[:10]} and {b[:10]} have no common ancestor")
    return out


@dataclass
class Change:
    status: str  # A, M, D, R, T
    path: str
    old_path: str | None
    similarity: int | None
    added: int
    removed: int
    binary: bool


def changed_files(root: Path, base: str, head: str) -> list[Change]:
    raw = git(root, "diff", "--name-status", "-M", "-z", base, head).decode("utf-8", "replace")
    toks = raw.split("\0")
    entries: list[tuple[str, str, str | None, int | None]] = []
    i = 0
    while i < len(toks):
        st = toks[i]
        if not st:
            i += 1
            continue
        code = st[0]
        if code in "RC":
            sim = int(st[1:]) if st[1:].isdigit() else 100
            entries.append(("R", toks[i + 2], toks[i + 1], sim))
            i += 3
        else:
            entries.append((code if code in "AMDT" else "M", toks[i + 1], None, None))
            i += 2

    stats = _numstat(root, base, head)
    out = []
    for status, path, old, sim in entries:
        a, d = stats.get(path, ("0", "0"))
        binary = a == "-" or d == "-"
        out.append(Change(status, path, old, sim, 0 if binary else int(a), 0 if binary else int(d), binary))
    return out


def _numstat(root: Path, base: str, head: str) -> dict[str, tuple[str, str]]:
    raw = git(root, "diff", "--numstat", "-M", "-z", base, head).decode("utf-8", "replace")
    toks = raw.split("\0")
    stats: dict[str, tuple[str, str]] = {}
    i = 0
    while i < len(toks):
        t = toks[i]
        if not t:
            i += 1
            continue
        parts = t.split("\t", 2)
        if len(parts) != 3:
            i += 1
            continue
        a, d, p = parts
        if p == "":  # rename: old and new paths follow as separate tokens
            stats[toks[i + 2]] = (a, d)
            i += 3
        else:
            stats[p] = (a, d)
            i += 1
    return stats


def file_diff(root: Path, base: str, head: str, path: str, old_path: str | None = None) -> str:
    paths = [path] if not old_path or old_path == path else [old_path, path]
    return git_text(root, "diff", "-M", "--no-color", "-U3", base, head, "--", *paths)


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff: str) -> list[int]:
    """Line numbers (in the new file) of every added line in a unified diff."""
    out: list[int] = []
    new_ln = 0
    in_hunk = False
    for line in diff.splitlines():
        m = _HUNK.match(line)
        if m:
            new_ln = int(m.group(1))
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            out.append(new_ln)
            new_ln += 1
        elif line.startswith(" "):
            new_ln += 1
        elif line.startswith("\\"):
            continue
        elif line.startswith("-"):
            continue
        else:
            in_hunk = False
    return out


def list_files(root: Path, rev: str) -> list[str]:
    raw = git(root, "ls-tree", "-r", "-z", "--name-only", rev).decode("utf-8", "replace")
    return [p for p in raw.split("\0") if p]


class BlobReader:
    """Reads many blobs through one `git cat-file --batch` process."""

    def __init__(self, root: Path, max_bytes: int = 1_000_000):
        self.max_bytes = max_bytes
        self.proc = subprocess.Popen(
            ["git", "cat-file", "--batch"],
            cwd=str(root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def __enter__(self) -> "BlobReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)

    def read(self, rev: str, path: str) -> bytes | None:
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(f"{rev}:{path}\n".encode())
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().decode("utf-8", "replace").strip()
        parts = header.split()
        if len(parts) != 3 or parts[-1] == "missing":
            return None
        _, kind, size_s = parts
        size = int(size_s)
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)  # trailing newline
        if kind != "blob" or size > self.max_bytes:
            return None
        return data

    def read_text(self, rev: str, path: str) -> str | None:
        data = self.read(rev, path)
        if data is None or b"\0" in data[:8000]:
            return None
        return data.decode("utf-8", "replace")


_HUNK_FULL = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_TRAILING_COMMA = re.compile(r",(?=[)\]}])")


def _normalize(lines: list[str]) -> str:
    """Strip whitespace, comments and trailing commas so reformatting compares equal."""
    out = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("#"):
            continue
        if "#" in line and "'" not in line and '"' not in line:
            line = line.split("#", 1)[0]
        out.append("".join(line.split()))
    return _TRAILING_COMMA.sub("", "".join(out))


def hunks(diff: str) -> list[dict]:
    """Hunks of a unified diff. `trivial` means the hunk only changes
    whitespace, line breaks, comments or trailing commas."""
    out: list[dict] = []
    cur = None
    for line in diff.splitlines():
        m = _HUNK_FULL.match(line)
        if m:
            cur = {"old_start": int(m.group(1)), "old_len": int(m.group(2) or 1),
                   "new_start": int(m.group(3)), "new_len": int(m.group(4) or 1),
                   "_add": [], "_del": []}
            out.append(cur)
            continue
        if cur is None:
            continue
        if line.startswith("+"):
            cur["_add"].append(line[1:])
        elif line.startswith("-"):
            cur["_del"].append(line[1:])
    for h in out:
        add, rem = h.pop("_add"), h.pop("_del")
        h["added"], h["removed"] = len(add), len(rem)
        h["trivial"] = _normalize(add) == _normalize(rem)
        h["symbols"] = []
    return out
