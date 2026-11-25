"""The Claude skill's building blocks: packs, findings, status, carry-over, install."""

import json
import re
from pathlib import Path

import jsonschema
import pytest
from click.testing import CliRunner

from pr_review import analyze, findings, pack, report
from pr_review.cli import main

SKILL = Path(__file__).parents[1] / "src/pr_review/skill"


@pytest.fixture
def review(shop_repo):
    return analyze(shop_repo.root, base="main")


def test_pack_has_what_a_reviewer_needs(review):
    p = pack.build_pack(review, "c1")
    t = p["text"]
    assert review["meta"]["head_sha"][:12] in t
    assert "## Read closely: `src/shop/pricing.py`" in t
    assert "before: `def apply_discount(order, pct)`" in t
    assert "call site `src/shop/report.py:6`" in t and "return apply_discount(order, 0.1)" in t
    assert "legacy_total` was removed" in t  # static gap carried in
    assert "```diff" in t and p["tokens"] > 0 and len(p["hash"]) == 16


def test_pack_respects_size_cap(review):
    full = pack.build_pack(review, "c1")
    small = pack.build_pack(review, "c1", max_tokens=1000)
    assert small["dropped_diffs"] and small["tokens"] < full["tokens"]
    assert "diff left out to fit the size limit" in small["text"]


def test_pack_leaves_out_formatting_hunks():
    f = {"diff": "diff --git a/x b/x\n@@ -1,1 +1,1 @@\n-a=1\n+a = 1\n@@ -9,1 +9,1 @@\n-b = 1\n+b = 2\n",
         "hunks": [{"trivial": True, "symbols": []}, {"trivial": False, "symbols": ["f"]}]}
    out = pack._nontrivial_diff(f)
    assert "a = 1" not in out and "+b = 2" in out and "[in f]" in out


def test_cross_pack_lists_breaking_changes_used_elsewhere(review):
    t = pack.build_cross(review)
    assert "`apply_discount` (signature, area c1) is used by code outside the PR" in t
    assert "not reviewed by an agent" in t


def test_findings_validation(review):
    with pytest.raises(findings.FindingError, match="severity"):
        findings.add(review, [{"severity": "urgent", "title": "x"}], cluster="c1")
    with pytest.raises(findings.FindingError, match="not in this PR"):
        findings.add(review, [{"severity": "low", "title": "x", "file": "nope.py"}], cluster="c1")
    with pytest.raises(findings.FindingError, match="needs a file"):
        findings.add(review, [{"severity": "low", "title": "x", "line": 3}], cluster="c1")
    # a file that exists at head but isn't in the PR is allowed
    findings.add(review, [{"severity": "low", "title": "x", "file": "src/shop/report.py", "line": 6}], cluster="c1")


def test_findings_replace_and_attach_to_files(review):
    item = {"severity": "high", "title": "Secret committed", "file": "src/shop/auth/session.py", "line": 4}
    findings.add(review, [item], cluster="c1", summary="s")
    findings.add(review, [item], cluster="c1")
    c = review["clusters"][0]
    assert len(c["findings"]) == 1 and c["summary"] == "s"
    f = next(x for x in review["files"] if x["path"] == "src/shop/auth/session.py")
    assert len(f["findings"]) == 1
    findings.add(review, [], cluster="c1")
    assert c["findings"] == [] and f["findings"] == []
    jsonschema.validate(review, report.schema())


def test_status_and_carry_over(shop_repo):
    r1 = analyze(shop_repo.root, base="main")
    assert [s["needs_review"] for s in findings.status(r1)] == [True]
    findings.add(r1, [{"severity": "high", "title": "t", "file": "src/shop/pricing.py"}], cluster="c1")
    assert findings.status(r1)[0]["reviewed"]

    shop_repo.commit({"docs/notes.md": "# notes\n"})  # docs don't invalidate a review
    r2 = analyze(shop_repo.root, base="main")
    assert findings.carry_over(r1, r2) == 1
    assert findings.status(r2)[0]["reviewed"] and r2["clusters"][0]["findings"]

    shop_repo.commit({"src/shop/new_feature.py": "def ship(order):\n    return 2\n"})
    r3 = analyze(shop_repo.root, base="main")
    assert findings.carry_over(r2, r3) == 0
    st = findings.status(r3)[0]
    assert st["needs_review"] and st["stale"]
    assert "Found by an earlier review" in pack.build_pack(r3, "c1")["text"]


def test_cli_loop(shop_repo, monkeypatch, tmp_path):
    monkeypatch.chdir(shop_repo.root)
    run = CliRunner().invoke
    assert run(main, ["analyze", "--base", "main", "--quiet"]).exit_code == 0
    st = json.loads(run(main, ["status", "--json"]).output)
    assert st["areas"][0]["needs_review"] and st["review"].endswith("review.json")
    res = run(main, ["pack", "c1"])
    assert res.exit_code == 0 and "# Area c1" in res.output
    assert run(main, ["pack", "c9"]).exit_code != 0
    data = {"summary": "area ok", "findings": [{"severity": "medium", "title": "t", "file": "src/shop/pricing.py",
                                               "line": 4, "confidence": "high"}]}
    src = tmp_path / "f.json"
    src.write_text(json.dumps(data))
    res = run(main, ["findings", "add", "--area", "c1", str(src)])
    assert res.exit_code == 0 and "Saved 1 finding" in res.output
    bad = run(main, ["findings", "add", "--area", "c1", "-"], input='[{"severity": "nope", "title": "x"}]')
    assert bad.exit_code != 0 and "Nothing was saved" in bad.output
    assert run(main, ["findings", "add", "-"], input="[]").exit_code != 0  # needs --area or --pr
    assert run(main, ["findings", "add", "--pr", "-"], input='{"summary": "fine", "findings": []}').exit_code == 0
    assert "done" in run(main, ["status"]).output
    # findings survive an unrelated re-analysis
    out = run(main, ["analyze", "--base", "main", "--quiet"]).output
    assert "Kept earlier agent findings for 1 area" in out
    saved = report.load(shop_repo.root / ".pr-review/feature/review.json")
    assert saved["summary"] == "fine" and saved["clusters"][0]["summary"] == "area ok"
    assert "area ok" in (shop_repo.root / ".pr-review/feature/report.html").read_text()


def test_install_skill(shop_repo, monkeypatch, tmp_path):
    monkeypatch.chdir(shop_repo.root)
    monkeypatch.setenv("HOME", str(tmp_path))
    run = CliRunner().invoke
    assert run(main, ["install-skill"]).exit_code == 0
    assert (tmp_path / ".claude/skills/pr-review/SKILL.md").exists()
    assert run(main, ["install-skill"]).exit_code != 0  # no silent overwrite
    assert run(main, ["install-skill", "--force"]).exit_code == 0
    assert run(main, ["install-skill", "--project"]).exit_code == 0
    assert (shop_repo.root / ".claude/skills/pr-review/reviewer.md").exists()


def test_skill_files_are_well_formed():
    text = (SKILL / "SKILL.md").read_text()
    m = re.match(r"^---\nname: (.+)\ndescription: (.+)\n---\n", text)
    assert m and m.group(1) == "pr-review" and 50 < len(m.group(2)) < 1024
    reviewer = (SKILL / "reviewer.md").read_text()
    for ph in ("{AREA}", "{AREA_NAME}", "{REVIEW}", "{REPO}"):
        assert ph in reviewer and ph in text
    # every command the skill tells Claude to run exists
    cmds = set(re.findall(r"(?:^|`|&& )pr-review ([a-z][a-z-]*)", text + reviewer, re.M))
    assert {"analyze", "status", "pack", "findings", "fetch"} <= cmds
    known = set(main.commands)
    assert cmds <= known | {"app"}, cmds - known
    assert "—" not in text + reviewer
