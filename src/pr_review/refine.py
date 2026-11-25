"""The accurate usage search: re-check callers of changed symbols with jedi.

The name-based pass in symbols.py cannot follow types, so `obj.method()` on
an object of unknown type is only a guess. jedi infers types, so it confirms
or drops those guesses and finds callers the name pass missed.

The code is read from a clean export of the head commit, never from the
working tree, so local edits don't change the answer.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from pr_review import derive, pyast
from pr_review import symbols as symmod

DEFAULT_TIMEOUT = 300


class RefineUnavailable(RuntimeError):
    pass


def available() -> bool:
    try:
        import jedi  # noqa: F401
        return True
    except ImportError:
        return False


def _export(repo: Path, sha: str, dest: Path) -> None:
    """Write every .py file at `sha` into dest."""
    proc = subprocess.Popen(["git", "archive", "--format=tar", sha], cwd=repo, stdout=subprocess.PIPE)
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
        for m in tar:
            if m.isfile() and m.name.endswith(".py") and not m.name.startswith(("/", "..")) and ".." not in m.name.split("/"):
                tar.extract(m, dest, **({"filter": "data"} if hasattr(tarfile, "data_filter") else {}))
    proc.wait()


def _priority(s: dict) -> tuple:
    # breaking changes first, then anything with uncertain callers, then the rest
    low = sum(1 for c in s["callers"] if c["confidence"] == "low")
    return (not s["breaking"], s["change"] == "added", -low, not s["public"], s["id"])


def refine(review: dict, max_symbols: int | None = None, timeout: float = DEFAULT_TIMEOUT,
           progress=None, repo: str | Path | None = None) -> dict:
    """Replace name-based callers with jedi's for the highest-priority symbols,
    then recompute gaps, tiers and budget. Returns run statistics."""
    try:
        import jedi
    except ImportError as exc:
        raise RefineUnavailable(
            "The accurate search needs the 'jedi' package. Install it with: pip install 'pr-review[refine]'"
        ) from exc

    repo = Path(repo or review["meta"].get("repo_path") or ".")
    started = time.time()
    todo = [s for s in review.get("symbols", [])
            if s["kind"] in ("function", "method", "class") and s["change"] != "removed" and s.get("line")]
    todo.sort(key=_priority)
    if max_symbols:
        todo = todo[:max_symbols]
    stats = {"engine": "jedi", "engine_version": jedi.__version__, "checked": 0, "skipped": 0,
             "confirmed": 0, "added": 0, "dropped": 0, "timed_out": False}

    with tempfile.TemporaryDirectory(prefix="pr-review-") as tmp:
        root = Path(tmp)
        _export(repo, review["meta"]["head_sha"], root)
        extra = [str(root / d) for d in ("src", "lib") if (root / d).is_dir()]
        project = jedi.Project(str(root), added_sys_path=extra, smart_sys_path=True, load_unsafe_extensions=False)
        span_cache: dict[str, list] = {}

        def spans_for(rel: str):
            if rel not in span_cache:
                try:
                    tree = pyast.parse((root / rel).read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    tree = None
                span_cache[rel] = symmod.spans(tree) if tree else []
            return span_cache[rel]

        for i, s in enumerate(todo):
            if time.time() - started > timeout:
                stats["timed_out"] = True
                stats["skipped"] = len(todo) - i
                break
            if progress:
                progress(i, len(todo), s["qualname"])
            path = root / s["path"]
            if not path.exists():
                continue
            try:
                script = jedi.Script(path=str(path), project=project)
                names = script.get_references(s["line"], s["col"], include_builtins=False, scope="project")
            except Exception:  # jedi can fail on odd code; keep the name-based result
                continue
            found = {}
            for n in names:
                if n.module_path is None:
                    continue
                try:
                    rel = Path(n.module_path).resolve().relative_to(root.resolve()).as_posix()
                except ValueError:
                    continue
                if n.is_definition() or (rel == s["path"] and s["line"] <= n.line <= (s["end"] or s["line"])):
                    continue
                caller, _ = symmod.enclosing(spans_for(rel), n.line)
                text = n.get_line_code() or ""
                after = text[n.column + len(n.name):].lstrip()
                found[(rel, n.line)] = {"path": rel, "line": n.line, "caller": caller, "call": after.startswith("("),
                                        "confidence": "high", "resolution": "jedi"}
            before = {(c["path"], c["line"]): c for c in s["callers"]}
            merged = dict(found)
            for key, c in before.items():
                if key in found:
                    stats["confirmed"] += 1
                elif c["confidence"] == "high":
                    merged[key] = c  # import-resolved and jedi missed it: keep, it is still evidence
                else:
                    stats["dropped"] += 1
            stats["added"] += sum(1 for k in found if k not in before)
            s["callers"] = sorted(merged.values(), key=lambda c: (c["path"], c["line"]))[: symmod.MAX_CALLERS]
            s["resolution"] = "jedi"
            stats["checked"] += 1

    stats["seconds"] = round(time.time() - started, 1)
    stats["status"] = "done"
    stats["remaining"] = sum(1 for s in review.get("symbols", []) if s["kind"] in ("function", "method", "class")
                             and s["change"] != "removed" and s["resolution"] != "jedi")
    review["meta"]["refine"] = stats
    derive.refresh(review)
    return stats


if __name__ == "__main__":  # pragma: no cover
    import json
    import sys
    data = json.loads(Path(sys.argv[1]).read_text())
    print(refine(data, max_symbols=int(os.environ.get("N", "50"))))
