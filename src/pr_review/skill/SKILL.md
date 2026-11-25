---
name: pr-review
description: Review a large Python pull request area by area with the pr-review tool. Use when the user asks to review a PR, branch or diff, wants the risks, breaking changes or missing tests in a change, asks which parts of a big change need attention, or mentions pr-review. Runs the static analysis, sends one reviewer subagent per high-risk area, and writes the findings back so the user can browse them in the pr-review app.
---

# Reviewing a pull request with pr-review

`pr-review` does the static work: it sorts the changed files into *read closely*, *skim* and *skip*, groups them into areas, and lists changed functions with their callers. Your job is the judgment. Reviewer subagents read the close-read areas, you connect what they found, and the user gets a short, cited summary they can act on.

Keep diffs out of the main conversation. Subagents pull their own context with `pr-review pack`. You read summaries, status and findings.

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

## 3. Plan the agent review

```bash
pr-review status --json
```

Areas with `"needs_review": true` are the close-read areas that no agent has reviewed at their current code. Add up their `est_tokens`.

- If the total is over 150,000 tokens, tell the user the number of areas and the estimate. Offer `--rigor quick`, a lower count, or going ahead. Wait for an answer.
- If nothing needs review, skip to step 5.
- If the user asked for a specific area or file only, review just that.

## 4. Send one reviewer per area

For each area that needs review, start a subagent (the Agent or Task tool, general-purpose type). Run them in parallel, at most five at a time. Build each prompt from [reviewer.md](reviewer.md), filling in:

- `{AREA}`: the area id, such as `c3`
- `{AREA_NAME}`: its name
- `{REVIEW}`: the `review` path printed by `pr-review status --json`
- `{REPO}`: the repository root

The reviewer saves its own findings with `pr-review findings add`. You only get back a short reply. If a reviewer reports that saving failed, fix the JSON it sent you and run `pr-review findings add --area <id> --review <path> <file>` yourself.

Don't review the skim and skip areas with agents. Their static checks are already in the gap list.

## 5. Look across areas

```bash
pr-review pack --cross
```

This lists breaking changes that are used in other areas or outside the PR, new import loops, links between areas, and every area's findings so far. Look for problems no single reviewer could see:

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
4. Next steps: "say *look at c4* to dig into an area", `pr-review app` to browse everything with the findings attached, or a rerun with `--rigor thorough`.

Don't paste diffs or whole packs. Don't repeat findings the user can already see in the app unless they ask.

## 7. Follow-ups

- **"Look closer at X"** (an area, file or function): run `pr-review pack <area>` and read it here, then open the files you need. Save any new findings to that area with `--keep`.
- **"Why is X flagged?"**: `pr-review summary --cluster <area>` lists score reasons and gaps.
- **"Is this a false positive?"**: check the call sites in the pack or the source. If it is, say so. To record that, add an `info` finding explaining why.
- **"The author pushed new commits"**: rerun step 2. Areas whose code didn't change keep their findings, and `pr-review status` shows which areas need review again.
- **"More accurate"** or many "possible" callers: `pr-review refine` re-checks who uses each changed function with jedi (about 15 seconds per 100 functions). Run it before step 4 for thorough reviews.
- **"Stricter" or "cheaper"**: `pr-review retier --deep-score <n>` or `--token-budget <n>` changes which areas count as close-read without re-running the analysis.

## Rules

- Stay read-only. Don't commit, push, comment on GitHub, approve, or request changes unless the user asks. Writing to `.pr-review/` is fine.
- Every finding needs a file, and a line wherever there is one, at the head commit. Check the line before citing it.
- Report problems, not taste. Leave out style nits, naming preferences and formatting unless the user asks for them.
- Say how sure you are. Static analysis misses dynamic calls (fixtures, `importlib`, Django settings, plugin registries), so a "nothing uses this" claim needs a quick text search before you rely on it.
- If `pr-review` fails, show the error in one line and suggest the fix it gives. Don't fall back to reading the whole diff into this conversation.
