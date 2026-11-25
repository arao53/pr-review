"""Turn an analysis into a review plan for Claude Code's built-in /code-review.

`/code-review` does the bug-finding. This module decides what to point it at:
which commits, how hard to look (effort), and whether one pass over the
whole change is enough or the close-read areas should be reviewed one at a
time. Areas marked skim or skip are left to the static checks.
"""

from __future__ import annotations

EFFORT = {"quick": "low", "standard": "high", "thorough": "xhigh"}
# One /code-review pass handles this much close-read code well; past it,
# review area by area so each pass stays focused.
SINGLE_PASS_LINES = 4000
SINGLE_PASS_AREAS = 6
MAX_FOCUS_FILES = 15


def build_plan(review: dict, mode: str | None = None, effort: str | None = None) -> dict:
    meta, budget = review["meta"], review.get("budget") or {}
    rigor = (review.get("rigor") or {}).get("rigor", "standard")
    effort = effort or EFFORT.get(rigor, "high")
    rng = f"{meta['merge_base_sha'][:12]}..{meta['head_sha'][:12]}"
    files = {f["path"]: f for f in review["files"]}
    from pr_review.findings import status
    todo = {s["id"] for s in status(review) if s["needs_review"]}

    areas = []
    for c in review["clusters"]:
        if c["id"] not in todo:
            continue
        ordered = sorted(c["files"], key=lambda p: ({"deep": 0, "skim": 1}.get(files[p].get("tier"), 2), -files[p]["score"]))
        focus = [p for p in ordered if files[p].get("tier") == "deep"] or ordered[:3]
        lines = sum(files[p].get("review_lines", 0) for p in focus)
        areas.append({"id": c["id"], "name": c["name"], "focus": focus[:MAX_FOCUS_FILES], "lines": lines,
                      "command": f"/code-review {effort} --max-findings all {rng} "
                                 f"(only these files: {', '.join(focus[:MAX_FOCUS_FILES])})"})

    total = sum(a["lines"] for a in areas)
    if mode is None:
        mode = "single" if total <= SINGLE_PASS_LINES and len(areas) <= SINGLE_PASS_AREAS else "per-area"
    focus_all = [p for a in areas for p in a["focus"]][:MAX_FOCUS_FILES]
    single = f"/code-review {effort} --max-findings all {rng}" + (
        f" (start with these files: {', '.join(focus_all)})" if focus_all else "")
    return {
        "range": rng,
        "base": meta["base"],
        "head": meta["head"],
        "effort": effort,
        "mode": mode if areas else "nothing",
        "areas": areas,
        "close_read_lines": total,
        "single_command": single,
        "ultra_command": f"/code-review ultra {meta['base']}",
        "import_command": "pr-review findings import --areas " + ",".join(a["id"] for a in areas) + " <file>",
        "not_reviewed": {"skim_files": budget.get("files", {}).get("skim", 0),
                         "skip_files": budget.get("files", {}).get("skip", 0) + review["stats"]["collapsed_files"]},
    }
