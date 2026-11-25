"""Per-file risk score, 0-100. Each component is capped so no single factor
dominates, and every point comes with a reason a reviewer can check."""

from __future__ import annotations

import math

from pr_review import triage
from pr_review.pyast import SIGNALS

CATEGORY_FACTOR = {
    "source": 1.0, "migration": 1.0, "dependency": 1.0, "ci": 0.9,
    "config": 0.8, "test": 0.5, "other": 0.6, "docs": 0.2,
}


def score_file(rec: dict) -> tuple[int, list[str]]:
    reasons: list[str] = []
    total = 0.0

    churn = rec["added"] + rec["removed"]
    pts = min(25.0, 4 * math.log2(1 + churn))
    if pts >= 1:
        total += pts
        reasons.append(f"+{pts:.0f} churn ({rec['added']}+/{rec['removed']}-)")

    deps = rec.get("dependents", {}).get("transitive", 0)
    pts = min(20.0, 5 * math.log2(1 + deps))
    if pts >= 1:
        total += pts
        reasons.append(f"+{pts:.0f} imported by {deps} module(s), directly or indirectly")

    cat = rec["category"]
    sens = triage.sensitive_reason(rec["path"])
    if sens:
        total += 15
        reasons.append(f"+15 {sens}")
    if churn == 0:
        pass
    elif cat == "migration":
        total += 12
        reasons.append("+12 database migration")
    elif cat == "dependency":
        total += 10
        reasons.append("+10 dependency manifest")
    elif cat == "ci":
        total += 8
        reasons.append("+8 CI or build config")
    if rec["path"].endswith("conftest.py"):
        total += 10
        reasons.append("+10 conftest.py (implicit fixture dependency)")

    api = rec["api_changes"] if cat == "source" else []
    breaking = [c for c in api if not c.get("compatible", c["change"] == "added")]
    soft = [c for c in api if c not in breaking]
    pts = min(20.0, 8 * len(breaking) + 2 * len(soft))
    if pts:
        total += pts
        reasons.append(f"+{pts:.0f} public API: {len(breaking)} breaking, {len(soft)} added or backward compatible")

    sig_pts = 0
    kinds: dict[str, int] = {}
    for s in rec["signals"]:
        kinds[s["kind"]] = kinds.get(s["kind"], 0) + 1
    for kind, n in kinds.items():
        w = SIGNALS[kind].weight
        sig_pts += w + (n - 1) * max(1, w // 3)
    sig_pts = min(25, sig_pts)
    if sig_pts:
        total += sig_pts
        top = ", ".join(f"{SIGNALS[k].label} x{n}" for k, n in sorted(kinds.items(), key=lambda kv: -SIGNALS[kv[0]].weight)[:3])
        reasons.append(f"+{sig_pts} risk patterns ({top})")

    tests = rec.get("tests") or {}
    if cat == "source" and rec["kind"] == "semantic" and not tests.get("changed"):
        pts = 10 if not tests.get("importing") else 5
        total += pts
        reasons.append(f"+{pts} no test in this PR touches it" + ("" if tests.get("importing") else " (and no test imports it)"))

    cov = rec.get("coverage")
    if cov and cov.get("measured"):
        frac = len(cov["uncovered"]) / cov["measured"]
        pts = round(15 * frac)
        if pts:
            total += pts
            reasons.append(f"+{pts} {len(cov['uncovered'])}/{cov['measured']} changed lines uncovered")

    factor = CATEGORY_FACTOR.get(cat, 1.0)
    if factor != 1.0:
        reasons.append(f"x{factor} {cat} file")
    return int(round(min(100.0, total * factor))), reasons
