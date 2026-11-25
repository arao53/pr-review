"""The marimo app and its helpers."""

import importlib
import subprocess
import sys

import pytest

from pr_review import analyze, appkit

marimo = pytest.importorskip("marimo")


def test_plain_reasons_cover_every_score_reason(shop_repo):
    r = analyze(shop_repo.root, base="main")
    for f in r["files"]:
        for reason in f["score_reasons"]:
            if reason.startswith("collapsed"):
                continue
            pts, text = appkit.plain_reason(reason)
            assert text != reason, f"no plain wording for: {reason}"


def test_every_gap_kind_has_help(shop_repo):
    r = analyze(shop_repo.root, base="main")
    for g in r["gaps"]:
        title, what, todo = appkit.gap_help(g["kind"])
        assert what and todo, g["kind"]


def test_versions_and_saved_reviews(shop_repo):
    v = appkit.list_versions(shop_repo.root)
    names = [n for n, _ in v["refs"]]
    assert "main" in names and "feature" in names
    assert v["current"] == "feature" and v["default_base"] == "main"
    r = analyze(shop_repo.root, base="main")
    appkit.save(r, shop_repo.root)
    saved = appkit.saved_reviews(shop_repo.root)
    assert saved and saved[0][0].startswith("feature vs main")
    assert appkit.load(saved[0][1])["budget"]


def test_progress_resets_when_a_file_changes_again(shop_repo):
    r = analyze(shop_repo.root, base="main")
    prog = appkit.Progress(shop_repo.root, r)
    prog.set("src/shop/pricing.py", True)
    assert appkit.Progress(shop_repo.root, r).done("src/shop/pricing.py")
    shop_repo.commit({"src/shop/pricing.py": "def apply_discount(order, rate, cap=None):\n    return 0\n"})
    r2 = analyze(shop_repo.root, base="main")
    assert not appkit.Progress(shop_repo.root, r2).done("src/shop/pricing.py")


def test_diff_html_folds_formatting_and_escapes():
    f = {"diff": "@@ -1,1 +1,1 @@\n-x = '<b>'\n+x = \"<b>\"\n@@ -5,1 +5,1 @@\n-a=1\n+a = 1\n",
         "hunks": [{"added": 1, "removed": 1, "trivial": False, "symbols": []},
                   {"added": 1, "removed": 1, "trivial": True, "symbols": []}]}
    out = appkit.diff_html(f, focus=(1, 1))
    assert "&lt;b&gt;" in out and "<b>'" not in out
    assert "formatting only" in out and "<details" in out
    assert "#4c6ef5" in out


def test_charts_build(shop_repo):
    r = analyze(shop_repo.root, base="main")
    assert appkit.areas_chart(r).to_dict()
    assert appkit.files_chart(r, r["clusters"][0]["id"]).to_dict()


def test_notebook_passes_marimo_check():
    path = importlib.import_module("pr_review.app").__file__
    res = subprocess.run([sys.executable, "-m", "marimo", "check", path], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr


def test_notebook_runs_headless_on_a_saved_review(shop_repo, monkeypatch):
    appkit.save(analyze(shop_repo.root, base="main"), shop_repo.root)
    monkeypatch.setenv("PR_REVIEW_REPO", str(shop_repo.root))
    mod = importlib.reload(importlib.import_module("pr_review.app"))
    _, defs = mod.app.run()
    assert defs["view"]["budget"]["files"]["deep"] >= 1
    for name in ("map_view", "checks_view", "structure_view", "settings_view", "help_view", "tabs"):
        assert defs[name] is not None, name
