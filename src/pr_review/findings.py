"""Reviewer findings: validate, store, and keep them across re-analysis.

Findings live in review.json at three levels: the whole PR, each area
(cluster) and each file. An area remembers the content hash it was reviewed
at, so after new commits only areas whose code changed need another look.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pr_review import derive
from pr_review.pack import area_hash

SEVERITIES = ("high", "medium", "low", "info")
CONFIDENCE = ("high", "medium", "low")


class FindingError(ValueError):
    pass


def normalize(item: dict, review: dict, index: int = 0) -> dict:
    """Check one finding and return the stored form. Raises FindingError with
    a message a model can act on."""
    where = f"finding {index + 1}"
    if not isinstance(item, dict):
        raise FindingError(f"{where}: expected an object, got {type(item).__name__}")
    sev = str(item.get("severity", "")).lower()
    if sev not in SEVERITIES:
        raise FindingError(f"{where}: severity must be one of {', '.join(SEVERITIES)}")
    title = str(item.get("title") or "").strip()
    if not title:
        raise FindingError(f"{where}: title is required")
    out = {"severity": sev, "title": title[:200]}
    if item.get("detail"):
        out["detail"] = str(item["detail"]).strip()
    path = item.get("file")
    if path:
        known = {f["path"] for f in review["files"]}
        if path not in known and not _exists_at_head(review, path):
            raise FindingError(f"{where}: file '{path}' is not in this PR or in the head commit")
        out["file"] = path
    line = item.get("line")
    if line is not None:
        try:
            line = int(line)
        except (TypeError, ValueError):
            raise FindingError(f"{where}: line must be a number") from None
        if not path:
            raise FindingError(f"{where}: a line needs a file")
        if line < 1:
            raise FindingError(f"{where}: line must be 1 or more")
        out["line"] = line
    conf = str(item.get("confidence") or "medium").lower()
    if conf not in CONFIDENCE:
        raise FindingError(f"{where}: confidence must be one of {', '.join(CONFIDENCE)}")
    out["confidence"] = conf
    if item.get("suggestion"):
        out["suggestion"] = str(item["suggestion"]).strip()
    out["source"] = str(item.get("source") or "claude")
    return out


def _exists_at_head(review: dict, path: str) -> bool:
    from pr_review import gitutil
    root = review["meta"].get("repo_path")
    if not root:
        return False
    try:
        with gitutil.BlobReader(root) as r:
            return r.read(review["meta"]["head_sha"], path) is not None
    except (OSError, gitutil.GitError):
        return False


def _key(f: dict) -> tuple:
    return (f.get("file"), f.get("line"), f["title"].lower())


def add(review: dict, items: list[dict], cluster: str | None = None, summary: str | None = None,
        replace: bool = True) -> dict:
    """Store findings for an area (or the whole PR when cluster is None).

    replace=True drops the area's earlier findings first, so a re-review
    doesn't pile up duplicates. Returns counts."""
    target = review
    if cluster:
        target = next((c for c in review["clusters"] if c["id"] == cluster), None)
        if target is None:
            raise FindingError(f"no area '{cluster}'; have {', '.join(c['id'] for c in review['clusters'])}")
    clean = [normalize(x, review, i) for i, x in enumerate(items)]

    files = {f["path"]: f for f in review["files"]}
    if replace:
        old = {_key(f) for f in target.get("findings", [])}
        target["findings"] = []
        for f in files.values():
            f["findings"] = [x for x in f.get("findings", []) if _key(x) not in old]
    seen = {_key(f) for f in target.get("findings", [])}
    added = 0
    for f in clean:
        if _key(f) in seen:
            continue
        seen.add(_key(f))
        target.setdefault("findings", []).append(f)
        if f.get("file") in files:
            files[f["file"]].setdefault("findings", []).append(f)
        added += 1
    target["findings"].sort(key=lambda f: (derive.SEVERITY_ORDER[f["severity"]], f.get("file") or "", f.get("line") or 0))
    if summary is not None:
        target["summary"] = summary.strip() or None
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if cluster:
        target["review"] = {"hash": area_hash(review, target), "at": now}
        target.pop("previous", None)
    else:
        review["meta"]["reviewed_at"] = now
    return {"added": added, "total": len(target["findings"])}


def status(review: dict) -> list[dict]:
    """Every area with whether an agent has reviewed its current code."""
    out = []
    for c in review["clusters"]:
        h = area_hash(review, c)
        done = (c.get("review") or {}).get("hash") == h
        out.append({"id": c["id"], "name": c["name"], "tier": c.get("tier"), "est_tokens": c.get("est_tokens", 0),
                    "files": c["stats"]["files"], "reviewed": done,
                    "stale": not done and bool(c.get("review") or c.get("previous")),
                    "findings": len(c.get("findings", [])), "needs_review": c.get("tier") == "deep" and not done})
    return out


def carry_over(old: dict, new: dict) -> int:
    """Bring area reviews forward from an earlier review.json.

    An area whose code is unchanged keeps its review as is (matched by
    content, so renumbered areas still match). An area whose code changed
    gets the closest earlier review as `previous`, so the next reviewer can
    re-check those findings. The PR-level summary is kept only when every
    reviewed area carried over. Returns the number of areas kept as is."""
    reviewed = [c for c in old.get("clusters", []) if (c.get("review") or {}).get("hash")]
    by_hash = {c["review"]["hash"]: c for c in reviewed}
    files = {f["path"]: f for f in new["files"]}
    kept, used = 0, set()
    for c in new["clusters"]:
        prev = by_hash.get(area_hash(new, c))
        if prev:
            c["findings"] = prev.get("findings", [])
            c["summary"] = prev.get("summary")
            c["review"] = prev["review"]
            for f in c["findings"]:
                if f.get("file") in files:
                    files[f["file"]].setdefault("findings", []).append(f)
            used.add(id(prev))
            kept += 1
            continue
        mine = set(c["files"])
        best, overlap = None, 0.0
        for o in reviewed:
            theirs = set(o["files"])
            j = len(mine & theirs) / max(1, len(mine | theirs))
            if j > overlap:
                best, overlap = o, j
        if best is not None and overlap >= 0.3:
            c["previous"] = {"findings": best.get("findings", []), "summary": best.get("summary"),
                             "at": best["review"].get("at"), "overlap": round(overlap, 2)}
    if reviewed and kept == len(reviewed) and old.get("summary"):
        new["summary"] = old["summary"]
        new["findings"] = old.get("findings", [])
    return kept
