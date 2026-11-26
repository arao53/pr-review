"""Helpers for the marimo app: git lookups, plain-language wording, charts,
diff rendering and saved progress. Kept out of app.py so it can be tested
without a browser."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
from pathlib import Path

from pr_review import derive, gitutil, report
from pr_review.cli import STATE_DIR, _slug

# ---------------------------------------------------------------- words

TIER = {
    "deep": {"label": "Read closely", "color": "#d1495b", "soft": "#fbe3e6"},
    "skim": {"label": "Skim", "color": "#d58a1c", "soft": "#fdf1dc"},
    "skip": {"label": "Safe to skip", "color": "#7b8a99", "soft": "#eef1f4"},
}
TIER_ORDER = {"deep": 0, "skim": 1, "skip": 2}
SEVERITY = {
    "high": {"label": "Must check", "kind": "danger"},
    "medium": {"label": "Should check", "kind": "warn"},
    "low": {"label": "Worth a look", "kind": "neutral"},
    "info": {"label": "For your information", "kind": "info"},
}

GAP_HELP = {
    "removed_symbol_still_used": (
        "Something was deleted but is still used",
        "A function or class was removed, yet other code still refers to it. That code will fail when it runs.",
        "Check each listed place. Either keep the function or update those places in this change."),
    "stale_module_import": (
        "A file was deleted or moved but is still imported",
        "Other files still load this file by its old name. They will fail to start.",
        "Update the listed files to use the new location."),
    "no_tests_changed": (
        "Code changed but no tests did",
        "Nothing in this change checks that the new behaviour works.",
        "Ask the author which tests cover this, or ask for new ones."),
    "syntax_error": (
        "A file has a syntax error",
        "Python cannot read this file as written, so nothing in it will run.",
        "Ask the author to fix it before reviewing further."),
    "breaking_change_untouched_callers": (
        "A function changed how it is called, but its users weren't updated",
        "The function now expects different inputs. Places that call it the old way were not edited in this change.",
        "Open the listed places and confirm each call still matches."),
    "breaking_change_maybe_callers": (
        "A function changed how it is called and may have users elsewhere",
        "Code that calls a method with the same name might be affected. The quick pass can't tell for sure.",
        "Run 'Improve accuracy' to confirm, or check the listed places."),
    "removed_method_maybe_used": (
        "A method was removed and something with the same name is still called",
        "This may or may not be the same method. The quick pass can't tell for sure.",
        "Run 'Improve accuracy' to confirm."),
    "removed_override": (
        "A method was removed, but a parent class may still provide it",
        "Calls will probably fall back to the parent class's version, which can behave differently.",
        "Confirm the parent version does what callers expect."),
    "untested_change": (
        "Changed code with no tests",
        "No test loads this file, and no test was changed for it.",
        "Ask how this change was checked."),
    "untested_new_file": (
        "New file with no tests",
        "This file is new and no test loads it.",
        "Ask for at least a basic test."),
    "tests_not_updated": (
        "Tests exist but weren't updated",
        "Tests load this file, but none of them changed even though the file did.",
        "Check whether the existing tests still describe the new behaviour."),
    "uncovered_lines": (
        "Changed lines the tests never run",
        "The coverage report shows these new lines were never executed by any test.",
        "Ask for a test that runs them."),
    "irreversible_migration": (
        "A database change can't be undone",
        "If this release has to be rolled back, the database change will stay.",
        "Ask whether a rollback step is needed."),
    "dependency_without_lock": (
        "New library versions without a lock file",
        "The list of required libraries changed but the exact pinned versions did not.",
        "Ask the author to update the lock file."),
    "conftest_changed": (
        "Shared test setup changed",
        "This file sets up many tests at once, so a change here can affect all of them.",
        "Skim it with that in mind."),
    "wide_blast_radius": (
        "A widely used file changed its public parts",
        "Many other files load this one, so mistakes here spread far.",
        "Give it extra attention."),
    "new_import_cycle": (
        "Two parts of the code now depend on each other in a loop",
        "Loops like this make code harder to change and can cause errors when the program starts.",
        "Ask whether the new import is necessary."),
    "new_package_dependency": (
        "One part of the code now depends on another for the first time",
        "This links two areas that were independent before.",
        "Check that this connection is intended."),
}


def gap_help(kind: str) -> tuple[str, str, str]:
    return GAP_HELP.get(kind, (kind.replace("_", " ").capitalize(), "", ""))


CHANGE_RANK = {"removed": 1, "signature": 2, "added": 3, "modified": 4}


def symbol_rank(s: dict) -> tuple:
    """Most consequential first: breaking, removed, inputs changed, new, inside only."""
    return (not s["breaking"], CHANGE_RANK.get(s["change"], 5), -s.get("high_callers", 0), s["qualname"])


def usage_text(s: dict) -> str:
    sure = s.get("high_callers", 0)
    maybe = len(s["callers"]) - sure
    if not sure and not maybe:
        return "not used anywhere else"
    parts = []
    if sure:
        parts.append(f"used in {sure} place{'s' if sure != 1 else ''}")
    if maybe:
        parts.append(f"{maybe} possible use{'s' if maybe != 1 else ''} (same name, not confirmed)")
    return ", ".join(parts)


def symbol_change_label(s: dict) -> str:
    if s["kind"] == "module":
        return "Top-level code changed"
    if s["change"] == "added":
        return "New"
    if s["change"] == "removed":
        return "Removed"
    if s["change"] == "signature":
        if s["kind"] == "class":
            return "Parent class changed"
        return "Inputs changed, may break callers" if s["breaking"] else "Inputs changed, old calls still work"
    return "Changed inside"


_REASONS = [
    (re.compile(r"^\+(\d+) churn \((\d+)\+/(\d+)-\)"), lambda m: f"Size of the change: {m[2]} lines added, {m[3]} removed"),
    (re.compile(r"^\+(\d+) imported by (\d+) module"), lambda m: f"Used by {m[2]} other file(s), directly or indirectly"),
    (re.compile(r"^\+(\d+) sensitive path \((.+)\)"), lambda m: f"In a sensitive area ({m[2]})"),
    (re.compile(r"^\+(\d+) database migration"), lambda m: "Changes the database structure"),
    (re.compile(r"^\+(\d+) dependency manifest"), lambda m: "Changes which libraries the project needs"),
    (re.compile(r"^\+(\d+) CI or build config"), lambda m: "Changes how the project is built or tested automatically"),
    (re.compile(r"^\+(\d+) conftest"), lambda m: "Shared test setup, affects many tests"),
    (re.compile(r"^\+(\d+) public API: (\d+) breaking, (\d+)"),
     lambda m: f"Changes what other code can use: {m[2]} breaking, {m[3]} additions or safe changes"),
    (re.compile(r"^\+(\d+) risk patterns \((.+)\)"), lambda m: f"Risky code patterns: {m[2]}"),
    (re.compile(r"^\+(\d+) no test in this PR touches it(.*)"),
     lambda m: "No test was changed for it" + (", and no test loads it" if m[2] else "")),
    (re.compile(r"^\+(\d+) (\d+)/(\d+) changed lines uncovered"), lambda m: f"{m[2]} of {m[3]} changed lines never run in tests"),
    (re.compile(r"^x([\d.]+) (\w+) file"), lambda m: f"Counts less because it is a {m[2]} file"),
]


def plain_reason(text: str) -> tuple[str, str]:
    """('+15', 'In a sensitive area (auth)') from a score reason."""
    pts = text.split(" ", 1)[0]
    for rx, fn in _REASONS:
        m = rx.match(text)
        if m:
            return pts, fn(m)
    return pts, text


def headline(review: dict) -> str:
    s, b = review["stats"], review["budget"]
    skipped = s["collapsed_files"] + b["files"]["skip"]
    parts = [
        f"This change touches **{s['files_changed']} files** "
        f"(+{s['lines_added']:,} / −{s['lines_removed']:,} lines).",
        f"**{skipped}** of them don't need reading: formatting, renames, generated files or low-risk edits.",
        f"The rest is grouped into **{len(review['clusters'])} areas**. "
        f"**{b['clusters']['deep']}** need a close read and **{b['clusters']['skim']}** a skim.",
    ]
    high = [g for g in review["gaps"] if g["severity"] == "high"]
    if high:
        parts.append(f"There {'is' if len(high) == 1 else 'are'} **{len(high)} thing{'s' if len(high) != 1 else ''} "
                     f"you must check** before approving.")
    return " ".join(parts)


def fmt_minutes(m: int) -> str:
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h} h {mm:02d} min" if mm else f"{h} h"


def fmt_tokens(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 1000 else str(n)


# ---------------------------------------------------------------- git

def _git(repo, *args, timeout=60) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    p = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=timeout, env=env)
    if p.returncode:
        raise gitutil.GitError(p.stderr.strip() or f"git {' '.join(args)} failed")
    return p.stdout


def find_repo(path: str | Path) -> Path | None:
    try:
        return gitutil.repo_root(Path(path).expanduser())
    except (gitutil.GitError, FileNotFoundError, NotADirectoryError):
        return None


def list_versions(repo: Path) -> dict:
    """Branches, newest first, the one that is checked out, a sensible base,
    and a suggested head that actually differs from that base."""
    out = _git(repo, "for-each-ref", "--sort=-committerdate",
               "--format=%(refname:short)\t%(committerdate:relative)\t%(objectname)",
               "refs/heads", "refs/remotes", "refs/pr-review")
    refs, sha = [], {}
    for line in out.splitlines():
        name, when, obj = (line.split("\t") + ["", ""])[:3]
        if name.endswith("/HEAD") or "/" not in name and name in _remotes(repo):
            continue
        refs.append((name, when))
        sha[name] = obj
    try:
        default = gitutil.default_base(repo)
    except gitutil.GitError:
        default = refs[0][0] if refs else None
    current = gitutil.current_branch(repo)
    base_sha = sha.get(default) or (gitutil.try_rev_parse(repo, default) if default else None)

    def differs(name):
        return name != default and sha.get(name) != base_sha

    # A downloaded pull request first, then the checked-out branch, then the
    # most recently updated branch with different code.
    names = [n for n, _ in refs]
    pr_refs = [n for n in names if n.startswith("pr-review/")]
    suggested = next((n for n in pr_refs if differs(n)), None)
    if suggested is None and current and differs(current):
        suggested = current
    if suggested is None:
        suggested = next((n for n in names if differs(n)), current or (names[0] if names else None))
    return {"current": current, "refs": refs, "default_base": default, "suggested_head": suggested, "sha": sha}


def _remotes(repo: Path) -> set[str]:
    return set(_git(repo, "remote").split())


def fetch_pull_request(repo: Path, number: int, remote: str | None = None) -> str:
    """Download a GitHub pull request without switching branches. Returns the
    short ref name the version list uses.

    GitHub keeps pull requests on the repository they were opened against,
    so in a fork the `upstream` remote is tried before `origin`."""
    ref = f"refs/pr-review/pr-{number}"
    remotes = [remote] if remote else sorted(_remotes(repo), key=lambda r: {"upstream": 0, "origin": 1}.get(r, 2))
    errors = []
    for r in remotes:
        try:
            _git(repo, "fetch", "--quiet", r, f"+pull/{number}/head:{ref}", timeout=180)
            return ref[len("refs/"):]
        except gitutil.GitError as exc:
            errors.append(f"{r}: {str(exc).splitlines()[-1] if str(exc) else exc}")
    raise gitutil.GitError("; ".join(errors) or "this project has no remote to download from")


def explain_same(repo: Path, base: str, head: str) -> str | None:
    """Why comparing these two versions would show no changes, in plain
    words, or None when there is something to review."""
    b, h = gitutil.try_rev_parse(repo, base), gitutil.try_rev_parse(repo, head)
    if not b or not h:
        return None
    lb, lh = ref_label(base), ref_label(head)
    if b == h:
        return (f"**{lh}** and **{lb}** are the same version (commit `{h[:7]}`), so there is nothing to compare. "
                "Pick the version that has the pull request's changes as the first one.")
    try:
        mb = gitutil.merge_base(repo, b, h)
    except gitutil.GitError:
        return None
    if mb == h:
        return (f"Everything in **{lh}** is already in **{lb}**. It was probably merged already, or it is an "
                "old copy of the branch. If you're reviewing a pull request, download it by number below "
                "to get its latest version.")
    if not _git(repo, "diff", "--name-only", mb, h).strip():
        return f"**{lh}** has new commits, but they don't change any files compared with **{lb}**."
    return None


def ref_label(ref: str) -> str:
    m = re.match(r"(?:refs/)?pr-review/pr-(\d+)$", ref or "")
    return f"Pull request #{m.group(1)}" if m else ref


# ---------------------------------------------------------------- saved state

def review_dir(repo: Path, review: dict) -> Path:
    head = review["meta"]["head"]
    name = head if head != "HEAD" else review["meta"]["head_sha"][:12]
    return Path(repo) / STATE_DIR / _slug(name)


def save(review: dict, repo: Path, fresh: bool = False) -> Path:
    """Write the review. fresh=True means it came from a new analysis, so
    earlier agent findings are carried over where the code is unchanged."""
    d = review_dir(repo, review)
    if fresh and (d / "review.json").exists():
        from pr_review import findings
        try:
            findings.carry_over(report.load(d / "review.json"), review)
        except (ValueError, KeyError, OSError):
            pass
    report.write(review, d)
    state = Path(repo) / STATE_DIR
    (state / ".gitignore").write_text("*\n")
    (state / "latest").write_text(d.name + "\n")
    return d / "review.json"


def saved_reviews(repo: Path) -> list[tuple[str, Path]]:
    state = Path(repo) / STATE_DIR
    out = []
    for p in sorted(state.glob("*/review.json"), key=lambda p: -p.stat().st_mtime):
        try:
            meta = json.loads(p.read_text())["meta"]
            out.append((f"{ref_label(meta['head'])} vs {meta['base']} · {meta['generated_at'][:16].replace('T', ' ')}", p))
        except (OSError, ValueError, KeyError):
            continue
    return out


def load(path: Path) -> dict:
    r = report.load(path)
    if r.get("budget") is None or "symbols" not in r:
        r.setdefault("symbols", [])
        r.setdefault("delta", {"module_edges": {"added": [], "removed": []}, "package_edges": {"added": [], "removed": []},
                               "cycles": [], "call_edges": {"added": [], "removed": []}, "symbols": {}})
        derive.refresh(r)
    return r


def _file_key(f: dict) -> str:
    return hashlib.sha1(json.dumps(f.get("hunks") or [f["added"], f["removed"]], sort_keys=True).encode()).hexdigest()[:12]


class Progress:
    """Which files the reviewer has marked as done. A mark is dropped when the
    file's changes are different from when it was marked."""

    def __init__(self, repo: Path, review: dict):
        self.path = review_dir(repo, review) / "progress.json"
        try:
            self.data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.data = {}
        self.keys = {f["path"]: _file_key(f) for f in review["files"]}

    def done(self, path: str) -> bool:
        return self.data.get(path) == self.keys.get(path)

    def set(self, path: str, value: bool) -> None:
        if value:
            self.data[path] = self.keys.get(path)
        else:
            self.data.pop(path, None)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1))

    def count(self, paths) -> int:
        return sum(1 for p in paths if self.done(p))


# ---------------------------------------------------------------- charts

def _layout(nodes: list[str], edges: list[tuple[str, str, float]], ring: bool = False,
            seed: int = 7) -> dict[str, tuple[float, float]]:
    """Positions in roughly [-1, 1]. `ring` places nodes on a circle in the
    given order (clockwise from the top), which keeps a short list readable."""
    import math

    import networkx as nx
    n = len(nodes)
    if n == 1:
        return {nodes[0]: (0.0, 0.0)}
    if ring:
        return {v: (math.sin(2 * math.pi * i / n), math.cos(2 * math.pi * i / n)) for i, v in enumerate(nodes)}
    g = nx.Graph()
    g.add_nodes_from(nodes)
    for a, b, w in edges:
        if a in g and b in g and a != b:
            g.add_edge(a, b, weight=w)
    # A hidden hub with weak links keeps unconnected files near the middle
    # instead of drifting to the edges.
    hub = object()
    for v in nodes:
        g.add_edge(hub, v, weight=0.08)
    pos = nx.spring_layout(g, k=1.4 / n ** 0.5, seed=seed, weight="weight", iterations=200)
    pos.pop(hub)
    return {v: (float(x), float(y)) for v, (x, y) in pos.items()}


def _graph_chart(nodes: list[dict], edges: list[tuple[str, str, float]], height: int, label_field: str,
                 tooltip: list, ring: bool = False):
    import altair as alt
    import pandas as pd
    pos = _layout([n["id"] for n in nodes], edges, ring=ring)
    for n in nodes:
        n["x"], n["y"] = pos[n["id"]]
    df = pd.DataFrame(nodes)
    erows = [{"x": pos[a][0], "y": pos[a][1], "x2": pos[b][0], "y2": pos[b][1], "w": w}
             for a, b, w in edges if a in pos and b in pos and a != b]
    axis = alt.Axis(labels=False, ticks=False, grid=False, domain=False, title=None)
    pad = 0.35
    xs, ys = df["x"], df["y"]
    xscale = alt.Scale(domain=[xs.min() - pad, xs.max() + pad])
    yscale = alt.Scale(domain=[ys.min() - pad, ys.max() + pad])
    pick = alt.selection_point(fields=["id"], name="pick", on="click")
    layers = []
    if erows:
        layers.append(alt.Chart(pd.DataFrame(erows)).mark_rule(color="#b9c2cc", opacity=0.6).encode(
            x=alt.X("x:Q", axis=axis, scale=xscale), y=alt.Y("y:Q", axis=axis, scale=yscale),
            x2="x2:Q", y2="y2:Q", strokeWidth=alt.StrokeWidth("w:Q", legend=None, scale=alt.Scale(range=[1, 5]))))
    colors = alt.Color("tier_label:N", title=None,
                       scale=alt.Scale(domain=[t["label"] for t in TIER.values()], range=[t["color"] for t in TIER.values()]),
                       legend=alt.Legend(orient="bottom"))
    base = alt.Chart(df).encode(x=alt.X("x:Q", axis=axis, scale=xscale), y=alt.Y("y:Q", axis=axis, scale=yscale))
    layers.append(base.mark_circle(opacity=0.9, stroke="white", strokeWidth=2, cursor="pointer").encode(
        size=alt.Size("size:Q", legend=None, scale=alt.Scale(range=[150, 2600])),
        color=colors, tooltip=tooltip).add_params(pick))
    layers.append(base.mark_text(dy=alt.expr("-sqrt(datum.size) / 3.2 - 9"), fontSize=12, color="#3b4752")
                  .encode(text=f"{label_field}:N"))
    return alt.layer(*layers).properties(height=height, width="container").configure_view(strokeWidth=0)


def areas_chart(review: dict):
    nodes = []
    for c in sorted(review["clusters"], key=lambda c: (TIER_ORDER[c["tier"]], -c["stats"]["priority"])):
        nodes.append({"id": c["id"], "name": c["name"], "short": short(c["name"], 26), "tier": c["tier"],
                      "tier_label": TIER[c["tier"]]["label"], "size": c["stats"]["added"] + c["stats"]["removed"] + 10,
                      "files": c["stats"]["files"], "risk": c["stats"]["max_score"],
                      "lines": f"+{c['stats']['added']:,} / −{c['stats']['removed']:,}"})
    edges = [(e["source"], e["target"], float(e["weight"])) for e in review["graph"]["cluster_edges"]]
    tooltip = [{"field": "name", "title": "Area"}, {"field": "tier_label", "title": "Advice"},
               {"field": "files", "title": "Files"}, {"field": "lines", "title": "Lines"},
               {"field": "risk", "title": "Highest risk"}]
    return _graph_chart(nodes, edges, 420, "short", tooltip, ring=len(nodes) <= 16)


def files_chart(review: dict, cluster_id: str):
    c = next(c for c in review["clusters"] if c["id"] == cluster_id)
    files = {f["path"]: f for f in review["files"]}
    members = set(c["files"])
    nodes = [{"id": p, "name": p, "short": short(Path(p).name, 22), "tier": files[p]["tier"],
              "tier_label": TIER[files[p]["tier"]]["label"],
              "size": files[p].get("review_lines", files[p]["added"] + files[p]["removed"]) + 5,
              "risk": files[p]["score"], "lines": f"+{files[p]['added']} / −{files[p]['removed']}"} for p in c["files"]]
    edges = [(e["source"], e["target"], 1.0) for e in review["graph"]["file_edges"]
             if e["source"] in members and e["target"] in members]
    tooltip = [{"field": "name", "title": "File"}, {"field": "tier_label", "title": "Advice"},
               {"field": "lines", "title": "Lines"}, {"field": "risk", "title": "Risk score"}]
    return _graph_chart(nodes, edges, 340, "short", tooltip)


def short(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------- html pieces

def badge(tier: str, text: str | None = None) -> str:
    t = TIER[tier]
    return (f"<span style='display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;"
            f"font-weight:600;background:{t['soft']};color:{t['color']};white-space:nowrap'>"
            f"<span style='display:inline-block;width:7px;height:7px;border-radius:50%;background:{t['color']};"
            f"margin-right:6px;vertical-align:1px'></span>{html.escape(text or t['label'])}</span>")


def pill(text: str, bg: str = "#eef1f4", fg: str = "#3b4752") -> str:
    return (f"<span style='display:inline-block;padding:1px 8px;border-radius:6px;font-size:12px;"
            f"background:{bg};color:{fg};white-space:nowrap'>{html.escape(text)}</span>")


def diff_html(f: dict, focus: tuple[int, int] | None = None) -> str:
    """The file's diff, one block per hunk, formatting-only hunks folded away."""
    diff = f.get("diff")
    if not diff:
        return "<p style='color:#6b7785'>The changes for this file weren't saved with the review.</p>"
    hunks = f.get("hunks") or []
    blocks, cur, idx = [], None, -1
    for line in diff.splitlines():
        if line.startswith("@@"):
            idx += 1
            m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)", line)
            cur = {"old": int(m.group(1)), "new": int(m.group(2)), "rows": [], "hunk": hunks[idx] if idx < len(hunks) else {}}
            blocks.append(cur)
            continue
        if cur is None or line.startswith("\\"):
            continue
        cur["rows"].append(line)
    if not blocks:
        return "<p style='color:#6b7785'>No line changes (binary, rename or mode change).</p>"
    out = []
    for b in blocks:
        h = b["hunk"]
        names = [s for s in h.get("symbols", []) if s != "<module>"]
        where = ", ".join(f"<code>{html.escape(n)}</code>" for n in names) or "top-level code"
        new_ln, old_ln, rows = b["new"], b["old"], []
        for row in b["rows"]:
            kind = row[:1]
            text = html.escape(row[1:]) or "&nbsp;"
            if kind == "+":
                style, num, sign = "background:#e6f4ea;color:#13652e", new_ln, "+"
                new_ln += 1
            elif kind == "-":
                style, num, sign = "background:#fde8e8;color:#9b1c1c", old_ln, "−"
                old_ln += 1
            else:
                style, num, sign = "color:#3b4752", new_ln, " "
                new_ln += 1
                old_ln += 1
            hl = ("box-shadow:inset 4px 0 0 #4c6ef5;" if focus and kind != "-" and focus[0] <= num <= focus[1]
                  else "")
            rows.append(f"<div style='{style};{hl}display:flex'><span style='width:52px;flex:none;text-align:right;"
                        f"padding-right:10px;color:#9aa5b1;user-select:none'>{num}</span>"
                        f"<span style='width:14px;flex:none;user-select:none'>{sign}</span>"
                        f"<span style='white-space:pre'>{text}</span></div>")
        body = (f"<div style='font:12.5px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-x:auto;"
                f"border:1px solid #e3e7ec;border-radius:8px;padding:6px 0;background:#fff'>{''.join(rows)}</div>")
        title = (f"Lines {b['new']}–{max(b['new'], new_ln - 1)} · in {where} · "
                 f"+{h.get('added', 0)} / −{h.get('removed', 0)}")
        if h.get("trivial"):
            out.append(f"<details style='margin:10px 0'><summary style='cursor:pointer;color:#6b7785'>{title} · "
                       f"<b>formatting only</b>, hidden</summary>{body}</details>")
        else:
            out.append(f"<div style='margin:14px 0 4px;color:#3b4752;font-size:13px'>{title}</div>{body}")
    if f.get("diff_truncated"):
        out.append("<p style='color:#6b7785'>This diff was cut short because it is very large.</p>")
    return "".join(out)


# ---------------------------------------------------------------- prompts

def claude_prompt(review: dict, cluster: dict | None = None, file: dict | None = None) -> str:
    path = review["meta"].get("review_path") or ".pr-review/<branch>/review.json"
    ctx = (review.get("rigor") or {}).get("context_callers", 3)
    if file:
        syms = [s for s in review.get("symbols", []) if s["path"] == file["path"] and s["kind"] != "module"]
        lines = [f"Please review the changes to {file['path']} in this pull request.",
                 f"The analysis is in {path}. This file's risk score is {file['score']} because: "
                 + "; ".join(file["score_reasons"]) + "."]
        if syms:
            lines.append("Changed functions: " + ", ".join(f"{s['qualname']} ({symbol_change_label(s).lower()})" for s in syms[:15]) + ".")
        lines.append(f"For each changed function, check up to {ctx} places that use it. Report problems with "
                     "file:line references, how sure you are, and what to do about each.")
        return "\n".join(lines)
    if cluster:
        return (f"Please review area {cluster['id']} (\"{cluster['name']}\") of this pull request. "
                f"The analysis is in {path}; run `pr-review summary --cluster {cluster['id']} --json` for details. "
                f"Read these files in order: {', '.join(cluster['files'][:12])}"
                f"{' and others' if len(cluster['files']) > 12 else ''}. "
                f"For each changed function, check up to {ctx} places that use it. Report problems with "
                "file:line references, how sure you are, and what to do about each.")
    return (f"Please review this pull request using {path}. Start with the areas marked 'deep', in order, "
            "and report problems with file:line references.")


# ---------------------------------------------------------------- agent findings

FINDING_KIND = {"high": "danger", "medium": "warn", "low": "neutral", "info": "info"}


def all_findings(review: dict) -> list[tuple[dict, str | None]]:
    """(finding, area id) for the PR and every area, most severe first."""
    from pr_review.derive import SEVERITY_ORDER
    out = [(f, None) for f in review.get("findings", [])]
    for c in review["clusters"]:
        out += [(f, c["id"]) for f in c.get("findings", [])]
    return sorted(out, key=lambda x: (SEVERITY_ORDER[x[0]["severity"]], x[0].get("file") or "", x[0].get("line") or 0))


def finding_md(f: dict, with_where: bool = True) -> str:
    where = ""
    if with_where and f.get("file"):
        where = f"<br><span class='pr-muted'>at <code>{html.escape(f['file'])}{':' + str(f['line']) if f.get('line') else ''}</code></span>"
    sure = {"high": "checked in the code", "medium": "likely", "low": "a guess worth a look"}.get(f.get("confidence"), "")
    body = f"**{html.escape(f['title'])}**"
    if f.get("detail"):
        body += f"  \n{html.escape(f['detail'])}"
    if f.get("suggestion"):
        body += f"  \n*What to do:* {html.escape(f['suggestion'])}"
    meta = f"{SEVERITY[f['severity']]['label']}" + (f" · {sure}" if sure else "") + " · from AI review"
    return f"{body}  \n<span class='pr-muted'>{meta}</span>{where}"
