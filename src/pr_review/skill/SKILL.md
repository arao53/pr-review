---
name: pr-review
description: Review a large Python pull request with the pr-review tool. Use when the user asks to review a big PR, branch or diff, wants to know which parts of a large change need attention, or mentions pr-review. Runs the static triage, points Claude Code's /code-review at the high-risk areas, and saves the findings so the user can browse them in the pr-review app. For a small change, /code-review on its own is enough.
---

# Reviewing a pull request with pr-review

`pr-review` does the static work: it sorts the changed files into *read closely*, *skim* and *skip*, groups them into areas, and lists changed functions with their callers. Claude Code's built-in `/code-review` does the bug-finding, pointed at the close-read areas. Your job is to connect the two: decide what gets reviewed, save what is found, and give the user a short, cited summary.

Keep diffs out of the main conversation. `/code-review` runs in its own subagent, and the fallback reviewers pull their own context with `pr-review pack`. You read plans, status and findings.

## 1. Set up

1. Check the tool is installed: `pr-review --version`. If it isn't, ask the user before installing, then run `pipx install "pr-review[app] @ git+https://github.com/arao53/pr-review"` (or `pip install` into their environment if they prefer).
2. Work out what to compare, in this order:
   - The user named a PR number: `pr-review fetch <number>` prints a ref such as `refs/pr-review/pr-123`. Use it as `--head`. This doesn't switch the user's branch.
   - The user named branches: use them.
   - Otherwise: head is the current branch and base is the tool's default (origin/HEAD, then main or master). Say which you picked in one line.
3. Pick the rigor from what the user said. "Quick look", "skim" or "just the big risks" means `quick`. "Careful", "thorough" or "before release" means `thorough`. Otherwise use `standard`.

## 2. Analyze

```bash
pr-review analyze --base <base> --head <head> --rigor <rigor> --quiet
pr-review summary
```

Tell the user in three or four lines: how many files changed, how many need a close read, how many areas, and any "high" gaps. If the summary says earlier findings were kept for some areas, mention it.

If more than about 40,000 changed lines are left to review, say the PR is very large. Suggest splitting it along the areas the summary lists, and ask whether to continue.

## 3. Plan the review

```bash
pr-review plan --json
```

The plan lists the close-read areas that haven't been reviewed at their current code, the commit range, the `/code-review` effort for the chosen rigor (quick is `low`, standard `high`, thorough `xhigh`), and a `mode`:

- `nothing`: every close-read area already has an up-to-date review. Skip to step 5.
- `single`: the close-read code is small enough for one `/code-review` pass. Use `single_command`.
- `per-area`: run one pass per area, in the order listed, using each area's `command`.

If the user asked for one area or file, review just that. If they asked for the deepest review, offer the cloud review (`ultra_command`) and mention that it can cost extra usage. Run it only if they agree.

## 4. Review with /code-review

Run the command from the plan with the `/code-review` skill, exactly as the plan gives it. `--max-findings all` keeps it from stopping at the default limit of 32. With `per-area`, run the areas one after another, because a second `/code-review` started while one is running moves into the foreground.

When the findings come back, save them. Write them to `.pr-review/code-review.json` as a JSON list in this shape (use `[]` if there were none):

```json
[{"severity": "high", "title": "One-line summary", "detail": "What happens and why",
  "file": "path/at/head.py", "line": 42, "confidence": "high", "suggestion": "How to fix it"}]
```

Use `high` for a bug this change adds that will break or is unsafe, `medium` for a likely bug, `low` for pre-existing problems, and `info` for nits. Then import them, naming the areas the pass covered so they count as reviewed even when nothing was found:

```bash
pr-review findings import --areas <ids from the plan> .pr-review/code-review.json
```

The import places each finding in the area its file belongs to, puts findings on files outside the PR at PR level, and lists anything it couldn't place. Look at what it skipped; it's usually a path that needs fixing.

**Fallback: reviewer subagents.** Use these instead of `/code-review` when that command isn't available (Claude Code older than v2.1.147), when the user asks for one reviewer per area, or when the plan is `per-area` with more than about eight areas and running them one by one would take too long. For each area in the plan, start a subagent (Agent or Task tool, general-purpose type), at most five at a time. Build each prompt from [reviewer.md](reviewer.md), filling in:

- `{AREA}`: the area id, such as `c3`
- `{AREA_NAME}`: its name
- `{REVIEW}`: the review path from `pr-review status --json`
- `{REPO}`: the repository root

Each reviewer saves its own findings with `pr-review findings add`. If one reports that saving failed, fix the JSON it sent you and run `pr-review findings add --area <id> --review <path> <file>` yourself.

Don't review the skim and skip areas either way. Their static checks are already in the gap list.

## 5. Look across areas

```bash
pr-review pack --cross
```

This lists breaking changes that are used in other areas or outside the PR, new import loops, links between areas, and every area's findings so far. Look for problems a review focused on one area could miss:

- a function changed in one area while its callers in another area weren't updated
- two areas making assumptions about the same data or config that don't match
- the same mistake repeated in several areas (report it once, listing the places)
- findings that contradict each other

Save anything new, along with a PR-level summary:

```bash
pr-review findings add --pr - <<'JSON'
{"summary": "<3-6 sentences: what the change does, overall risk, the most important things to fix>",
 "findings": [ ...same format as the reviewers use... ]}
JSON
```

## 6. Report to the user

Keep it short and lead with what to do:

1. One or two sentences on what the change does and how risky it is.
2. Findings, high severity first. Give each as `path:line`, a short title, and one sentence on why it matters. Show at most about eight. Say how many more there are.
3. What wasn't read by an agent: the skim and skip counts, and any area left out because of cost.
4. Next steps: "say *look at c4* to dig into an area", `pr-review app` to browse everything with the findings attached, `/code-review --fix` to apply fixes, or a rerun with `--rigor thorough`.

Don't paste diffs or whole packs. Don't repeat findings the user can already see in the app unless they ask.

## 7. Follow-ups

- **"Look closer at X"** (an area, file or function): run `pr-review pack <area>` and read it here, then open the files you need. Save any new findings to that area with `--keep`.
- **"Why is X flagged?"**: `pr-review summary --cluster <area>` lists score reasons and gaps.
- **"Is this a false positive?"**: check the call sites in the pack or the source. If it is, say so. To record that, add an `info` finding explaining why.
- **"The author pushed new commits"**: rerun steps 2 to 4. Areas whose code didn't change keep their findings, so `pr-review plan` only lists the areas that need review again.
- **"Fix them"**: that's `/code-review --fix`, or editing the files directly. Ask first, because this skill is otherwise read-only.
- **"Post them on the PR"**: `/code-review --comment` posts inline comments. Only do this when the user asks.
- **"More accurate"** or many "possible" callers: `pr-review refine` re-checks who uses each changed function with jedi (about 15 seconds per 100 functions). Run it before step 4 for thorough reviews.
- **"Stricter" or "cheaper"**: `pr-review retier --deep-score <n>` or `--token-budget <n>` changes which areas count as close-read without re-running the analysis.

## Rules

- Stay read-only. Don't commit, push, comment on GitHub, approve, request changes, or pass `--fix` or `--comment` unless the user asks. Writing to `.pr-review/` is fine.
- Every finding needs a file, and a line wherever there is one, at the head commit. Check the line before citing it.
- Report problems, not taste. Leave out style nits, naming preferences and formatting unless the user asks for them.
- Say how sure you are. Static analysis misses dynamic calls (fixtures, `importlib`, Django settings, plugin registries), so a "nothing uses this" claim needs a quick text search before you rely on it.
- If `pr-review` fails, show the error in one line and suggest the fix it gives. Don't fall back to reading the whole diff into this conversation.
