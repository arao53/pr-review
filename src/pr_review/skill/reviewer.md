# Prompt for one area reviewer

Fill in the placeholders and send the text below as the subagent's prompt.

---

You are reviewing one area of a pull request: area **{AREA}** ("{AREA_NAME}") in the repository at `{REPO}`. Other reviewers have the other areas, so stay inside this one. Read code outside it only to check how this area's changes affect it.

**1. Get your context.** Run:

```bash
cd {REPO} && pr-review pack {AREA} --review {REVIEW}
```

The pack lists what static checks already flagged. Then, for each file to read closely, it gives the reasons, the changed functions with their old and new signatures, real call sites with surrounding lines, and the meaningful changes. Line numbers refer to the head commit named at the top. If your checkout is on a different commit, read files with the `git show` command the pack gives you. Open more of a file whenever the pack isn't enough to judge.

**2. Review.** In rough order of importance, look for:

1. Code that will fail or do the wrong thing: logic errors, wrong conditions, off-by-one, missing `await`, wrong types, unhandled `None`, broken error handling, resources not closed.
2. Changes that break other code. For each function marked "may break callers" or "removed", check every call site listed. A call that still matches the new signature is fine. For example, a renamed parameter doesn't break positional calls. Say which calls actually break.
3. Security: injection (SQL, shell, templates), secrets in code, unsafe deserialization, missing permission checks, sensitive data in logs.
4. Data safety: migrations that lose data or can't be rolled back, changed defaults, behaviour that differs for existing records.
5. Concurrency and state: shared mutable state, mutable default arguments, race conditions, caching that goes stale.
6. Missing tests for behaviour that matters, especially for anything in 1 to 4.

Confirm or dismiss each static flag in the pack rather than repeating it. Don't report style, naming or formatting.

**3. Save your findings.** Write them to `{REPO}/.pr-review/findings-{AREA}.json` in this format:

```json
{
  "summary": "2-4 sentences: what this area changes and how risky it is.",
  "findings": [
    {
      "severity": "high",
      "title": "invoice.total() still passes tax= after the rename",
      "detail": "compute_total renamed tax to tax_rate; this keyword call now raises TypeError.",
      "file": "src/billing/invoice.py",
      "line": 88,
      "confidence": "high",
      "suggestion": "Pass tax_rate= instead."
    }
  ]
}
```

- `severity`: `high` (will break or is unsafe), `medium` (likely bug or real risk), `low` (worth fixing), `info` (context, or a static flag you checked and dismissed).
- `confidence`: `high` when you checked it in the code, `medium` when it is likely, `low` when it is a guess worth a look.
- `file` and `line` must point at the head commit. Every finding needs a file, and a line wherever there is one.
- An empty `findings` list is a valid result when the area looks fine.

Then save them into the review:

```bash
cd {REPO} && pr-review findings add --area {AREA} --review {REVIEW} .pr-review/findings-{AREA}.json
```

If the command reports a problem, fix the JSON and run it again. Nothing is saved until it succeeds.

**4. Reply** in at most five lines: the number of findings by severity, the single most important one with its `file:line`, and whether saving worked. Don't include the diff or the full findings; they are in the review file.
