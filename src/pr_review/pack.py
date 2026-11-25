"""Context packs: everything one reviewer needs for one area, and nothing more.

A pack holds the changed hunks of the files to read closely, the old and new
signature of each changed function, a few real call sites for each one (with
surrounding lines), and the gaps already found. Files to skim get a
one-line summary. Formatting-only hunks are left out.

Packs are plain markdown so a subagent can read them directly. Each one
is capped at a token budget; when a pack would go over, the lowest-risk
files lose their diff first and the pack says so.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pr_review import gitutil

CHARS_PER_TOKEN = 4
DEFAULT_MAX_TOKENS = 40_000
SNIPPET_RADIUS = 2

_HUNK = re.compile(r"^@@ ")


def area_hash(review: dict, cluster: dict) -> str:
    """Changes when any file in the area changes, so findings can be reused safely."""
    files = {f["path"]: f for f in review["files"]}
    parts = []
    for p in sorted(cluster["files"]):
        f = files[p]
        if f["category"] == "docs":
            continue  # documentation can't break code, so it doesn't invalidate a review
        parts.append([p, f.get("hunks") or [f["added"], f["removed"]]])
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def _nontrivial_diff(f: dict) -> str:
    """The file's diff with formatting-only hunks dropped."""
    diff = f.get("diff") or ""
    hunks = f.get("hunks") or []
    out, i, keep = [], -1, True
    for line in diff.splitlines():
        if _HUNK.match(line):
            i += 1
            keep = not (i < len(hunks) and hunks[i].get("trivial"))
            if keep:
                syms = [s for s in (hunks[i].get("symbols") if i < len(hunks) else []) or [] if s != "<module>"]
                out.append(line + (f"   [in {', '.join(syms)}]" if syms else ""))
            continue
        if i < 0:
            continue  # diff header
        if keep:
            out.append(line)
    return "\n".join(out)


class _Source:
    def __init__(self, review: dict):
        self.root = Path(review["meta"].get("repo_path") or ".")
        self.sha = review["meta"]["head_sha"]
        self.cache: dict[str, list[str] | None] = {}
        self.reader = None

    def lines(self, path: str) -> list[str] | None:
        if path not in self.cache:
            try:
                if self.reader is None:
                    self.reader = gitutil.BlobReader(self.root)
                text = self.reader.read_text(self.sha, path)
            except (OSError, gitutil.GitError):
                text = None
            self.cache[path] = text.splitlines() if text is not None else None
        return self.cache[path]

    def snippet(self, path: str, line: int) -> str:
        src = self.lines(path)
        if not src:
            return ""
        lo, hi = max(1, line - SNIPPET_RADIUS), min(len(src), line + SNIPPET_RADIUS)
        return "\n".join(f"{'>' if n == line else ' '}{n:>5}  {src[n - 1]}" for n in range(lo, hi + 1))

    def close(self):
        if self.reader:
            self.reader.close()


def _pick_callers(s: dict, n: int) -> list[dict]:
    """Call sites most worth showing: not edited and outside the PR first."""
    callers = [c for c in s["callers"] if c["confidence"] == "high"] or s["callers"]
    callers = sorted(callers, key=lambda c: (c.get("touched", False), c.get("in_pr", False), c["path"], c["line"]))
    return callers[:n]


def _sym_block(s: dict, src: _Source, n_callers: int) -> str:
    from pr_review.appkit import symbol_change_label
    lines = [f"- `{s['qualname']}`: {symbol_change_label(s).lower()}"]
    if s.get("old_sig") and s.get("new_sig") and s["old_sig"] != s["new_sig"]:
        lines.append(f"  - before: `{s['old_sig']}`")
        lines.append(f"  - after:  `{s['new_sig']}`")
    elif s.get("old_sig") and not s.get("new_sig"):
        lines.append(f"  - was: `{s['old_sig']}`")
    sure = sum(1 for c in s["callers"] if c["confidence"] == "high")
    if s["callers"]:
        lines.append(f"  - used in {sure} place(s) for certain, {len(s['callers']) - sure} possible "
                     f"(found by the {'accurate' if s.get('resolution') == 'jedi' else 'quick'} search)")
        for c in _pick_callers(s, n_callers):
            state = ("outside this PR" if not c.get("in_pr") else
                     "in this PR, line edited" if c.get("touched") else "in this PR, line not edited")
            lines.append(f"  - call site `{c['path']}:{c['line']}` in `{c.get('caller') or 'module level'}` ({state}"
                         f"{', possible' if c['confidence'] == 'low' else ''}):")
            snip = src.snippet(c["path"], c["line"])
            if snip:
                lines.append("    ```python\n" + "\n".join("    " + x for x in snip.splitlines()) + "\n    ```")
    else:
        lines.append("  - no other code uses it")
    return "\n".join(lines)


def build_pack(review: dict, cluster_id: str, max_tokens: int | None = None, context_callers: int | None = None) -> dict:
    """Markdown context for one area plus metadata."""
    cluster = next((c for c in review["clusters"] if c["id"] == cluster_id), None)
    if cluster is None:
        raise KeyError(f"no area {cluster_id}; have {', '.join(c['id'] for c in review['clusters'])}")
    rigor = review.get("rigor") or {}
    n_callers = context_callers if context_callers is not None else rigor.get("context_callers", 3)
    max_tokens = max_tokens or DEFAULT_MAX_TOKENS
    files = {f["path"]: f for f in review["files"]}
    syms_by_file: dict[str, list] = {}
    for s in review.get("symbols", []):
        if s["kind"] != "module":
            syms_by_file.setdefault(s["path"], []).append(s)
    gaps = [g for g in review["gaps"] if g.get("cluster") == cluster_id]
    src = _Source(review)
    meta = review["meta"]

    header = [
        f"# Area {cluster_id}: {cluster['name']}",
        f"Pull request: `{meta['head']}` into `{meta['base']}`, head commit `{meta['head_sha'][:12]}`. "
        f"Line numbers refer to that commit. Read files from it with "
        f"`git -C {meta.get('repo_path', '.')} show {meta['head_sha'][:12]}:<path>` if your checkout differs.",
        f"Advice for this area: {cluster.get('tier', '?')} ({'; '.join(cluster.get('tier_reasons', []))}).",
        f"{cluster['stats']['files']} files, +{cluster['stats']['added']}/-{cluster['stats']['removed']} lines, "
        f"{cluster.get('dependents_outside_pr', 0)} files outside the PR import this area.",
    ]
    prev = cluster.get("previous")
    if prev and prev.get("findings"):
        header.append(f"\n## Found by an earlier review of this area ({prev.get('at', '')[:10]}, older code)")
        header.append("The code has changed since. Check whether each one still applies and report it again only if it does.")
        for f in prev["findings"]:
            where = f"{f.get('file')}:{f.get('line')}" if f.get("line") else (f.get("file") or "")
            header.append(f"- [{f['severity']}] {f['title']} {where}")
    if gaps:
        header.append("\n## Already flagged by static checks")
        for g in gaps:
            where = g.get("file") or ", ".join(g.get("files", [])[:5])
            header.append(f"- [{g['severity']}] {g['message']} ({where})")
            for e in g.get("evidence", [])[:6]:
                header.append(f"  - {e}")

    deep = [files[p] for p in cluster["files"] if files[p].get("tier") == "deep"]
    rest = [files[p] for p in cluster["files"] if files[p].get("tier") != "deep"]
    if not deep:  # an area can be sent for review without any deep file (e.g. a new import loop)
        deep, rest = sorted(rest, key=lambda f: -f["score"])[:3], sorted(rest, key=lambda f: -f["score"])[3:]
    deep.sort(key=lambda f: -f["score"])

    sections = []
    for f in deep:
        block = [f"\n## Read closely: `{f['path']}` (risk {f['score']})",
                 "Why: " + "; ".join(r for r in f["score_reasons"]) + "."]
        if f.get("signals"):
            block.append("Risky patterns on added lines: " + "; ".join(f"line {s['line']}: {s['detail']}" for s in f["signals"][:10]))
        syms = sorted(syms_by_file.get(f["path"], []), key=lambda s: (not s["breaking"], s["change"] == "modified"))
        if syms:
            block.append("\n### Changed functions")
            block += [_sym_block(s, src, n_callers) for s in syms[:25]]
            if len(syms) > 25:
                block.append(f"- ... {len(syms) - 25} more changed functions, see the diff")
        diff = _nontrivial_diff(f)
        sections.append({"path": f["path"], "score": f["score"], "head": "\n".join(block),
                         "diff": f"\n### Changes (formatting-only hunks left out)\n```diff\n{diff}\n```" if diff else
                         "\n(no line changes kept; see the file)"})

    skim = ["\n## Also in this area (skim)"] if rest else []
    for f in sorted(rest, key=lambda f: -f["score"]):
        names = [s["qualname"] for s in syms_by_file.get(f["path"], [])][:6]
        skim.append(f"- `{f['path']}` +{f['added']}/-{f['removed']}, risk {f['score']}"
                    + (f"; changes {', '.join(names)}" if names else ""))
    src.close()

    def render(drop: set[str]) -> str:
        out = list(header)
        for s in sections:
            out.append(s["head"])
            out.append(f"\n(diff left out to fit the size limit; read `{s['path']}` directly)" if s["path"] in drop else s["diff"])
        return "\n".join(out + skim) + "\n"

    dropped: set[str] = set()
    text = render(dropped)
    for s in sorted(sections, key=lambda s: s["score"]):
        if len(text) / CHARS_PER_TOKEN <= max_tokens:
            break
        dropped.add(s["path"])
        text = render(dropped)
    return {"cluster": cluster_id, "hash": area_hash(review, cluster), "tokens": len(text) // CHARS_PER_TOKEN,
            "dropped_diffs": sorted(dropped), "text": text}


def build_cross(review: dict) -> str:
    """What connects the areas: changes in one that break or affect code in another."""
    files = {f["path"]: f for f in review["files"]}
    area_of = {p: f.get("cluster") for p, f in files.items()}
    names = {c["id"]: c["name"] for c in review["clusters"]}
    out = [f"# Across areas: `{review['meta']['head']}` into `{review['meta']['base']}`", ""]

    lines = []
    for s in review.get("symbols", []):
        if not s["breaking"] or s["kind"] == "module":
            continue
        home = area_of.get(s["path"])
        elsewhere: dict[str, list] = {}
        for c in s["callers"]:
            if c["confidence"] != "high":
                continue
            other = area_of.get(c["path"]) or "outside the PR"
            if other != home:
                elsewhere.setdefault(other, []).append(f"{c['path']}:{c['line']}" + ("" if c.get("touched") else " (not edited)"))
        for other, where in sorted(elsewhere.items()):
            who = f"area {other} ({names[other]})" if other in names else "code outside the PR"
            lines.append(f"- `{s['qualname']}` ({s['change']}, area {home}) is used by {who}: "
                         + ", ".join(where[:6]) + (" ..." if len(where) > 6 else ""))
    out.append("## Breaking changes used by other areas")
    out += lines or ["- none"]

    d = review.get("delta") or {}
    out.append("\n## Structure")
    out += [f"- new import loop: {' -> '.join(c['files'])}" for c in d.get("cycles", [])] or ["- no new import loops"]
    out += [f"- {p['source']} now depends on {p['target']}" for p in (d.get("package_edges") or {}).get("added", [])]
    edges = review["graph"].get("cluster_edges", [])
    if edges:
        out.append("- imports between areas: " + ", ".join(f"{e['source']}->{e['target']} ({e['weight']})" for e in edges[:30]))

    out.append("\n## What each area's reviewer found")
    for c in review["clusters"]:
        tier = c.get("tier", "?")
        if c.get("summary") or c.get("findings"):
            out.append(f"\n### {c['id']} {c['name']} ({tier})")
            if c.get("summary"):
                out.append(c["summary"])
            for f in c.get("findings", []):
                where = f"{f.get('file')}:{f.get('line')}" if f.get("line") else (f.get("file") or "")
                out.append(f"- [{f['severity']}] {f['title']} {where}")
        else:
            out.append(f"\n### {c['id']} {c['name']} ({tier}): not reviewed by an agent")
    return "\n".join(out) + "\n"
