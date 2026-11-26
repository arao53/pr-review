# pr-review

`pr-review` helps you review a large Python pull request. It works out which files need a close read, which you can skim and which you can skip. It groups the changes into areas and lists the changed functions along with the code that calls them. It also flags anything likely to break.

You can use it two ways: a point-and-click app in your browser, or a command-line tool that writes a JSON file for scripts, CI or Claude.

On a 120-commit range of [pallets/click](https://github.com/pallets/click) (131 files, +14,906/−4,827), analysis takes about 3 seconds. It sets aside 78 files as formatting, renames, generated code or low risk, and marks 6 for a close read. An AI review of just those close-read areas is estimated at 72k tokens, against 165k for reading everything.

Nothing is imported or executed. Everything is read from git and parsed with Python's `ast` module, so it works on any commit without setting up the project.

## Install

```bash
pipx install "pr-review[app] @ git+https://github.com/arao53/pr-review"
```

That installs the app (marimo, altair and pandas) and the accurate usage search (jedi). If you only want the command line, leave out `[app]`. Requires Python 3.10+ and git.

## The app

From inside your project folder:

```bash
pr-review app
```

It serves on port 2718, or the next free port if that one is taken (for example by another app window still running), and prints the address. Your browser opens on a page with two steps:

1. **What do you want to review?** Pick the version with the changes and the version it will be merged into, from lists of your project's branches. For a GitHub pull request that isn't on your computer yet, type its number and press **Download it**. You don't need any git commands.
2. **How careful should the review be?** Pick *Quick*, *Standard* or *Thorough*, then press **Analyze this change**.

The result opens on a summary in plain words, such as "This change touches 131 files. 78 of them don't need reading." Below it are counts of files to read closely, skim and skip, a rough reading time, and the AI review size. Then come five tabs:

- **Map** is where you do the review, one level at a time:
  - *All areas*: a bubble chart of the areas, sized by changed lines and colored by advice, with a list in reading order. Click a bubble or press **Open**.
  - *One area*: how its files use each other, anything flagged in it, its files from most to least important, and a prompt you can copy into Claude.
  - *One file*: why it was flagged, in plain words with the points behind its score, and its changed functions with the risky ones first. **Where it's used** lists every place that calls a function, whether that place is part of this change, and whether its line was edited. Below that is the diff, with formatting-only hunks folded away. Flip **I've reviewed this file** as you go. The mark stays until the file changes again.
- **Things to check** lists every flagged item, grouped into *Must check*, *Should check*, *Worth a look* and *For your information*. Each one says what it means and what to do, and has an **Open file** button.
- **What changed in the structure** covers how the code fits together, not the lines. It counts new, removed and changed functions, lists the changes most likely to break other code, and shows new import loops and folders that now depend on each other.
- **Cost & settings** has sliders that move files between *read closely*, *skim* and *skip*. The counts, reading time and AI review size update as you drag. It also holds the **Improve accuracy** button.
- **Help** explains the terms used on the page.

Reviews are saved in `.pr-review/` inside your project, and the app reopens the last one automatically. That folder writes its own `.gitignore`, so it is never committed.

## AI review with Claude Code

Claude Code's built-in `/code-review` finds bugs in a diff. On a small change it's all you need. On a large one, `pr-review` decides where it should look. Install the skill that connects the two:

```bash
pr-review install-skill            # for you, in ~/.claude/skills
pr-review install-skill --project  # or for everyone working in this repository
```

Then ask Claude Code something like "review this PR with pr-review", "review PR 1234", or "quick review of my branch against develop". The skill:

1. Runs `pr-review analyze` at the rigor you asked for and tells you how big the job is.
2. Runs `pr-review plan`, which picks the close-read areas that haven't been reviewed at their current code, and gives the exact `/code-review` command to run:
   - the commit range;
   - an effort level that follows the rigor: quick is `low`, standard `high`, thorough `xhigh`;
   - `--max-findings all`, so a big PR isn't cut off at 32 findings;
   - the files to start with.
   If there's more close-read code than one pass handles well (about 4,000 lines or six areas), it plans one pass per area instead.
3. Runs `/code-review` and saves what it finds with `pr-review findings import`. The import places each finding in the area its file belongs to. It accepts a JSON list, the output of `claude -p "/code-review ..." --output-format json`, or plain text that cites `path:line`.
4. Looks across areas with `pr-review pack --cross`, for a change in one area that breaks another and for repeated or contradictory findings.
5. Gives you a short summary: what to fix first, each item with `file:line`, and what wasn't reviewed.

If `/code-review` isn't available (Claude Code before v2.1.147), or you ask for one reviewer per area, the skill falls back to its own reviewer subagents. Each one pulls a trimmed context pack with `pr-review pack <area>` and saves findings with `pr-review findings add`.

Findings are stored in `review.json`, so they show up in the app, on the overview, in *Things to check* and on each file's page. When the author pushes new commits, re-run the review. Areas whose code didn't change keep their findings and aren't reviewed again. Areas whose code did change are reviewed again, and their earlier findings are shown to the reviewer to re-check.

The skill is read-only. It doesn't pass `--fix` or `--comment` to `/code-review`, comment on GitHub, approve, push or commit unless you ask.

You can use the same import without the skill, for example in CI:

```bash
pr-review analyze --base origin/main --quiet
claude -p "$(pr-review plan --json | jq -r .single_command)" --output-format json > review.out
pr-review findings import --areas "$(pr-review plan --json | jq -r '[.areas[].id] | join(",")')" review.out
```

## The command line

```bash
pr-review analyze                       # base: upstream's default branch in a fork, else origin's, then main or master
pr-review analyze --base develop --rigor thorough
pr-review analyze --coverage coverage.xml   # also flag changed lines that no test runs
pr-review summary                       # text summary of the latest review
pr-review summary --cluster c3 --json   # one area as JSON, without diffs (handy for Claude)
pr-review refine                        # the accurate usage search, see below
pr-review retier --deep-score 40        # change the advice without re-running the analysis
pr-review report                        # rebuild the shareable report.html
pr-review fetch 1234                    # download GitHub PR #1234 (from upstream first, in a fork); prints a ref for --head
pr-review status                        # which areas still need an agent review
pr-review plan                          # the /code-review command(s) to run next
pr-review findings import out.json      # save /code-review output, routed to areas
pr-review pack c3                       # the context a reviewer needs for area c3
pr-review findings add --area c3 f.json # save findings in pr-review's own format
```

`analyze` writes `review.json` (schema in `src/pr_review/schema/review.schema.json`) and `report.html`, a single offline page you can attach to a ticket. The app is the better place to review. The HTML file is for sharing a snapshot with someone who doesn't have the tool.

## How careful: rigor and cost

Every file gets one of three pieces of advice. The thresholds below set where the lines fall, and the presets are starting points:

| setting | what it does | quick | standard | thorough |
|---|---|---|---|---|
| `deep_score` | files at or above this risk score are *read closely* | 70 | 55 | 35 |
| `skip_score` | files below this, with nothing flagged, are *safe to skip* | 35 | 20 | 5 |
| `token_budget` | cap on AI review size; the lowest-priority areas drop to *skim* to fit | 60k | none | none |
| `context_callers` | how many call sites per changed function the AI is shown | 2 | 3 | 6 |
| `line_budget` | rough changed lines per area; smaller means more, tighter areas | 4000 | 2500 | 1500 |
| `refine` | run the accurate usage search during analysis | off | off | on |

Some files are always *read closely*, whatever the thresholds: files with a high-severity problem, and files that change a function in a way that breaks code which wasn't updated. Tests sitting next to code that needs a close read are never skipped, because they show what that code is supposed to do.

The reading time assumes about 300 lines an hour for a close read and 1,500 for a skim, which matches common code-review guidance. The AI size estimate counts about 10 tokens per changed line, plus the call sites and a fixed overhead per area.

To set team defaults, add them to the reviewed project's `pyproject.toml`:

```toml
[tool.pr-review]
rigor = "standard"
deep_score = 50
token_budget = 120000
```

Choosing `--rigor` on the command line, or a preset in the app, overrides these values.

## Improve accuracy

The first pass finds callers by name. It follows each file's imports, `module.attr` chains, package re-exports and `self.method` inside a class. That is fast and usually right. It can't tell the type of a variable, though, so a call like `obj.close()` is only a guess, and those guesses are marked *possible* rather than *certain*.

**Improve accuracy** (or `pr-review refine`) re-checks callers with [jedi](https://github.com/davidhalter/jedi), which follows types the way an editor's "find references" does. It reads a clean export of the commit, so uncommitted local edits don't change the result. Breaking changes are checked first. On click, it checked 150 functions in 22 seconds. It confirmed 1,124 uses, found 165 that the name search missed and dropped 619 wrong guesses.

## What the analysis does

1. **Triage.** Generated files, lockfiles, vendored code, pure renames, and files whose syntax tree didn't change (black, ruff format, comments) are set aside, with the reason shown. Inside the remaining files, hunks that only change whitespace, line breaks, comments or trailing commas are marked as formatting and don't count toward the size of the change.
2. **Import graph** for the whole tree at the head commit, including relative imports, `src/` layouts and re-exports.
3. **Changed functions.** Only the changed files are parsed at both ends. Each function, method and class is marked *new*, *removed*, *inputs changed* (breaking or not) or *changed inside*. Breaking means existing calls can fail: a required parameter was added, a parameter was removed or reordered, or a keyword argument was dropped. Callers are searched for only in files that import a changed file.
4. **Structure delta.** Only changed files can gain or lose imports, so the old graph is rebuilt exactly as the head graph minus added edges plus removed ones. That gives import edges added and removed, function calls added and removed, new import loops (not ones that already existed), and folders that now depend on each other for the first time.
5. **Risk score (0 to 100)** per file, with a written reason for every point. It counts size, how many files depend on it, sensitive paths (auth, payments, sessions, settings, migrations and similar), breaking API changes, risky patterns on added lines (bare `except`, `eval`, `shell=True`, `pickle.loads`, hard-coded secrets and more), and missing tests.
6. **Areas.** Louvain community detection over imports and folders, with areas split when they get too big and merged when they get too small. The same commits always give the same areas.
7. **Gaps and advice**, as described above.

Steps 5 to 7 can be recomputed from `review.json` alone (`pr_review.derive`). That's why the sliders and **Improve accuracy** don't need to re-run the analysis.

## What it can't see

The tool reads code without running it. Connections made at run time are invisible: pytest fixtures (a changed `conftest.py` is handled separately), `importlib` and other imports built from strings, Django settings and URL wiring, plugin entry points, and decorator registries. Use the advice to decide where to look first, not as proof that code is correct.

## Development

Tests run on GitHub Actions for Python 3.10 to 3.13, with and without the app's dependencies.

```bash
pip install -e ".[dev,app]"
pytest            # 65 tests, including a 134-file synthetic PR and a headless run of the app
```

```
src/pr_review/
  gitutil.py   git plumbing: diffs, hunks, batched file reads
  triage.py    path rules: categories, generated files, sensitive paths
  pyast.py     AST work: formatting detection, API compatibility, risk patterns, imports
  graph.py     import graph and re-exports
  symbols.py   changed functions and their callers (quick search)
  refine.py    accurate search with jedi
  analyze.py   the pipeline, structure delta and gaps
  derive.py    presets, advice tiers and cost, recomputed from review.json
  score.py     risk score with reasons
  cluster.py   areas
  app.py       the marimo app
  appkit.py    the app's wording, charts and saved progress
  plan.py      what to point /code-review at
  pack.py      context packs for reviewer agents
  findings.py  storing, importing and carrying over findings
  skill/       the Claude Code skill (SKILL.md, reviewer.md)
  report.py    JSON and shareable HTML output
  cli.py       command line
```

## License

MIT
