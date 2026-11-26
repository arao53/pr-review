"""Picking the two versions to compare, including a fork where the PR lives upstream."""

import subprocess
from pathlib import Path

import pytest

from pr_review import analyze, appkit, gitutil

from conftest import git


def run(cwd, *args):
    return git(Path(cwd), *args)


@pytest.fixture
def fork(tmp_path):
    """upstream (bare, has refs/pull/6/head) <- fork (bare) <- local clone of the fork."""
    seed = tmp_path / "seed"
    seed.mkdir()
    run(seed, "init", "-q", "-b", "main")
    (seed / "app.py").write_text("def f():\n    return 1\n")
    run(seed, "add", "-A")
    run(seed, "commit", "-q", "-m", "base")
    run(seed, "checkout", "-q", "-b", "feature")
    (seed / "app.py").write_text("def f():\n    return 2\n")
    run(seed, "commit", "-qam", "change")
    pr_sha = run(seed, "rev-parse", "HEAD").strip()
    run(seed, "checkout", "-q", "main")
    upstream = tmp_path / "upstream.git"
    run(tmp_path, "clone", "-q", "--bare", str(seed), str(upstream))
    run(upstream, "update-ref", "refs/pull/6/head", pr_sha)
    run(upstream, "branch", "-D", "feature")  # like GitHub: the branch lives on the fork only
    forkrepo = tmp_path / "fork.git"
    run(tmp_path, "clone", "-q", "--bare", str(upstream), str(forkrepo))
    local = tmp_path / "local"
    run(tmp_path, "clone", "-q", str(forkrepo), str(local))
    run(local, "remote", "add", "upstream", str(upstream))
    run(local, "fetch", "-q", "upstream")
    return local, pr_sha


def test_suggested_head_never_equals_base_when_on_main(fork):
    local, _ = fork
    v = appkit.list_versions(local)
    assert v["current"] == "main"
    assert v["default_base"].startswith("upstream/")
    # nothing differs yet, so the suggestion falls back to something sensible but explain_same says why
    assert appkit.explain_same(local, v["default_base"], "main").startswith("**main** and")


def test_download_pr_from_upstream_and_preselect(fork):
    local, pr_sha = fork
    ref = appkit.fetch_pull_request(local, 6)
    assert ref == "pr-review/pr-6"
    assert gitutil.rev_parse(local, ref) == pr_sha
    v = appkit.list_versions(local)
    assert v["suggested_head"] == "pr-review/pr-6"
    assert ref in [n for n, _ in v["refs"]]  # the menu holds the same name the download returns
    assert appkit.explain_same(local, v["default_base"], ref) is None
    r = analyze(local, base=v["default_base"], head=ref)
    assert r["stats"]["files_changed"] == 1


def test_download_reports_every_remote_on_failure(fork):
    local, _ = fork
    with pytest.raises(gitutil.GitError, match="upstream:.*origin:"):
        appkit.fetch_pull_request(local, 999)


def test_explain_same_for_merged_or_stale_branch(fork):
    local, _ = fork
    run(local, "branch", "old-copy", "HEAD")
    run(local, "commit", "-q", "--allow-empty", "-m", "later on main")
    msg = appkit.explain_same(local, "main", "old-copy")
    assert "already in **main**" in msg


def test_ref_label():
    assert appkit.ref_label("refs/pr-review/pr-6") == "Pull request #6"
    assert appkit.ref_label("pr-review/pr-6") == "Pull request #6"
    assert appkit.ref_label("main") == "main"
