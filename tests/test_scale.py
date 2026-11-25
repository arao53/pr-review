"""A synthetic PR at the size this tool is meant for: ~150 changed files across
8 packages, a few thousand changed lines."""

import random
import time

import pytest

from pr_review import analyze

from conftest import Repo

PACKAGES = ["billing", "catalog", "search", "users", "orders", "notify", "reports", "core"]


def module_body(pkg: str, i: int, rng: random.Random, version: int) -> str:
    deps = []
    if pkg != "core":
        deps.append(f"from app.core import m{rng.randrange(6)}")
    if i > 0:
        deps.append(f"from app.{pkg} import m{rng.randrange(i)}")
    funcs = []
    for f in range(8 + version * 4):
        funcs.append(
            f"def f{f}(x, y={version}):\n"
            f"    total = x + y + {f}\n"
            f"    for k in range({f + version}):\n"
            f"        total += k\n"
            f"    return total\n"
        )
    return "\n".join(deps) + "\n\n\n" + "\n\n".join(funcs)


def build(root, rng):
    repo = Repo(root)
    base = {"app/__init__.py": ""}
    for pkg in PACKAGES:
        base[f"app/{pkg}/__init__.py"] = ""
        n = 6 if pkg == "core" else 22
        for i in range(n):
            base[f"app/{pkg}/m{i}.py"] = module_body(pkg, i, rng, 0)
        base[f"tests/{pkg}/test_{pkg}.py"] = f"from app.{pkg} import m0\n\n\ndef test_it():\n    assert m0.f0(1)\n"
    repo.commit(base, "base")
    repo.branch("big-change")
    change = {}
    for pkg in PACKAGES:
        n = 6 if pkg == "core" else 22
        for i in rng.sample(range(n), k=min(n, 18)):
            change[f"app/{pkg}/m{i}.py"] = module_body(pkg, i, rng, 1)
        if pkg in ("billing", "orders"):
            change[f"tests/{pkg}/test_{pkg}.py"] = base[f"tests/{pkg}/test_{pkg}.py"] + "\n\ndef test_more():\n    assert True\n"
    repo.commit(change, "big change")
    return repo


@pytest.mark.slow
def test_large_pr(tmp_path):
    repo = build(tmp_path / "big", random.Random(3))
    t0 = time.time()
    review = analyze(repo.root, base="main")
    elapsed = time.time() - t0

    stats = review["stats"]
    assert stats["files_changed"] >= 130
    assert stats["lines_added"] >= 3000
    assert 3 <= stats["clusters"] <= 12
    sizes = [c["stats"]["files"] for c in review["clusters"]]
    assert min(sizes) >= 2
    assert elapsed < 30, f"analysis took {elapsed:.1f}s"
    # clusters should mostly follow package boundaries
    for c in review["clusters"]:
        pkgs = {p.split("/")[1] for p in c["files"]}
        assert len(pkgs) <= 3, (c["name"], pkgs)
