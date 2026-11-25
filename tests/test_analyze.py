import json
from pathlib import Path

import jsonschema
import pytest

from pr_review import analyze

SCHEMA = json.loads((Path(__file__).parents[1] / "src/pr_review/schema/review.schema.json").read_text())


@pytest.fixture
def review(shop_repo):
    return analyze(shop_repo.root, base="main")


def by_path(review):
    return {f["path"]: f for f in review["files"]}


def gap_kinds(review, path=None):
    return {g["kind"] for g in review["gaps"] if path is None or g["file"] == path}


def test_matches_schema(review):
    jsonschema.validate(review, SCHEMA)


def test_triage_kinds(review):
    f = by_path(review)
    assert f["src/shop/utils.py"]["kind"] == "cosmetic"
    assert f["src/shop/relocated.py"]["kind"] == "rename"
    assert f["src/shop/relocated.py"]["old_path"] == "src/shop/moved.py"
    assert f["generated/api_pb2.py"]["kind"] == "generated"
    assert f["src/shop/old_helpers.py"]["kind"] == "deleted"
    assert f["src/shop/pricing.py"]["kind"] == "semantic"
    collapsed = {c["path"] for c in review["collapsed"]}
    assert {"src/shop/utils.py", "src/shop/relocated.py", "generated/api_pb2.py"} <= collapsed


def test_categories(review):
    f = by_path(review)
    assert f["src/shop/migrations/0002_backfill.py"]["category"] == "migration"
    assert f["pyproject.toml"]["category"] == "dependency"


def test_api_change_detected(review):
    changes = by_path(review)["src/shop/pricing.py"]["api_changes"]
    names = {(c["name"], c["change"]) for c in changes}
    assert ("apply_discount", "changed") in names
    assert ("legacy_total", "removed") in names


def test_signals_on_added_lines(review):
    sigs = {s["kind"] for s in by_path(review)["src/shop/auth/session.py"]["signals"]}
    assert {"hardcoded_secret", "shell", "mutable_default"} <= sigs
    assert "broad_except" in {s["kind"] for s in by_path(review)["src/shop/pricing.py"]["signals"]}


def test_scores_rank_risky_files_first(review):
    f = by_path(review)
    assert f["src/shop/auth/session.py"]["score"] > f["src/shop/new_feature.py"]["score"]
    assert any("sensitive path" in r for r in f["src/shop/auth/session.py"]["score_reasons"])
    assert f["src/shop/utils.py"]["score"] == 0


def test_gaps(review):
    kinds = gap_kinds(review)
    assert "no_tests_changed" in kinds
    assert "removed_symbol_still_used" in gap_kinds(review, "src/shop/pricing.py")
    assert "breaking_change_untouched_callers" in gap_kinds(review, "src/shop/pricing.py")
    assert "stale_module_import" in gap_kinds(review, "src/shop/old_helpers.py")
    assert "irreversible_migration" in gap_kinds(review, "src/shop/migrations/0002_backfill.py")
    assert "dependency_without_lock" in gap_kinds(review, "pyproject.toml")
    assert "untested_new_file" in gap_kinds(review, "src/shop/new_feature.py")
    severities = [g["severity"] for g in review["gaps"]]
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    assert severities == sorted(severities, key=order.get)


def test_dependents_and_tests(review):
    pricing = by_path(review)["src/shop/pricing.py"]
    assert "src/shop/report.py" in pricing["dependents"]["outside_pr"]
    assert "tests/test_pricing.py" in pricing["tests"]["importing"]
    assert pricing["tests"]["changed"] == []


def test_every_reviewable_file_is_in_one_cluster(review):
    semantic = {f["path"] for f in review["files"] if f["kind"] == "semantic"}
    clustered = [p for c in review["clusters"] for p in c["files"]]
    assert sorted(clustered) == sorted(semantic)
    for f in review["files"]:
        assert (f["cluster"] is not None) == (f["kind"] == "semantic")


def test_diffs_optional(shop_repo):
    r = analyze(shop_repo.root, base="main", include_diffs=False)
    assert all(f["diff"] is None for f in r["files"])
    # signals still need the diff internally
    assert by_path(r)["src/shop/auth/session.py"]["signals"]


def test_coverage(shop_repo, tmp_path):
    xml = tmp_path / "coverage.xml"
    xml.write_text("""<?xml version="1.0" ?>
<coverage><packages><package><classes>
<class filename="shop/pricing.py"><lines>
<line number="5" hits="1"/><line number="6" hits="0"/><line number="7" hits="0"/><line number="9" hits="1"/>
</lines></class></classes></package></packages></coverage>""")
    r = analyze(shop_repo.root, base="main", coverage_xml=xml)
    cov = by_path(r)["src/shop/pricing.py"]["coverage"]
    assert cov["measured"] >= 3
    assert 6 in cov["uncovered"] and 7 in cov["uncovered"]
    assert "uncovered_lines" in gap_kinds(r, "src/shop/pricing.py")
