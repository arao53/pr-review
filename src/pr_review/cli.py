"""Command line entry point: `pr-review analyze | report | summary | schema`."""

from __future__ import annotations

import json
import re
import sys
import webbrowser
from pathlib import Path

import click

from pr_review import __version__, derive, gitutil, report
from pr_review.analyze import analyze as run_analysis

STATE_DIR = ".pr-review"


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "head"


def _default_out(root: Path, review: dict) -> Path:
    head = review["meta"]["head"]
    name = head if head != "HEAD" else review["meta"]["head_sha"][:12]
    return root / STATE_DIR / _slug(name)


def repo_config(root: Path) -> dict:
    """[tool.pr-review] from the reviewed repo's pyproject.toml, if any."""
    py = root / "pyproject.toml"
    if not py.exists():
        return {}
    try:
        import tomllib
    except ImportError:  # Python 3.10
        try:
            import tomli as tomllib
        except ImportError:
            return {}
    try:
        data = tomllib.loads(py.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {k.replace("-", "_"): v for k, v in data.get("tool", {}).get("pr-review", {}).items()}


def rigor_options(f):
    """Shared knobs for analyze and retier."""
    opts = [
        click.option("--rigor", type=click.Choice(list(derive.PRESETS)),
                     help="Preset: quick, standard (default) or thorough."),
        click.option("--deep-score", type=click.IntRange(0, 100), help=derive.KNOBS["deep_score"]),
        click.option("--skip-score", type=click.IntRange(0, 100), help=derive.KNOBS["skip_score"]),
        click.option("--token-budget", type=click.IntRange(0), help=derive.KNOBS["token_budget"] + " 0 = no limit."),
        click.option("--context-callers", type=click.IntRange(0, 50), help=derive.KNOBS["context_callers"]),
    ]
    for o in reversed(opts):
        f = o(f)
    return f


def _find_review(path: str | None) -> Path:
    if path:
        p = Path(path)
        return p / "review.json" if p.is_dir() else p
    root = gitutil.repo_root(Path.cwd())
    latest = root / STATE_DIR / "latest"
    if latest.exists():
        return root / STATE_DIR / latest.read_text().strip() / "review.json"
    raise click.ClickException("no review found; run `pr-review analyze` first or pass --review")


@click.group()
@click.version_option(__version__, prog_name="pr-review")
def main() -> None:
    """Triage, cluster and map a large Python pull request before reading it."""


@main.command()
@click.option("--base", help="Branch or commit the PR targets. Default: origin/HEAD, then main or master.")
@click.option("--head", default="HEAD", show_default=True, help="Branch or commit with the changes.")
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False), help="Path inside the git repository.")
@click.option("--out", type=click.Path(file_okay=False), help=f"Output directory. Default: {STATE_DIR}/<branch>/")
@click.option("--coverage", "coverage_xml", type=click.Path(exists=True, dir_okay=False),
              help="coverage.py XML report (`coverage xml`) to find untested changed lines.")
@click.option("--no-diffs", is_flag=True, help="Leave per-file diffs out of the JSON and HTML.")
@click.option("--max-clusters", type=click.IntRange(1, 50), help=derive.KNOBS["max_clusters"])
@click.option("--line-budget", type=click.IntRange(200), help=derive.KNOBS["line_budget"])
@click.option("--refine/--no-refine", default=None, help=derive.KNOBS["refine"])
@rigor_options
@click.option("--open", "open_browser", is_flag=True, help="Open report.html when done.")
@click.option("--quiet", is_flag=True, help="Only print the output paths.")
def analyze(base, head, repo, out, coverage_xml, no_diffs, max_clusters, line_budget, refine,
            rigor, deep_score, skip_score, token_budget, context_callers, open_browser, quiet):
    """Analyze the diff between BASE and HEAD and write review.json + report.html."""
    try:
        root = gitutil.repo_root(repo)
        opts = derive.resolve_options(rigor, repo_config(root), {
            "deep_score": deep_score, "skip_score": skip_score, "token_budget": token_budget,
            "context_callers": context_callers, "max_clusters": max_clusters, "line_budget": line_budget,
            "refine": refine})
        review = run_analysis(root, base=base, head=head, coverage_xml=coverage_xml,
                              include_diffs=not no_diffs, options=opts)
    except (gitutil.GitError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    out_dir = Path(out) if out else _default_out(root, review)
    json_path, html_path = report.write(review, out_dir)
    state = root / STATE_DIR
    if out_dir.resolve().parent == state.resolve():
        (state / ".gitignore").write_text("*\n")
        (state / "latest").write_text(out_dir.name + "\n")

    if not quiet:
        click.echo(_summary_text(review, top=8))
        click.echo("")
    click.echo(f"review: {json_path}")
    click.echo(f"report: {html_path}")
    if open_browser:
        webbrowser.open(html_path.resolve().as_uri())


@main.command("report")
@click.option("--review", "review_path", type=click.Path(exists=True), help="review.json or its directory. Default: latest.")
@click.option("--open", "open_browser", is_flag=True)
def report_cmd(review_path, open_browser):
    """Re-render report.html from review.json (after findings were added)."""
    path = _find_review(review_path)
    review = report.load(path)
    html_path = path.with_name("report.html")
    html_path.write_text(report.render_html(review), encoding="utf-8")
    click.echo(f"report: {html_path}")
    if open_browser:
        webbrowser.open(html_path.resolve().as_uri())


@main.command()
@click.option("--review", "review_path", type=click.Path(exists=True), help="review.json or its directory. Default: latest.")
@click.option("--cluster", help="Show one cluster in detail, e.g. c3.")
@click.option("--top", default=10, show_default=True, help="Files to list.")
@click.option("--json", "as_json", is_flag=True, help="Print the selected part as JSON without diffs.")
def summary(review_path, cluster, top, as_json):
    """Print a compact text summary. Built for terminals and for Claude to read."""
    review = report.load(_find_review(review_path))
    if cluster:
        c = next((c for c in review["clusters"] if c["id"] == cluster), None)
        if c is None:
            raise click.ClickException(f"no cluster {cluster}; have {', '.join(x['id'] for x in review['clusters'])}")
        files = [dict(f, diff=None) for f in review["files"] if f["cluster"] == cluster]
        if as_json:
            gaps = [g for g in review["gaps"] if g.get("cluster") == cluster]
            click.echo(json.dumps({"cluster": c, "files": files, "gaps": gaps}, indent=2))
        else:
            click.echo(_cluster_text(review, c, files))
        return
    if as_json:
        slim = dict(review, files=[dict(f, diff=None) for f in review["files"]])
        click.echo(json.dumps(slim, indent=2))
    else:
        click.echo(_summary_text(review, top=top))


@main.command()
@click.option("--review", "review_path", type=click.Path(exists=True), help="review.json or its directory. Default: latest.")
@click.option("--max-symbols", type=click.IntRange(1), help=derive.KNOBS["refine_max_symbols"])
@click.option("--timeout", default=300, show_default=True, help="Stop after this many seconds and keep what was found.")
def refine(review_path, max_symbols, timeout):
    """Re-check who uses each changed function with jedi (slower, more accurate)."""
    from pr_review import refine as refine_mod
    path = _find_review(review_path)
    review = report.load(path)
    n = max_symbols or (review.get("rigor") or {}).get("refine_max_symbols")

    def progress(i, total, name):
        click.echo(f"\r  {i + 1}/{total} {name[:60]:<60}", nl=False, err=True)

    try:
        stats = refine_mod.refine(review, max_symbols=n, timeout=timeout, progress=progress)
    except refine_mod.RefineUnavailable as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo("", err=True)
    report.write(review, path.parent)
    click.echo(f"Checked {stats['checked']} functions in {stats['seconds']}s: {stats['confirmed']} uses confirmed, "
               f"{stats['added']} found that the quick pass missed, {stats['dropped']} wrong guesses dropped.")
    if stats["remaining"]:
        click.echo(f"{stats['remaining']} lower-priority functions still use the quick pass. Raise --max-symbols to check them.")


@main.command()
@click.option("--review", "review_path", type=click.Path(exists=True), help="review.json or its directory. Default: latest.")
@rigor_options
def retier(review_path, rigor, deep_score, skip_score, token_budget, context_callers):
    """Change how strict the review is without re-running the analysis."""
    path = _find_review(review_path)
    review = report.load(path)
    current = dict(review.get("rigor") or {})
    base = derive.resolve_options(rigor) if rigor else current
    base.update({k: v for k, v in {"deep_score": deep_score, "skip_score": skip_score, "token_budget": token_budget,
                                   "context_callers": context_callers}.items() if v is not None})
    if base.get("token_budget") == 0:
        base["token_budget"] = None
    derive.apply_tiers(review, base)
    report.write(review, path.parent)
    click.echo(_budget_text(review))


@main.command()
@click.option("--repo", default=".", type=click.Path(exists=True, file_okay=False), help="Folder of the project to review.")
@click.option("--port", default=2718, show_default=True)
@click.option("--edit", is_flag=True, help="Open the notebook in edit mode instead of as an app.")
def app(repo, port, edit):
    """Open the point-and-click review app in your browser."""
    import os
    import subprocess
    try:
        import marimo  # noqa: F401
    except ImportError as exc:
        raise click.ClickException("The app needs marimo. Install it with: pip install 'pr-review[app]'") from exc
    notebook = Path(__file__).with_name("app.py")
    env = dict(os.environ, PR_REVIEW_REPO=str(Path(repo).resolve()))
    cmd = [sys.executable, "-m", "marimo", "edit" if edit else "run", str(notebook), "--port", str(port)]
    sys.exit(subprocess.call(cmd, env=env))


@main.command("schema")
def schema_cmd():
    """Print the JSON schema for review.json."""
    click.echo(json.dumps(report.schema(), indent=2))


# ---------------------------------------------------------------- text output

def _budget_text(review: dict) -> str:
    b, r = review.get("budget"), review.get("rigor") or {}
    if not b:
        return ""
    lines = [f"Rigor: {r.get('rigor', 'custom')} (close read at score {r.get('deep_score')}, skip below {r.get('skip_score')})",
             f"Read closely: {b['files']['deep']} files, {b['lines_deep']} lines. Skim: {b['files']['skim']} files, "
             f"{b['lines_skim']} lines. Skip: {b['files']['skip']} files.",
             f"About {b['est_minutes']} min of human reading. AI review of the close-read areas: about "
             f"{b['est_tokens']:,} tokens (reading everything would be about {b['baseline_tokens']:,})."]
    if b["demoted"]:
        lines.append(f"Moved to skim to fit the {b['token_budget']:,}-token budget: {', '.join(b['demoted'])}")
    return "\n".join(lines)


def _summary_text(review: dict, top: int = 10) -> str:
    m, s = review["meta"], review["stats"]
    lines = [
        f"{m['repo']}: {m['head']} -> {m['base']}  ({m['merge_base_sha'][:10]}..{m['head_sha'][:10]})",
        f"{s['files_changed']} files changed (+{s['lines_added']}/-{s['lines_removed']}). "
        f"{s['reviewable_files']} to review ({s['reviewable_lines']} lines), {s['collapsed_files']} collapsed.",
    ]
    kinds: dict[str, int] = {}
    for c in review["collapsed"]:
        kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
    if kinds:
        lines.append("Collapsed: " + ", ".join(f"{n} {k}" for k, n in sorted(kinds.items())))
    if review.get("budget"):
        lines += ["", _budget_text(review)]
    lines += ["", f"Clusters ({len(review['clusters'])}), highest priority first:"]
    for c in review["clusters"]:
        st = c["stats"]
        lines.append(f"  {c['id']:>4}  {c.get('tier', ''):<4}  risk {st['max_score']:>3}  {st['files']:>3} files  "
                     f"+{st['added']}/-{st['removed']}  {c['name']}")
    sem = [f for f in review["files"] if f["kind"] == "semantic"][:top]
    if sem:
        lines += ["", f"Riskiest files (top {len(sem)}):"]
        for f in sem:
            lines.append(f"  {f['score']:>3}  {f['cluster'] or '-':>4}  {f['path']}")
    gaps = [g for g in review["gaps"] if g["severity"] != "info"]
    if gaps:
        lines += ["", f"Gaps ({len(gaps)}, info items hidden):"]
        for g in gaps[:15]:
            where = f" [{g.get('file') or g.get('cluster') or 'PR'}]"
            lines.append(f"  {g['severity']:<6} {g['message']}{where}")
        if len(gaps) > 15:
            lines.append(f"  ... {len(gaps) - 15} more in review.json")
    return "\n".join(lines)


def _cluster_text(review: dict, c: dict, files: list[dict]) -> str:
    st = c["stats"]
    by = {f["path"]: f for f in files}
    lines = [f"{c['id']} {c['name']}: {st['files']} files, +{st['added']}/-{st['removed']}, "
             f"max risk {st['max_score']}, {c['dependents_outside_pr']} importers outside the PR", "",
             "Reading order:"]
    for p in c["files"]:
        f = by[p]
        lines.append(f"  {f['score']:>3}  {f['category']:<10} +{f['added']}/-{f['removed']}  {p}")
        for r in f["score_reasons"]:
            lines.append(f"         {r}")
        for sgl in f["signals"][:6]:
            lines.append(f"         line {sgl['line']}: {sgl['detail']}")
        for a in f["api_changes"]:
            if a["change"] != "added":
                lines.append(f"         API {a['change']}: {a['name']}")
    gaps = [g for g in review["gaps"] if g.get("cluster") == c["id"]]
    if gaps:
        lines += ["", "Gaps:"]
        lines += [f"  {g['severity']:<6} {g['message']} [{g.get('file') or ', '.join(g.get('files', [])[:5])}]" for g in gaps]
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
