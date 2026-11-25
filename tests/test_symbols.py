"""Symbol index, structure delta, hunks, tiers and the jedi refine."""

import json

import pytest

from pr_review import analyze, derive, gitutil, refine
from pr_review.graph import build_graph
from pr_review.symbols import Resolver, define, scan_refs

from conftest import Repo


def sym(review, sid):
    return next(s for s in review["symbols"] if s["id"] == sid)


@pytest.fixture
def review(shop_repo):
    return analyze(shop_repo.root, base="main")


# ---------------------------------------------------------------- symbols

def test_changed_symbols_and_breaking(review):
    s = sym(review, "src/shop/pricing.py::apply_discount")
    assert s["change"] == "signature" and s["breaking"]
    assert sym(review, "src/shop/pricing.py::legacy_total")["change"] == "removed"
    ok = sym(review, "src/shop/auth/session.py::make_session")
    assert ok["change"] == "signature" and not ok["breaking"]  # new optional argument
    assert sym(review, "src/shop/new_feature.py::ship")["change"] == "added"


def test_callers_found_through_imports(review):
    s = sym(review, "src/shop/pricing.py::apply_discount")
    where = {(c["path"], c["caller"]) for c in s["callers"]}
    assert ("src/shop/report.py", "build") in where
    assert ("tests/test_pricing.py", "test_discount") in where
    report_call = next(c for c in s["callers"] if c["path"] == "src/shop/report.py")
    assert report_call["confidence"] == "high" and not report_call["in_pr"] and not report_call["touched"]


def test_resolver_handles_reexports_modules_and_self():
    srcs = {
        "pkg/__init__.py": "from .core import Engine as Motor\n",
        "pkg/core.py": "class Engine:\n    def start(self):\n        return self.spin()\n    def spin(self):\n        return 1\n",
        "app/run.py": "import pkg\nfrom pkg import Motor\nimport pkg.core as c\n\n"
                      "def go(x):\n    Motor().start()\n    pkg.Motor()\n    c.Engine.spin(None)\n    x.start()\n",
    }
    g = build_graph(srcs)
    targets = {"pkg/core.py::Engine", "pkg/core.py::Engine.start", "pkg/core.py::Engine.spin"}
    methods = {"start": {"pkg/core.py::Engine.start"}, "spin": {"pkg/core.py::Engine.spin"}}
    refs = scan_refs("app/run.py", g.trees["app/run.py"], Resolver("app/run.py", g.trees["app/run.py"], g), targets, methods)
    got = {(r.target, r.line, r.confidence) for r in refs}
    assert ("pkg/core.py::Engine", 6, "high") in got   # from pkg import Motor (re-export with alias)
    assert ("pkg/core.py::Engine", 7, "high") in got   # pkg.Motor through the package
    assert ("pkg/core.py::Engine.spin", 8, "high") in got  # module alias then class attribute
    assert ("pkg/core.py::Engine.start", 9, "low") in got  # x.start(): type unknown
    own = scan_refs("pkg/core.py", g.trees["pkg/core.py"], Resolver("pkg/core.py", g.trees["pkg/core.py"], g),
                    targets, methods)
    assert ("pkg/core.py::Engine.spin", 3, "high") in {(r.target, r.line, r.confidence) for r in own}  # self.spin()


def test_define_tracks_methods_and_module_code():
    src = "X = 1\n\nclass A(B):\n    def f(self, a):\n        pass\n\ndef _g():\n    pass\n"
    import ast
    d = define(ast.parse(src), src)
    assert set(d) == {"A", "A.f", "_g", "<module>"}
    assert d["A.f"].kind == "method" and not d["_g"].public
    assert d["A.f"].col == src.splitlines()[3].index("f(")


def test_removed_override_is_not_high(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.commit({"lib/__init__.py": "", "lib/w.py": "import textwrap\n\nclass W(textwrap.TextWrapper):\n"
                 "    def _split(self, text):\n        return []\n\n    def go(self):\n        return self._split('x')\n"})
    repo.branch("f")
    repo.commit({"lib/w.py": "import textwrap\n\nclass W(textwrap.TextWrapper):\n"
                 "    def go(self):\n        return self._split('x')\n"})
    r = analyze(repo.root, base="main")
    kinds = {(g["kind"], g["severity"]) for g in r["gaps"]}
    assert ("removed_override", "low") in kinds
    assert not any(k == "removed_symbol_still_used" for k, _ in kinds)


# ---------------------------------------------------------------- delta

def test_delta_finds_new_cycle_and_package_link(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.commit({"a/__init__.py": "", "a/x.py": "from b import y\n", "b/__init__.py": "", "b/y.py": "Z = 1\n"})
    repo.branch("f")
    repo.commit({"b/y.py": "from a import x\nZ = 1\n"})
    r = analyze(repo.root, base="main")
    d = r["delta"]
    assert {"source": "b/y.py", "target": "a/x.py"} in d["module_edges"]["added"]
    assert d["cycles"] and set(d["cycles"][0]["files"]) == {"a/x.py", "b/y.py"}
    assert ("b", "a") in {(p["source"], p["target"]) for p in d["package_edges"]["added"]}
    assert "new_import_cycle" in {g["kind"] for g in r["gaps"]}


def test_existing_cycle_is_not_new(tmp_path):
    repo = Repo(tmp_path / "r")
    repo.commit({"a.py": "import b\n", "b.py": "import a\n", "c.py": "X = 1\n"})
    repo.branch("f")
    repo.commit({"b.py": "import a\nimport c\n", "c.py": "import a\n"})
    d = analyze(repo.root, base="main")["delta"]
    assert all(set(c["files"]) != {"a.py", "b.py"} for c in d["cycles"])


def test_call_edges(review):
    added = {(e["source"], e["target"]) for e in review["delta"]["call_edges"]["added"]}
    assert all("::" in a and "::" in b for a, b in added)


# ---------------------------------------------------------------- hunks

def test_formatting_only_hunks():
    diff = ("@@ -1,1 +1,4 @@\n-x = f(a, b)\n+x = f(\n+    a,\n+    b,\n+)\n"
            "@@ -9,1 +12,1 @@\n-y = 1  # old\n+y = 1  # new\n"
            "@@ -20,1 +23,1 @@\n-z = 1\n+z = 2\n")
    hs = gitutil.hunks(diff)
    assert [h["trivial"] for h in hs] == [True, True, False]


# ---------------------------------------------------------------- tiers

def test_presets_and_overrides():
    q = derive.resolve_options("quick")
    assert q["deep_score"] == 70 and q["token_budget"] == 60_000
    cfg = {"rigor": "thorough", "deep_score": 50}
    assert derive.resolve_options(None, cfg)["deep_score"] == 50
    assert derive.resolve_options("quick", cfg)["deep_score"] == 70  # explicit preset wins over config
    assert derive.resolve_options(None, cfg, {"deep_score": 42})["deep_score"] == 42
    with pytest.raises(ValueError):
        derive.resolve_options("reckless")


def test_tiers_follow_thresholds(review):
    loose = derive.preview(review, {"deep_score": 100, "skip_score": 100, "escalate_structural": False})
    tight = derive.preview(review, {"deep_score": 0, "skip_score": 0})
    assert loose["budget"]["files"]["deep"] < tight["budget"]["files"]["deep"]
    assert tight["budget"]["est_tokens"] > loose["budget"]["est_tokens"]
    # files with a high-severity gap are always read closely
    assert next(f for f in loose["files"] if f["path"] == "src/shop/pricing.py")["tier"] == "deep"


def test_token_budget_demotes_lowest_priority(shop_repo):
    r = analyze(shop_repo.root, base="main", options=derive.resolve_options(None, None, {"max_clusters": 1}))
    before = r["budget"]["est_tokens"]
    derive.apply_tiers(r, {"token_budget": 1})
    assert r["budget"]["demoted"] and r["budget"]["est_tokens"] < before
    assert all(c["tier"] != "deep" for c in r["clusters"])


def test_review_json_alone_is_enough(review, tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(review))
    again = json.loads(p.read_text())
    derive.refresh(again, {"deep_score": 10})
    assert again["budget"]["files"]["deep"] >= review["budget"]["files"]["deep"]


# ---------------------------------------------------------------- refine

@pytest.mark.skipif(not refine.available(), reason="jedi not installed")
def test_refine_with_jedi(review):
    stats = refine.refine(review)
    assert stats["status"] == "done" and stats["checked"] >= 3
    s = sym(review, "src/shop/pricing.py::apply_discount")
    assert s["resolution"] == "jedi"
    assert {"src/shop/report.py", "tests/test_pricing.py"} <= {c["path"] for c in s["callers"]}
    assert review["meta"]["refine"]["engine"] == "jedi"
    assert "breaking_change_untouched_callers" in {g["kind"] for g in review["gaps"]}


def test_refine_reads_the_commit_not_the_working_tree(shop_repo):
    if not refine.available():
        pytest.skip("jedi not installed")
    r = analyze(shop_repo.root, base="main")
    (shop_repo.root / "src/shop/report.py").write_text("# local edit, not committed\n")
    refine.refine(r)
    assert "src/shop/report.py" in {c["path"] for c in sym(r, "src/shop/pricing.py::apply_discount")["callers"]}
