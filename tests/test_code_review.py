"""Handing bug-finding to Claude Code's /code-review: the plan and the import."""

import json

import pytest
from click.testing import CliRunner

from pr_review import analyze, findings, plan, report
from pr_review.cli import main


@pytest.fixture
def review(shop_repo):
    return analyze(shop_repo.root, base="main")


def test_plan_single_pass(review):
    p = plan.build_plan(review)
    assert p["mode"] == "single" and p["effort"] == "high"
    assert p["single_command"].startswith(f"/code-review high --max-findings all {review['meta']['merge_base_sha'][:12]}..")
    assert "src/shop/auth/session.py" in p["single_command"]
    assert p["areas"][0]["id"] == "c1" and "--areas c1" in p["import_command"]


def test_plan_follows_rigor_and_overrides(review):
    review["rigor"]["rigor"] = "thorough"
    assert plan.build_plan(review)["effort"] == "xhigh"
    p = plan.build_plan(review, mode="per-area", effort="max")
    assert p["mode"] == "per-area" and p["areas"][0]["command"].startswith("/code-review max")


def test_plan_is_empty_once_reviewed(review):
    findings.add(review, [], cluster="c1")
    assert plan.build_plan(review)["mode"] == "nothing"


def test_parse_claude_json_envelope_with_prose():
    env = {"type": "result", "result": (
        "Found 2 issues.\n\n"
        "1. **Removed function still imported** - `src/shop/report.py:1`\n"
        "   Importing the module raises ImportError.\n\n"
        "2. Nit: tuple default at /repo/src/a.py:7\n\nNothing else.")}
    items = findings.parse_external(json.dumps(env))
    assert [(i["file"], i["line"]) for i in items] == [("src/shop/report.py", 1), ("/repo/src/a.py", 7)]
    assert items[0]["severity"] == "high" and items[0]["title"] == "Removed function still imported"
    assert items[1]["severity"] == "info"


def test_parse_claude_findings_list():
    raw = json.dumps([{"file": "a.py", "line": 3, "summary": "s", "failure_scenario": "f", "severity": "important",
                       "verdict": "CONFIRMED", "category": "correctness", "short_summary": "Short"}])
    (item,) = findings.parse_external(raw)
    assert item["severity"] == "high" and item["confidence"] == "high" and item["title"] == "Short"
    assert item["category"] == "correctness" and "f" in item["detail"]


def test_parse_fenced_json_and_our_format():
    text = "Here you go:\n```json\n{\"findings\": [{\"severity\": \"low\", \"title\": \"t\", \"file\": \"x.py\"}]}\n```"
    assert findings.parse_external(text)[0]["title"] == "t"
    assert findings.parse_external("") == []
    assert findings.parse_external("no locations here at all") == []


def test_import_routes_by_area_and_skips_bad_items(review):
    root = review["meta"]["repo_path"]
    items = [
        {"severity": "high", "title": "secret", "file": f"{root}/src/shop/auth/session.py", "line": 4},
        {"severity": "medium", "title": "caller", "file": "./src/shop/report.py", "line": 6},  # not in the PR
        {"severity": "low", "title": "ghost", "file": "src/shop/ghost.py", "line": 1},
    ]
    res = findings.import_findings(review, items, areas=["c1"])
    assert res["by_area"] == {"c1": 1, "PR": 1} and len(res["skipped"]) == 1
    assert review["clusters"][0]["findings"][0]["file"] == "src/shop/auth/session.py"
    assert review["findings"][0]["file"] == "src/shop/report.py"
    assert findings.status(review)[0]["reviewed"]
    with pytest.raises(findings.FindingError, match="no area c9"):
        findings.import_findings(review, [], areas=["c9"])


def test_import_with_no_findings_still_marks_areas_reviewed(review):
    findings.import_findings(review, [], areas=["c1"])
    assert findings.status(review)[0]["reviewed"] and review["clusters"][0]["findings"] == []


def test_cli_plan_and_import(shop_repo, monkeypatch, tmp_path):
    monkeypatch.chdir(shop_repo.root)
    run = CliRunner().invoke
    assert run(main, ["analyze", "--base", "main", "--quiet"]).exit_code == 0
    out = run(main, ["plan"]).output
    assert "One pass over the whole change" in out and "/code-review high" in out
    p = json.loads(run(main, ["plan", "--json"]).output)
    assert p["mode"] == "single"
    src = tmp_path / "cr.json"
    src.write_text(json.dumps({"type": "result", "result": "1. **Live key committed** `src/shop/auth/session.py:4`"}))
    res = run(main, ["findings", "import", "--areas", "c1", str(src)])
    assert res.exit_code == 0 and "1 new" in res.output
    saved = report.load(shop_repo.root / ".pr-review/feature/review.json")
    assert saved["clusters"][0]["findings"][0]["source"] == "code-review"
    assert "Every close-read area already has an up-to-date review" in run(main, ["plan"]).output
