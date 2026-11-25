"""Everything that can be recomputed from review.json alone: caller
annotations, symbol-based gaps, review tiers and the cost estimate.

Keeping this separate means a rigor change, or a jedi refine, only reruns
this module and never the git or AST work.
"""

from __future__ import annotations

import copy

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}

# ---------------------------------------------------------------- rigor knobs

KNOBS = {
    "deep_score": "Files at or above this risk score are marked 'read closely'.",
    "skip_score": "Files below this score, with nothing flagged, are marked 'safe to skip'.",
    "token_budget": "Upper limit on estimated tokens for AI review. Lowest-priority areas drop to 'skim' to fit. None means no limit.",
    "context_callers": "How many places that use each changed function are shown to the AI reviewer.",
    "line_budget": "Rough number of changed lines per area. Smaller means more, tighter areas.",
    "max_clusters": "Most areas the change is split into.",
    "refine": "Run the slower, more accurate usage search (jedi) as part of analysis.",
    "refine_max_symbols": "Cap on how many changed functions the accurate search checks.",
    "escalate_structural": "Always read closely when a change can break code elsewhere or creates an import loop.",
}

PRESETS = {
    "quick": {"deep_score": 70, "skip_score": 35, "token_budget": 60_000, "context_callers": 2,
              "line_budget": 4000, "max_clusters": 8, "refine": False, "refine_max_symbols": 50,
              "escalate_structural": True},
    "standard": {"deep_score": 55, "skip_score": 20, "token_budget": None, "context_callers": 3,
                 "line_budget": 2500, "max_clusters": 12, "refine": False, "refine_max_symbols": 150,
                 "escalate_structural": True},
    "thorough": {"deep_score": 35, "skip_score": 5, "token_budget": None, "context_callers": 6,
                 "line_budget": 1500, "max_clusters": 16, "refine": True, "refine_max_symbols": 600,
                 "escalate_structural": True},
}

PRESET_BLURB = {
    "quick": "A fast pass. Only the riskiest areas get a close read, and AI review is capped at about 60k tokens.",
    "standard": "Balanced. Most risky areas get a close read; quiet files are skimmed or skipped.",
    "thorough": "Careful. More areas get a close read, usages are checked with the accurate search, and the AI sees more context.",
}

# Rough reading speeds, from common code-review guidance (200-400 lines/hour).
LINES_PER_HOUR_DEEP = 300
LINES_PER_HOUR_SKIM = 1500
TOKENS_PER_LINE = 10
AGENT_OVERHEAD = 2500


def resolve_options(rigor: str | None = None, config: dict | None = None, overrides: dict | None = None) -> dict:
    """Preset, then the repo's [tool.pr-review] settings, then explicit
    overrides. An explicit `rigor` argument wins over the config's knob
    values, since choosing a preset is a deliberate act."""
    config = dict(config or {})
    name = rigor or config.pop("rigor", None) or "standard"
    config.pop("rigor", None)
    if name not in PRESETS:
        raise ValueError(f"unknown rigor '{name}', pick one of {', '.join(PRESETS)}")
    opts = dict(PRESETS[name])
    if not rigor:
        opts.update({k: v for k, v in config.items() if k in KNOBS})
    opts.update({k: v for k, v in (overrides or {}).items() if k in KNOBS and v is not None})
    if opts.get("token_budget") in (0, "none", "None"):
        opts["token_budget"] = None
    opts["rigor"] = name
    return opts


# ---------------------------------------------------------------- callers

def _changed_ranges(review: dict) -> dict[str, list[tuple[int, int]]]:
    out = {}
    for f in review["files"]:
        if f["kind"] == "semantic":
            out[f["path"]] = [(h["new_start"], h["new_start"] + max(h["new_len"], 1) - 1)
                              for h in f.get("hunks", []) if not h["trivial"]]
    return out


def annotate_callers(review: dict) -> None:
    """Mark each caller as inside the PR or not, and whether its line was edited."""
    changed = {f["path"] for f in review["files"]}
    ranges = _changed_ranges(review)
    for s in review.get("symbols", []):
        untouched = 0
        for c in s["callers"]:
            c["in_pr"] = c["path"] in changed
            c["touched"] = any(a <= c["line"] <= b for a, b in ranges.get(c["path"], ()))
            if c["confidence"] == "high" and not c["touched"]:
                untouched += 1
        s["untouched_callers"] = untouched
        s["high_callers"] = sum(1 for c in s["callers"] if c["confidence"] == "high")


# ---------------------------------------------------------------- symbol gaps

def symbol_gaps(review: dict) -> list[dict]:
    gaps = []
    by_path = {f["path"]: f for f in review["files"]}
    deleted = {f["path"] for f in review["files"] if f["kind"] == "deleted"}

    def add(kind, severity, message, s, evidence):
        f = by_path.get(s["path"])
        gaps.append({"kind": kind, "severity": severity, "file": s["path"], "symbol": s["id"],
                     "cluster": f.get("cluster") if f else None, "message": message,
                     "evidence": evidence, "source": "symbols"})

    for s in review.get("symbols", []):
        if s["kind"] == "module":
            continue
        live = [c for c in s["callers"] if c["path"] not in deleted]
        sure = [c for c in live if c["confidence"] == "high"]
        maybe = [c for c in live if c["confidence"] == "low"]
        where = lambda cs: [f"{c['path']}:{c['line']}" for c in cs[:12]]  # noqa: E731
        if s["change"] == "removed":
            if s.get("class_removed"):
                continue  # reported once, on the class itself
            if s.get("inherits"):
                if sure or maybe:
                    add("removed_override", "low",
                        f"`{s['qualname']}` was removed, but its class has a base class that may provide it. "
                        f"{len(sure) + len(maybe)} place(s) call it.", s, where(sure + maybe))
                continue
            if sure:
                add("removed_symbol_still_used", "high",
                    f"`{s['qualname']}` was removed, but {len(sure)} place(s) still use it.", s, where(sure))
            elif maybe and s["kind"] == "method":
                add("removed_method_maybe_used", "low",
                    f"`{s['qualname']}` was removed. {len(maybe)} call(s) to a method with the same name may be affected.",
                    s, where(maybe))
        elif s["change"] == "signature" and s["breaking"]:
            stale = [c for c in sure if not c["touched"]]
            if stale:
                outside = sum(1 for c in stale if not c["in_pr"])
                add("breaking_change_untouched_callers", "medium",
                    f"`{s['qualname']}` changed in a way existing callers may not survive. "
                    f"{len(stale)} call site(s) were not edited ({outside} in files outside this change).",
                    s, [f"{s['old_sig']}  ->  {s['new_sig']}"] + where(stale))
            elif maybe:
                add("breaking_change_maybe_callers", "low",
                    f"`{s['qualname']}` changed in a way callers may not survive. "
                    f"{len(maybe)} call(s) to a method with the same name could be affected.", s, where(maybe))
    return gaps


FOLDABLE = {"untested_change", "untested_new_file", "tests_not_updated", "uncovered_lines"}
FOLD_LABELS = {
    "untested_change": "changed modules have no test importing them and no test changed",
    "untested_new_file": "new modules have no test",
    "tests_not_updated": "modules have tests that import them, but none of those tests changed",
    "uncovered_lines": "modules have changed lines that coverage does not reach",
}


def fold_repeated(gaps: list[dict], threshold: int = 4) -> list[dict]:
    """Fold each (kind, cluster) group above the threshold into one entry."""
    groups: dict[tuple, list[dict]] = {}
    for g in gaps:
        if g["kind"] in FOLDABLE and g.get("file"):
            groups.setdefault((g["kind"], g.get("cluster")), []).append(g)
    out = [g for g in gaps if not (g["kind"] in FOLDABLE and g.get("file")
                                   and len(groups[(g["kind"], g.get("cluster"))]) > threshold)]
    for (kind, cluster), items in groups.items():
        if len(items) <= threshold:
            continue
        files = sorted(g["file"] for g in items)
        out.append({
            "kind": kind, "severity": min((g["severity"] for g in items), key=SEVERITY_ORDER.get),
            "file": None, "cluster": cluster, "files": files, "source": items[0].get("source", "static"),
            "message": f"{len(files)} {FOLD_LABELS[kind]}.",
            "evidence": files[:12] + ([f"... {len(files) - 12} more"] if len(files) > 12 else []),
        })
    return out


def sort_gaps(gaps: list[dict]) -> list[dict]:
    return sorted(gaps, key=lambda g: (SEVERITY_ORDER[g["severity"]], g.get("file") or "", g["kind"]))


# ---------------------------------------------------------------- tiers

def _file_tier(f, gaps, syms, o) -> tuple[str, list[str]]:
    if f["kind"] != "semantic":
        return "skip", ["collapsed: nothing to read"]
    sev = {g["severity"] for g in gaps}
    reasons, deep = [], False
    if f["score"] >= o["deep_score"]:
        deep = True
        reasons.append(f"risk score {f['score']} is at or above the close-read line ({o['deep_score']})")
    if "high" in sev:
        deep = True
        reasons.append("has a high-severity item to check")
    breaking = [s for s in syms if s.get("breaking") and s.get("untouched_callers")]
    if breaking and o.get("escalate_structural", True):
        deep = True
        n = sum(s["untouched_callers"] for s in breaking)
        reasons.append(f"changes {len(breaking)} function(s) in a way that can break {n} place(s) not edited here")
    if deep:
        return "deep", reasons
    if f["score"] < o["skip_score"] and not (sev & {"high", "medium", "low"}) and not any(s.get("breaking") for s in syms):
        return "skip", [f"risk score {f['score']} is below the skip line ({o['skip_score']}) and nothing is flagged"]
    why = [f"risk score {f['score']} is between the skip line ({o['skip_score']}) and the close-read line ({o['deep_score']})"]
    if "medium" in sev:
        why.append("has a medium-severity item to check")
    return "skim", why


def _file_tokens(f, syms, o) -> int:
    lines = f.get("review_lines", f["added"] + f["removed"])
    ctx = sum(40 + min(len(s["callers"]), o["context_callers"]) * 60 for s in syms if s["kind"] != "module")
    return lines * TOKENS_PER_LINE + ctx


def apply_tiers(review: dict, opts: dict | None = None) -> dict:
    """Assign skip/skim/deep to every file and cluster and estimate cost."""
    o = dict(PRESETS["standard"])
    o.update(review.get("rigor") or {})
    o.update(opts or {})
    files = {f["path"]: f for f in review["files"]}
    gaps_by_file: dict[str, list] = {}
    for g in review["gaps"]:
        for p in ([g["file"]] if g.get("file") else []) + g.get("files", []):
            gaps_by_file.setdefault(p, []).append(g)
    syms_by_file: dict[str, list] = {}
    for s in review.get("symbols", []):
        syms_by_file.setdefault(s["path"], []).append(s)
    cycle_files = {p for c in review.get("delta", {}).get("cycles", []) for p in c["files"]}

    for f in review["files"]:
        f["tier"], f["tier_reasons"] = _file_tier(f, gaps_by_file.get(f["path"], []), syms_by_file.get(f["path"], []), o)

    for c in review["clusters"]:
        # Tests next to code that needs a close read say what that code should do.
        if any(files[p]["tier"] == "deep" for p in c["files"]):
            for p in c["files"]:
                if files[p]["category"] == "test" and files[p]["tier"] == "skip":
                    files[p]["tier"] = "skim"
                    files[p]["tier_reasons"] = ["tests for code in this area that needs a close read"]
        tiers = [files[p]["tier"] for p in c["files"]]
        reasons = []
        cgaps = [g for g in review["gaps"] if g.get("cluster") == c["id"] and g["severity"] != "info"]
        loops = [p for p in c["files"] if p in cycle_files]
        if "deep" in tiers:
            tier = "deep"
            reasons.append(f"{tiers.count('deep')} file(s) need a close read")
        elif loops and o.get("escalate_structural", True):
            tier = "deep"
            reasons.append("part of a new import loop")
        elif all(t == "skip" for t in tiers) and not cgaps:
            tier = "skip"
            reasons.append("every file is low risk and nothing is flagged")
        else:
            tier = "skim"
            reasons.append(f"{tiers.count('skim')} file(s) worth a skim, none need a close read")
        c["tier"], c["tier_reasons"] = tier, reasons
        c["has_high"] = any(g["severity"] == "high" for g in cgaps)
        deep_files = [files[p] for p in c["files"] if files[p]["tier"] == "deep"]
        other = [files[p] for p in c["files"] if files[p]["tier"] != "deep"]
        # files that aren't read closely get a short summary, never more than the file itself
        c["est_tokens"] = (AGENT_OVERHEAD + sum(_file_tokens(f, syms_by_file.get(f["path"], []), o) for f in deep_files)
                           + sum(min(150, _file_tokens(f, syms_by_file.get(f["path"], []), o)) for f in other)
                           ) if tier == "deep" else 0

    demoted = []
    budget = o.get("token_budget")
    if budget:
        deep = sorted((c for c in review["clusters"] if c["tier"] == "deep"),
                      key=lambda c: (c["has_high"], c["stats"]["priority"]))
        total = sum(c["est_tokens"] for c in deep)
        for c in deep:
            if total <= budget:
                break
            total -= c["est_tokens"]
            c["tier"], c["est_tokens"] = "skim", 0
            c["tier_reasons"] = ["moved to skim to stay within the AI token budget"] + c["tier_reasons"]
            demoted.append(c["id"])
            for p in c["files"]:
                if files[p]["tier"] == "deep":
                    files[p]["tier"] = "skim"
                    files[p]["tier_reasons"] = ["its area was moved to skim to fit the token budget"] + files[p]["tier_reasons"]

    sem = [f for f in review["files"] if f["kind"] == "semantic"]
    lines = lambda fs: sum(f.get("review_lines", f["added"] + f["removed"]) for f in fs)  # noqa: E731
    deep_lines = lines([f for f in sem if f["tier"] == "deep"])
    skim_lines = lines([f for f in sem if f["tier"] == "skim"])
    review["rigor"] = {k: o[k] for k in list(KNOBS) + ["rigor"] if k in o}
    review["budget"] = {
        "clusters": {t: sum(1 for c in review["clusters"] if c["tier"] == t) for t in ("deep", "skim", "skip")},
        "files": {t: sum(1 for f in sem if f["tier"] == t) for t in ("deep", "skim", "skip")},
        "lines_deep": deep_lines,
        "lines_skim": skim_lines,
        "lines_total": lines(sem),
        "est_minutes": round(60 * deep_lines / LINES_PER_HOUR_DEEP + 60 * skim_lines / LINES_PER_HOUR_SKIM),
        "est_tokens": sum(c["est_tokens"] for c in review["clusters"]),
        "baseline_tokens": AGENT_OVERHEAD + lines(sem) * TOKENS_PER_LINE,
        "token_budget": budget,
        "demoted": demoted,
    }
    return review["budget"]


def refresh(review: dict, opts: dict | None = None) -> dict:
    """Recompute caller flags, symbol gaps, tiers and budget in place."""
    annotate_callers(review)
    kept = [g for g in review["gaps"] if g.get("source") != "symbols"]
    review["gaps"] = sort_gaps(kept + symbol_gaps(review))
    apply_tiers(review, opts)
    return review


def preview(review: dict, opts: dict) -> dict:
    """Tiers and budget for different knobs, without touching `review`."""
    r = copy.deepcopy(review)
    apply_tiers(r, opts)
    return r
