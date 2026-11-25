"""The analysis pipeline: git diff -> triage -> import graph -> scores ->
gaps -> clusters -> one JSON document (see schema/review.schema.json)."""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

from pr_review import cluster as clustering
from pr_review import derive, gitutil, pyast, triage
from pr_review import symbols as symmod
from pr_review.derive import SEVERITY_ORDER
from pr_review.graph import ImportGraph, _alt_names, _chain_name, build_graph
from pr_review.score import score_file

MAX_EDGES = 400


def analyze(
    repo: str | Path = ".",
    base: str | None = None,
    head: str = "HEAD",
    coverage_xml: str | Path | None = None,
    include_diffs: bool = True,
    max_clusters: int | None = None,
    max_diff_bytes: int = 150_000,
    line_budget: int | None = None,
    options: dict | None = None,
    progress=None,
) -> dict:
    """Run the full static analysis. `options` comes from
    derive.resolve_options(); max_clusters and line_budget override it."""
    from pr_review import SCHEMA_VERSION, __version__

    opts = dict(options or derive.resolve_options())
    if max_clusters is not None:
        opts["max_clusters"] = max_clusters
    if line_budget is not None:
        opts["line_budget"] = line_budget
    step = progress or (lambda msg: None)

    root = gitutil.repo_root(repo)
    head_sha = gitutil.rev_parse(root, head)
    base_ref = base or gitutil.default_base(root)
    base_sha = gitutil.rev_parse(root, base_ref)
    mb = gitutil.merge_base(root, base_sha, head_sha)
    changes = gitutil.changed_files(root, mb, head_sha)
    head_tree = gitutil.list_files(root, head_sha)
    head_set = set(head_tree)
    coverage = _load_coverage(coverage_xml) if coverage_xml else None

    recs: list[dict] = []
    step(f"Reading {len(head_tree)} files")
    with gitutil.BlobReader(root) as blobs:
        sources = {}
        for p in head_tree:
            if p.endswith(".py"):
                text = blobs.read_text(head_sha, p)
                if text is not None:
                    sources[p] = text
        step("Mapping imports")
        graph = build_graph(sources)

        step(f"Sorting {len(changes)} changed files")
        for ch in changes:
            recs.append(_triage_change(root, blobs, ch, mb, head_sha, sources, include_diffs, max_diff_bytes, coverage))

        # A partial graph of the merge-base: only the changed files are parsed,
        # plus package __init__ files for re-exports. Everything else in the
        # tree is identical at both ends, so its edges are taken from head.
        base_paths = {p for p in gitutil.list_files(root, mb) if p.endswith(".py")}
        base_sources = {}
        for r in recs:
            old = r["old_path"] or r["path"]
            if r["_old_text"] is not None and old in base_paths:
                base_sources[old] = r["_old_text"]
        for p in base_paths:
            if p.endswith("__init__.py") and p not in base_sources:
                text = blobs.read_text(mb, p)
                if text is not None:
                    base_sources[p] = text
        base_graph = build_graph(base_sources, base_paths)

    changed = {r["path"] for r in recs}
    step("Finding changed functions and who uses them")
    symbols, call_delta = _symbols(recs, graph, base_graph, changed)
    step("Comparing structure before and after")
    delta = _delta(recs, graph, base_graph)
    delta["call_edges"] = call_delta
    delta["symbols"] = _symbol_counts(symbols)
    test_files = [p for p in sources if triage.is_test(p)]
    for r in recs:
        if r["kind"] == "semantic":
            _link(r, graph, changed, test_files, head_set)
            r["score"], r["score_reasons"] = score_file(r)
        else:
            r["score_reasons"] = [f"collapsed: {r.pop('_reason')}"]
        r.pop("_reason", None)
        r.pop("_old_text", None)
        r.pop("_new_text", None)

    gaps = _gaps(recs, graph, changed, sources) + _delta_gaps(delta)

    reviewable = [r for r in recs if r["kind"] == "semantic"]
    by_path = {r["path"]: r for r in recs}
    groups = clustering.cluster_paths(
        [r["path"] for r in reviewable], graph, {r["path"]: r["category"] for r in reviewable},
        {r["path"]: r.get("review_lines", r["added"] + r["removed"]) for r in reviewable},
        opts["max_clusters"], line_budget=opts["line_budget"],
    )
    clusters = _build_clusters(groups, by_path, graph, changed)
    for g in gaps:
        if g.get("file") in by_path:
            g["cluster"] = by_path[g["file"]].get("cluster")
    gaps = derive.fold_repeated(gaps)

    sem_set = {r["path"] for r in reviewable}
    file_edges, cluster_w = [], {}
    for a, b, data in graph.g.edges(data=True):
        if a in sem_set and b in sem_set:
            file_edges.append({"source": a, "target": b, "names": sorted(data["names"])})
            ca, cb = by_path[a]["cluster"], by_path[b]["cluster"]
            if ca != cb:
                cluster_w[(ca, cb)] = cluster_w.get((ca, cb), 0) + 1

    collapsed = [
        {"path": r["path"], "old_path": r["old_path"], "kind": r["kind"], "reason": r["score_reasons"][0][len("collapsed: "):],
         "added": r["added"], "removed": r["removed"]}
        for r in recs if r["kind"] != "semantic"
    ]
    recs.sort(key=lambda r: (r["kind"] != "semantic", -r["score"], r["path"]))

    review = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "pr-review", "version": __version__},
        "meta": {
            "repo": root.name,
            "base": base_ref,
            "head": head if head != "HEAD" else (gitutil.current_branch(root) or "HEAD"),
            "base_sha": base_sha,
            "head_sha": head_sha,
            "merge_base_sha": mb,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "review_path": None,
            "repo_path": str(root),
            "options": {"include_diffs": include_diffs, "coverage": str(coverage_xml) if coverage_xml else None},
            "refine": None,
            "graph_note": "Edges come from static imports only. pytest fixtures, importlib, Django settings/URLs, "
                          "entry points and registries are not visible.",
        },
        "stats": {
            "files_changed": len(recs),
            "reviewable_files": len(reviewable),
            "collapsed_files": len(collapsed),
            "lines_added": sum(r["added"] for r in recs),
            "lines_removed": sum(r["removed"] for r in recs),
            "reviewable_lines": sum(r["added"] + r["removed"] for r in reviewable),
            "review_lines": sum(r.get("review_lines", 0) for r in reviewable),
            "symbols_changed": len(symbols),
            "clusters": len(clusters),
            "parse_errors": len(graph.parse_errors),
            "graph_modules": graph.g.number_of_nodes(),
        },
        "summary": None,
        "files": recs,
        "clusters": clusters,
        "collapsed": collapsed,
        "graph": {
            "cluster_edges": [{"source": a, "target": b, "weight": w} for (a, b), w in sorted(cluster_w.items())],
            "file_edges": file_edges,
        },
        "symbols": symbols,
        "delta": delta,
        "gaps": derive.sort_gaps(gaps),
        "findings": [],
        "rigor": opts,
        "budget": None,
    }
    derive.refresh(review, opts)
    if opts.get("refine"):
        from pr_review import refine as refine_mod
        try:
            step("Running the accurate usage search")
            refine_mod.refine(review, max_symbols=opts.get("refine_max_symbols"))
        except refine_mod.RefineUnavailable as exc:
            review["meta"]["refine"] = {"status": "unavailable", "message": str(exc)}
    return review


# ---------------------------------------------------------------- per file

def _triage_change(root, blobs, ch, mb, head_sha, sources, include_diffs, max_diff_bytes, coverage) -> dict:
    path = ch.path
    is_py = path.endswith(".py")
    cat = triage.category(path)
    old_text = blobs.read_text(mb, ch.old_path or path) if ch.status != "A" and is_py else None
    new_text = None
    if ch.status != "D":
        new_text = sources.get(path) if is_py else blobs.read_text(head_sha, path)

    rec = {
        "path": path, "old_path": ch.old_path, "status": ch.status, "category": cat,
        "language": triage.language(path), "added": ch.added, "removed": ch.removed,
        "similarity": ch.similarity, "score": 0, "score_reasons": [], "signals": [], "api_changes": [],
        "dependents": {"direct": 0, "transitive": 0, "outside_pr": []},
        "tests": {"importing": [], "changed": []}, "coverage": None, "cluster": None,
        "diff": None, "diff_truncated": False, "findings": [], "hunks": [], "review_lines": 0, "symbols": [],
        "_old_text": old_text, "_new_text": new_text,
    }

    gen = triage.generated_reason(path, new_text)
    if ch.status == "D":
        rec["kind"], rec["_reason"] = "deleted", "file deleted"
        if is_py:
            rec["api_changes"] = pyast.api_changes(old_text, None)
    elif ch.binary:
        rec["kind"], rec["_reason"] = "binary", "binary file"
    elif gen:
        rec["kind"], rec["_reason"] = "generated", gen
    elif ch.status == "R" and ch.added == 0 and ch.removed == 0:
        rec["kind"], rec["_reason"] = "rename", f"pure rename from {ch.old_path}"
    elif is_py and ch.status in "MRT" and pyast.same_ast(old_text, new_text):
        rec["kind"], rec["_reason"] = "cosmetic", "formatting or comments only (identical AST)"
    else:
        rec["kind"] = "semantic"

    if rec["kind"] != "semantic":
        return rec

    diff = gitutil.file_diff(root, mb, head_sha, path, ch.old_path)
    added = gitutil.added_lines(diff)
    rec["hunks"] = gitutil.hunks(diff)
    rec["review_lines"] = sum(h["added"] + h["removed"] for h in rec["hunks"] if not h["trivial"])
    if include_diffs:
        if len(diff) > max_diff_bytes:
            rec["diff"], rec["diff_truncated"] = diff[:max_diff_bytes], True
        else:
            rec["diff"] = diff
    if is_py:
        rec["api_changes"] = pyast.api_changes(old_text, new_text)
    if new_text is not None:
        rec["signals"] = pyast.scan_signals(new_text, set(added), is_test=cat == "test")
    if cat == "migration" and is_py and ch.status == "A":
        issue = pyast.migration_issue(new_text)
        if issue:
            rec["_migration_issue"] = issue
    if coverage is not None and is_py and cat == "source":
        lines = _match_coverage(coverage, path)
        if lines is not None:
            measured = [ln for ln in added if ln in lines]
            rec["coverage"] = {"measured": len(measured), "uncovered": [ln for ln in measured if lines[ln] == 0]}
    return rec


def _link(rec: dict, graph: ImportGraph, changed: set[str], test_files: list[str], head_set: set[str]) -> None:
    path = rec["path"]
    if path.endswith("conftest.py"):
        base = posixpath.dirname(path)
        under = {t for t in test_files if t != path and (not base or t.startswith(base + "/"))}
        rec["dependents"] = {"direct": len(under), "transitive": len(under),
                             "outside_pr": sorted(under - changed)[:20]}
        return
    direct = graph.direct_dependents(path)
    trans = graph.transitive_dependents(path)
    rec["dependents"] = {"direct": len(direct), "transitive": len(trans), "outside_pr": sorted(direct - changed)[:20]}

    if rec["category"] == "source":
        importing = {d for d in trans if triage.is_test(d)}
        stem = posixpath.splitext(posixpath.basename(path))[0]
        for t in test_files:
            if posixpath.basename(t) in (f"test_{stem}.py", f"{stem}_test.py"):
                importing.add(t)
        rec["tests"] = {"importing": sorted(importing)[:50], "changed": sorted(importing & changed)}


# ---------------------------------------------------------------- gaps

def _gaps(recs: list[dict], graph: ImportGraph, changed: set[str], sources: dict[str, str]) -> list[dict]:
    gaps: list[dict] = []
    pyset = set(sources)

    def add(kind, severity, message, file=None, evidence=None):
        gaps.append({"kind": kind, "severity": severity, "file": file, "cluster": None,
                     "message": message, "evidence": evidence or [], "source": "static"})

    sem_src = [r for r in recs if r["kind"] == "semantic" and r["category"] == "source"]
    tests_changed = [r for r in recs if r["category"] == "test" and r["kind"] == "semantic"]
    if sem_src and not tests_changed:
        add("no_tests_changed", "high",
            f"{len(sem_src)} source file(s) changed and no test file changed.")

    for r in recs:
        path = r["path"]
        if r["kind"] == "semantic" and r["category"] == "source":
            t = r["tests"]
            if not t["changed"]:
                if r["status"] == "A":
                    add("untested_new_file", "medium", "New module with no test that imports it or follows its name.", path)
                elif not t["importing"]:
                    if r["added"] >= 5:
                        add("untested_change", "medium", "No test imports this module, and no test changed for it.", path)
                elif r["added"] >= 20 or any(not c.get("compatible") for c in r["api_changes"]):
                    add("tests_not_updated", "low",
                        f"{len(t['importing'])} test(s) import this module but none changed in this PR.",
                        path, t["importing"][:5])
            cov = r.get("coverage")
            if cov and cov["uncovered"]:
                sev = "medium" if len(cov["uncovered"]) >= 10 else "low"
                add("uncovered_lines", sev,
                    f"{len(cov['uncovered'])} of {cov['measured']} changed executable lines are not covered.",
                    path, [f"line {ln}" for ln in cov["uncovered"][:20]])

        if r["kind"] in ("deleted", "rename") or (r["status"] == "R" and r["old_path"]):
            old = r["old_path"] if r["status"] == "R" else path
            if old and old.endswith(".py"):
                names = {_chain_name(old, pyset | {old})} | set(_alt_names(old))
                users = set()
                for n in names:
                    if n:
                        users |= graph.importers_of_module_name(n)
                if users:
                    verb = "deleted" if r["kind"] == "deleted" else f"moved to {path}"
                    add("stale_module_import", "high",
                        f"{old} was {verb}, but {len(users)} file(s) still import the old module path.",
                        path, sorted(users)[:10])

        if r.get("_migration_issue"):
            add("irreversible_migration", "medium", r.pop("_migration_issue") + ".", path)
        if r["category"] == "dependency" and not triage.lockfile_reason(path) and r["kind"] == "semantic":
            locks = [c for c in changed if triage.lockfile_reason(c)]
            if not locks:
                add("dependency_without_lock", "low", "Dependency manifest changed but no lockfile changed.", path)
        if path.endswith("conftest.py") and r["kind"] == "semantic":
            add("conftest_changed", "info",
                f"conftest.py changed; fixtures here reach {r['dependents']['direct']} test file(s) without imports.", path)
        if r["kind"] == "semantic" and r["dependents"]["outside_pr"] and r["category"] == "source" \
                and any(not c.get("compatible") for c in r["api_changes"]) and r["dependents"]["direct"] >= 10:
            add("wide_blast_radius", "info",
                f"Public API changed in a module with {r['dependents']['direct']} direct importers.", path)

    for p in graph.parse_errors:
        if p in changed:
            add("syntax_error", "high", "File does not parse as Python at the head commit.", p)
    return gaps


# ---------------------------------------------------------------- symbols

def _symbols(recs, graph: ImportGraph, base_graph: ImportGraph, changed: set[str]):
    """Changed symbols with their callers, plus added/removed call edges."""
    rename = {r["old_path"]: r["path"] for r in recs if r["old_path"]}
    out: list[dict] = []
    edges_added: set[tuple[str, str]] = set()
    edges_removed: set[tuple[str, str]] = set()
    targets_by_file: dict[str, dict[str, dict]] = {}

    def head_id(i: str) -> str:
        p, q = i.split("::", 1)
        return symmod.sym_id(rename.get(p, p), q)

    for r in recs:
        if not r["path"].endswith(".py") or r["kind"] not in ("semantic", "deleted"):
            continue
        old_src, new_src = r["_old_text"], r["_new_text"]
        old_tree, new_tree = pyast.parse(old_src), pyast.parse(new_src)
        if (old_src and old_tree is None) or (new_src and new_tree is None):
            continue
        old_defs = symmod.define(old_tree, old_src) if old_tree else {}
        new_defs = symmod.define(new_tree, new_src) if new_tree else {}
        old_path = r["old_path"] or r["path"]
        old_res = symmod.Resolver(old_path, old_tree, base_graph, old_defs) if old_tree else None
        new_res = symmod.Resolver(r["path"], new_tree, graph, new_defs) if new_tree else None

        for change, a, b in symmod.changed_symbols(old_defs, new_defs):
            cur = b or a
            if cur.kind == "module" and change != "modified":
                continue
            sid = symmod.sym_id(r["path"], cur.qualname)
            # A class whose bases change is reported, but not as breaking: what
            # callers rely on (the constructor) is tracked through Class.__init__.
            breaking = change == "removed" or (change == "signature" and cur.kind != "class" and a.shape and b.shape
                                               and not pyast._compatible(a.shape, b.shape))
            owner = cur.qualname.split(".")[0] if cur.kind == "method" else None
            entry = {
                "id": sid, "path": r["path"], "qualname": cur.qualname, "kind": cur.kind, "change": change,
                "breaking": bool(breaking), "public": cur.public,
                "line": b.line if b else None, "end": b.end if b else None, "col": b.col if b else None,
                "old_line": a.line if a else None,
                "old_sig": a.sig if a else None, "new_sig": b.sig if b else None,
                "callers": [], "callers_truncated": 0, "resolution": "name",
                "callees_added": [], "callees_removed": [],
                # for methods: does the class survive, and could a base class supply the method?
                "class_removed": bool(owner and owner not in new_defs),
                "inherits": bool(owner and owner in new_defs and new_defs[owner].node.bases),
            }
            if cur.kind != "module":
                before = {head_id(x) for x in symmod.callees(a, old_res)} if a and old_res else set()
                after = symmod.callees(b, new_res) if b and new_res else set()
                entry["callees_added"] = sorted(after - before)
                entry["callees_removed"] = sorted(before - after)
                edges_added |= {(sid, t) for t in after - before}
                edges_removed |= {(sid, t) for t in before - after}
                if r["kind"] != "deleted":
                    targets_by_file.setdefault(r["path"], {})[sid] = entry
            out.append(entry)

        # which changed symbols each hunk touches
        new_spans = [(d.line, d.end, q) for q, d in new_defs.items() if q != "<module>"]
        old_spans = [(d.line, d.end, q) for q, d in old_defs.items() if q != "<module>"]
        changed_q = {e["qualname"] for e in out if e["path"] == r["path"]}
        removed_q = {e["qualname"] for e in out if e["path"] == r["path"] and e["change"] == "removed"}
        for h in r.get("hunks", []):
            hit = set()
            if h["new_len"]:
                lo, hi = h["new_start"], h["new_start"] + h["new_len"] - 1
                hit |= {q for s0, e0, q in new_spans if s0 <= hi and e0 >= lo and q in changed_q}
            if h["old_len"]:
                lo, hi = h["old_start"], h["old_start"] + h["old_len"] - 1
                hit |= {q for s0, e0, q in old_spans if s0 <= hi and e0 >= lo and q in removed_q}
            hit = sorted(hit, key=lambda q: (-q.count("."), q))
            h["symbols"] = [q for q in hit if not any(o.startswith(q + ".") for o in hit)] or (
                ["<module>"] if "<module>" in changed_q else [])
        r["symbols"] = sorted(e["id"] for e in out if e["path"] == r["path"])

    _find_callers(targets_by_file, graph)
    call_delta = {
        "added": [{"source": a, "target": b} for a, b in sorted(edges_added)[:MAX_EDGES]],
        "removed": [{"source": a, "target": b} for a, b in sorted(edges_removed)[:MAX_EDGES]],
        "added_total": len(edges_added), "removed_total": len(edges_removed),
    }
    return out, call_delta


def _find_callers(targets_by_file: dict[str, dict[str, dict]], graph: ImportGraph) -> None:
    """Name-based reference search, limited to files that import each changed file."""
    scan: dict[str, set[str]] = {}
    for path in targets_by_file:
        for user in {path} | graph.direct_dependents(path):
            scan.setdefault(user, set()).add(path)
    entries = {sid: e for t in targets_by_file.values() for sid, e in t.items()}
    for user, owners in sorted(scan.items()):
        tree = graph.trees.get(user)
        if tree is None:
            continue
        targets = {sid for o in owners for sid in targets_by_file[o]}
        methods: dict[str, set[str]] = {}
        for sid in targets:
            e = entries[sid]
            if e["kind"] == "method":
                name = e["qualname"].split(".")[-1]
                if not (name.startswith("__") and name.endswith("__")):
                    methods.setdefault(name, set()).add(sid)
        resolver = symmod.Resolver(user, tree, graph)
        for ref in symmod.scan_refs(user, tree, resolver, targets, methods):
            e = entries[ref.target]
            if ref.path == e["path"] and (ref.caller == e["qualname"]
                                          or (e["line"] and e["line"] <= ref.line <= (e["end"] or 0))):
                continue  # recursion or the definition itself
            low = [c for c in e["callers"] if c["confidence"] == "low"]
            if len(e["callers"]) >= symmod.MAX_CALLERS or (ref.confidence == "low" and len(low) >= symmod.MAX_LOW_CONFIDENCE):
                e["callers_truncated"] += 1
                continue
            e["callers"].append({"path": ref.path, "line": ref.line, "caller": ref.caller, "call": ref.call,
                                 "confidence": ref.confidence, "resolution": ref.resolution})
    for e in entries.values():
        e["callers"].sort(key=lambda c: (c["confidence"] != "high", c["path"], c["line"]))


def _symbol_counts(symbols: list[dict]) -> dict:
    out = {"added": 0, "removed": 0, "signature": 0, "modified": 0, "breaking": 0}
    for s in symbols:
        if s["kind"] == "module":
            continue
        out[s["change"]] += 1
        out["breaking"] += bool(s["breaking"])
    return out


# ---------------------------------------------------------------- structure delta

def _pkg(path: str) -> str:
    return posixpath.dirname(path) or "(root)"


def _delta(recs, graph: ImportGraph, base_graph: ImportGraph) -> dict:
    """How the module-level import graph changes. Only changed files can gain
    or lose edges, so base = head - added + removed is exact for the rest."""
    rename = {r["old_path"]: r["path"] for r in recs if r["old_path"]}
    head_e, base_e = set(), set()
    for r in recs:
        if not r["path"].endswith(".py") or r["kind"] in ("generated", "binary"):
            continue
        if r["kind"] != "deleted" and r["path"] in graph.g:
            head_e |= {(a, b) for a, b in graph.g.out_edges(r["path"])}
        old = r["old_path"] or r["path"]
        if r["status"] != "A" and old in base_graph.g:
            base_e |= {(rename.get(a, a), rename.get(b, b)) for a, b in base_graph.g.out_edges(old)}
    added, removed = head_e - base_e, base_e - head_e

    g_head = graph.g
    g_base = g_head.copy()
    g_base.remove_edges_from(added)
    g_base.add_edges_from(removed)

    def prod(a: str) -> bool:
        return not triage.is_test(a)

    cycles, seen = [], set()
    for a, b in sorted(added):
        if not (prod(a) and prod(b)) or not nx.has_path(g_head, b, a):
            continue
        if b in g_base and a in g_base and nx.has_path(g_base, a, b) and nx.has_path(g_base, b, a):
            continue  # already in a loop before this change
        loop = [a] + nx.shortest_path(g_head, b, a)
        key = frozenset(loop)
        if key in seen:
            continue
        seen.add(key)
        cycles.append({"files": loop, "edge": {"source": a, "target": b}})
        if len(cycles) >= 20:
            break

    def pairs(g) -> dict[tuple[str, str], int]:
        out: dict[tuple[str, str], int] = {}
        for a, b in g.edges():
            if prod(a) and _pkg(a) != _pkg(b):
                out[(_pkg(a), _pkg(b))] = out.get((_pkg(a), _pkg(b)), 0) + 1
        return out

    hp, bp = pairs(g_head), pairs(g_base)
    new_pkg = [{"source": s, "target": t, "edges": [{"source": a, "target": b} for a, b in sorted(added)
                                                    if prod(a) and (_pkg(a), _pkg(b)) == (s, t)][:10]}
               for (s, t) in sorted(hp) if (s, t) not in bp]
    gone_pkg = [{"source": s, "target": t, "edges": [{"source": a, "target": b} for a, b in sorted(removed)
                                                     if prod(a) and (_pkg(a), _pkg(b)) == (s, t)][:10]}
                for (s, t) in sorted(bp) if (s, t) not in hp]
    return {
        "module_edges": {
            "added": [{"source": a, "target": b} for a, b in sorted(added)[:MAX_EDGES]],
            "removed": [{"source": a, "target": b} for a, b in sorted(removed)[:MAX_EDGES]],
            "added_total": len(added), "removed_total": len(removed),
        },
        "package_edges": {"added": new_pkg, "removed": gone_pkg},
        "cycles": cycles,
    }


def _delta_gaps(delta: dict) -> list[dict]:
    gaps = []
    for c in delta["cycles"]:
        a, b = c["edge"]["source"], c["edge"]["target"]
        gaps.append({"kind": "new_import_cycle", "severity": "medium", "file": a, "cluster": None,
                     "message": f"A new import of {b} creates a loop: " + " -> ".join(c["files"]) + ".",
                     "evidence": c["files"], "source": "delta"})
    for p in delta["package_edges"]["added"]:
        first = p["edges"][0]["source"] if p["edges"] else None
        gaps.append({"kind": "new_package_dependency", "severity": "info", "file": first, "cluster": None,
                     "message": f"{p['source']} now depends on {p['target']} for the first time.",
                     "evidence": [f"{e['source']} -> {e['target']}" for e in p["edges"]], "source": "delta"})
    return gaps


# ---------------------------------------------------------------- clusters

def _build_clusters(groups, by_path, graph: ImportGraph, changed: set[str]) -> list[dict]:
    out = []
    for g in groups:
        recs = [by_path[p] for p in g]
        scores = sorted((r["score"] for r in recs), reverse=True)
        priority = scores[0] + 0.5 * (scores[1] if len(scores) > 1 else 0) + 0.25 * (scores[2] if len(scores) > 2 else 0)
        tests = sorted((r for r in recs if r["category"] == "test"), key=lambda r: (-r["score"], r["path"]))
        rest = sorted((r for r in recs if r["category"] != "test"), key=lambda r: (-r["score"], r["path"]))
        outside = set()
        for r in recs:
            outside |= graph.direct_dependents(r["path"]) - changed
        lead = sorted(rest or tests, key=lambda r: -(r["added"] + r["removed"]))[:2]
        out.append({
            "_label": ", ".join(posixpath.splitext(posixpath.basename(r["path"]))[0] for r in lead),
            "name": clustering.cluster_name(
                {r["path"] for r in rest if r["category"] in ("source", "migration")}
                or {r["path"] for r in rest} or g),
            "files": [r["path"] for r in tests + rest],
            "stats": {
                "files": len(recs),
                "added": sum(r["added"] for r in recs),
                "removed": sum(r["removed"] for r in recs),
                "max_score": scores[0],
                "mean_score": round(sum(scores) / len(scores), 1),
                "priority": round(priority, 1),
            },
            "dependents_outside_pr": len(outside),
            "summary": None,
            "findings": [],
        })
    out.sort(key=lambda c: -c["stats"]["priority"])
    counts: dict[str, int] = {}
    for c in out:
        counts[c["name"]] = counts.get(c["name"], 0) + 1
    seen: dict[str, int] = {}
    for i, c in enumerate(out, 1):
        c["id"] = f"c{i}"
        label = c.pop("_label")
        if counts[c["name"]] > 1 or c["name"] == "(root)":
            c["name"] = f"{c['name']}: {label}" if label else c["name"]
        n = seen.get(c["name"], 0)
        seen[c["name"]] = n + 1
        if n:
            c["name"] = f"{c['name']} ({n + 1})"
        for p in c["files"]:
            by_path[p]["cluster"] = c["id"]
    return [{"id": c.pop("id"), **c} for c in out]


# ---------------------------------------------------------------- coverage

def _load_coverage(path) -> dict[str, dict[int, int]]:
    """Cobertura XML (coverage.py `coverage xml`) -> filename -> {line: hits}."""
    root = ET.parse(path).getroot()
    out: dict[str, dict[int, int]] = {}
    for cls in root.iter("class"):
        fn = (cls.get("filename") or "").replace("\\", "/")
        lines = out.setdefault(fn, {})
        for line in cls.iter("line"):
            try:
                lines[int(line.get("number"))] = int(line.get("hits", "0"))
            except (TypeError, ValueError):
                continue
    return out


def _match_coverage(cov: dict[str, dict[int, int]], path: str) -> dict[int, int] | None:
    if path in cov:
        return cov[path]
    for fn, lines in cov.items():
        if path.endswith("/" + fn) or fn.endswith("/" + path):
            return lines
    return None
