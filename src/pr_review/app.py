import marimo

__generated_with = "0.26.0"
app = marimo.App(width="medium", app_title="Pull request review", css_file="app.css")


@app.cell(hide_code=True)
def _():
    import html
    import os
    from pathlib import Path

    import marimo as mo

    from pr_review import appkit, derive
    from pr_review import refine as refine_mod
    from pr_review.analyze import analyze as run_analysis
    from pr_review.cli import repo_config
    return Path, appkit, derive, html, mo, os, refine_mod, repo_config, run_analysis


@app.cell(hide_code=True)
def _(mo):
    # App-wide state: the loaded review, where the reader is, which tab is open.
    get_review, set_review = mo.state(None)
    get_nav, set_nav = mo.state({"area": None, "file": None, "symbol": None})
    get_tab, set_tab = mo.state("Map")
    get_tick, set_tick = mo.state(0)
    get_fetched, set_fetched = mo.state(None)

    def go(area=None, file=None, symbol=None):
        set_nav({"area": area, "file": file, "symbol": symbol})
        set_tab("Map")
    return (get_fetched, get_nav, get_review, get_tab, get_tick, go, set_fetched,
            set_review, set_tab, set_tick)


@app.cell(hide_code=True)
def _(mo):
    mo.md("""
    # Pull request review

    Work through a large change before it is merged. Pick the change below and press
    **Analyze**. The tool sorts the files into what needs a close read, what you can skim
    and what you can skip, then walks you through it one area at a time.
    """)
    return


@app.cell(hide_code=True)
def _(mo, os):
    folder = mo.ui.text(
        value=os.environ.get("PR_REVIEW_REPO") or os.getcwd(),
        label="Project folder",
        full_width=True,
    )
    return (folder,)


@app.cell(hide_code=True)
def _(appkit, folder, get_fetched):
    get_fetched()  # reload the list after a pull request download
    repo = appkit.find_repo(folder.value)
    versions = appkit.list_versions(repo) if repo else None
    saved = appkit.saved_reviews(repo) if repo else []
    return repo, saved, versions


@app.cell(hide_code=True)
def _(appkit, derive, get_fetched, mo, repo, saved, versions):
    if repo is None or not versions or not versions["refs"]:
        head_pick = base_pick = pr_number = fetch_btn = rigor_pick = analyze_btn = saved_pick = open_btn = None
    else:
        _opts = {f"{appkit.ref_label(n)}  ·  updated {w}": n for n, w in versions["refs"]}
        _label = {v: k for k, v in _opts.items()}
        _first = next(iter(_opts))
        head_pick = mo.ui.dropdown(
            _opts, value=_label.get(get_fetched()) or _label.get(versions["suggested_head"])
            or _label.get(versions["current"]) or _first,
            label="Version with the changes", searchable=True, full_width=True)
        base_pick = mo.ui.dropdown(
            _opts, value=_label.get(versions["default_base"]) or _first,
            label="Version it will be merged into", searchable=True, full_width=True)
        pr_number = mo.ui.text(placeholder="e.g. 1234", label="Pull request number")
        fetch_btn = mo.ui.run_button(label="Download it")
        rigor_pick = mo.ui.radio(
            {f"{n.capitalize()}: {derive.PRESET_BLURB[n]}": n for n in derive.PRESETS},
            value=f"Standard: {derive.PRESET_BLURB['standard']}")
        analyze_btn = mo.ui.run_button(label="Analyze this change", kind="success")
        saved_pick = mo.ui.dropdown({lbl: str(p) for lbl, p in saved}, value=saved[0][0] if saved else None,
                                    label="Reopen a saved review", full_width=True) if saved else None
        open_btn = mo.ui.run_button(label="Open") if saved else None
    return analyze_btn, base_pick, fetch_btn, head_pick, open_btn, pr_number, rigor_pick, saved_pick


@app.cell(hide_code=True)
def _(analyze_btn, appkit, base_pick, fetch_btn, folder, get_review, head_pick, mo, open_btn, pr_number, repo,
      rigor_pick, saved_pick):
    if repo is None:
        _body = [folder, mo.callout(mo.md(
            "This folder isn't a project tracked by git. Paste the path of the folder that holds the code, "
            "for example `/Users/you/projects/shop`."), kind="warn")]
    elif head_pick is None:
        _body = [folder, mo.callout(mo.md("This project has no saved versions yet, so there is nothing to compare."),
                                    kind="warn")]
    else:
        _same = appkit.explain_same(repo, base_pick.value, head_pick.value)
        _body = [
            folder,
            mo.md("### 1. What do you want to review?"),
            mo.hstack([head_pick, mo.md("<div style='padding-top:28px;color:#8a9aa9'>→</div>"), base_pick],
                      widths=[10, 1, 10], align="start"),
            *([mo.callout(mo.md(_same), kind="warn")] if _same else []),
            mo.accordion({"Reviewing a GitHub pull request that isn't on your computer yet?": mo.vstack([
                mo.md("Type its number (the digits after `/pull/` in its web address) and press **Download it**. "
                      "It will then appear in the list above."),
                mo.hstack([pr_number, fetch_btn], justify="start", align="end"),
            ])}),
            mo.md("### 2. How careful should the review be?"),
            rigor_pick,
            mo.hstack([analyze_btn, mo.md("<span style='color:#6b7785'>Usually takes a few seconds.</span>")],
                      justify="start", align="center"),
        ]
        if saved_pick is not None:
            _body.append(mo.accordion({"Or reopen a review you ran before": mo.hstack([saved_pick, open_btn],
                                                                                    align="end", widths=[5, 1])}))
    _setup = mo.vstack(_body, gap=0.8)
    # Once a review is open, fold the setup away so the review gets the screen.
    (mo.accordion({"Review a different change": _setup}) if get_review() is not None and head_pick is not None
     else _setup)
    return


@app.cell(hide_code=True)
def _(appkit, fetch_btn, mo, pr_number, repo, set_fetched):
    mo.stop(fetch_btn is None or not fetch_btn.value)
    _raw = (pr_number.value or "").strip().lstrip("#")
    if not _raw.isdigit():
        _out = mo.callout(mo.md("Type just the number, like `1234`."), kind="warn")
    else:
        try:
            with mo.status.spinner(title=f"Downloading pull request #{_raw}"):
                _ref = appkit.fetch_pull_request(repo, int(_raw))
            set_fetched(_ref)
            _out = mo.callout(mo.md(f"Pull request #{_raw} is downloaded and selected as the version with the "
                                    "changes. Press **Analyze this change**."), kind="success")
        except Exception as _e:
            _out = mo.callout(mo.md(
                f"Couldn't download pull request #{_raw}. Check the number, and that this project came from GitHub "
                "and you can reach it. If this folder is a copy of your fork, the pull request lives on the original "
                "project: add it with `git remote add upstream <its address>` and try again."
                f"\n\n<small>Details: {_e}</small>"), kind="danger")
    _out
    return


@app.cell(hide_code=True)
def _(analyze_btn, appkit, base_pick, derive, go, head_pick, mo, repo, repo_config, rigor_pick,
      run_analysis, set_review):
    mo.stop(analyze_btn is None or not analyze_btn.value)
    if head_pick.value == base_pick.value:
        _out = mo.callout(mo.md("Pick two different versions: the one with the changes, and the one it goes into."),
                          kind="warn")
    elif appkit.explain_same(repo, base_pick.value, head_pick.value):
        _out = mo.callout(mo.md(appkit.explain_same(repo, base_pick.value, head_pick.value)), kind="warn")
    else:
        try:
            with mo.status.spinner(title="Analyzing the change", subtitle="Getting started") as _sp:
                _opts = derive.resolve_options(rigor_pick.value, repo_config(repo))
                _r = run_analysis(repo, base=base_pick.value, head=head_pick.value, options=_opts,
                                  progress=lambda m: _sp.update(subtitle=m))
            if not _r["stats"]["files_changed"]:
                _out = mo.callout(mo.md(
                    appkit.explain_same(repo, base_pick.value, head_pick.value)
                    or "These two versions have the same code, so there is nothing to review."), kind="warn")
            else:
                appkit.save(_r, repo, fresh=True)
                set_review(_r)
                go()
                _out = mo.md("")
        except Exception as _e:
            _out = mo.callout(mo.md(f"The analysis stopped with an error: {_e}"), kind="danger")
    _out
    return


@app.cell(hide_code=True)
def _(Path, appkit, get_review, go, mo, open_btn, saved, saved_pick, set_review):
    # Open a saved review: on request, or the newest one when the page first loads.
    if open_btn is not None and open_btn.value:
        set_review(appkit.load(Path(saved_pick.value)))
        go()
    elif get_review() is None and saved:
        set_review(appkit.load(saved[0][1]))
    mo.md("")
    return


@app.cell(hide_code=True)
def _(get_review, mo):
    # Assign only after the check: if `review` were set to None here, a later
    # re-run of a step that also depends on something else (like the project
    # folder) would see None instead of being skipped.
    _loaded = get_review()
    mo.stop(_loaded is None, mo.callout(mo.md(
        "Pick the two versions above and press **Analyze this change**."), kind="info"))
    review = _loaded
    return (review,)


@app.cell(hide_code=True)
def _(mo, review):
    # Knobs that change how strict the review is, without re-running the analysis.
    _r = review.get("rigor") or {}
    deep_slider = mo.ui.slider(0, 100, step=5, value=_r.get("deep_score", 55), show_value=True, full_width=True,
                               label="Read closely when the risk score is at least")
    skip_slider = mo.ui.slider(0, 100, step=5, value=_r.get("skip_score", 20), show_value=True, full_width=True,
                               label="Mark as safe to skip when the risk score is below")
    _budgets = {"No limit": 0, "30k tokens (very small)": 30_000, "60k tokens": 60_000, "120k tokens": 120_000,
                "250k tokens": 250_000}
    _cur = _r.get("token_budget") or 0
    budget_pick = mo.ui.dropdown(_budgets, value=next((k for k, v in _budgets.items() if v == _cur), "No limit"),
                                 label="Limit on AI review size")
    callers_slider = mo.ui.slider(1, 10, value=_r.get("context_callers", 3), show_value=True, full_width=True,
                                  label="Usages of each changed function shown to the AI")
    save_settings_btn = mo.ui.run_button(label="Save these settings")
    _todo = sum(1 for s in review.get("symbols", []) if s["kind"] != "module" and s["change"] != "removed"
                and s.get("resolution") != "jedi")
    refine_count = mo.ui.slider(10, max(10, min(1000, _todo or 10)), step=10, value=min(max(10, _todo), 150),
                                show_value=True, label="Functions to check", full_width=True)
    refine_btn = mo.ui.run_button(label="Improve accuracy", tooltip="Re-check who uses each changed function")
    return budget_pick, callers_slider, deep_slider, refine_btn, refine_count, save_settings_btn, skip_slider


@app.cell(hide_code=True)
def _(Path, appkit, budget_pick, callers_slider, deep_slider, derive, get_tick, repo, review, skip_slider):
    get_tick()
    view = review
    derive.apply_tiers(view, {**(view.get("rigor") or {}), "deep_score": deep_slider.value,
                              "skip_score": min(skip_slider.value, deep_slider.value),
                              "token_budget": budget_pick.value or None, "context_callers": callers_slider.value})
    review_repo = Path(view["meta"].get("repo_path") or repo)
    progress = appkit.Progress(review_repo, view)
    files_by_path = {f["path"]: f for f in view["files"]}
    clusters_by_id = {c["id"]: c for c in view["clusters"]}
    return clusters_by_id, files_by_path, progress, review_repo, view


@app.cell(hide_code=True)
def _(appkit, mo, refine_btn, view):
    _b = view["budget"]
    _done_rf = (view["meta"].get("refine") or {}).get("status") == "done"
    _acc = ("Usages checked with the accurate search." if _done_rf else
            "Usages found with the quick search. It can miss or over-count some.")
    mo.vstack([
        mo.md(f"## {appkit.ref_label(view['meta']['head'])} → {view['meta']['base']}"),
        mo.md(appkit.headline(view)),
        mo.callout(mo.md(f"**AI review summary.** {view['summary']}"), kind="info") if view.get("summary") else mo.md(""),
        mo.hstack([
            mo.stat(f"{_b['files']['deep']}", label="Files to read closely", caption=f"{_b['lines_deep']:,} lines",
                    bordered=True),
            mo.stat(f"{_b['files']['skim']}", label="Files to skim", caption=f"{_b['lines_skim']:,} lines", bordered=True),
            mo.stat(f"{_b['files']['skip'] + view['stats']['collapsed_files']}", label="Files you can skip",
                    bordered=True),
            mo.stat(appkit.fmt_minutes(_b["est_minutes"]), label="Rough reading time", bordered=True),
            mo.stat(appkit.fmt_tokens(_b["est_tokens"]), label="AI review size",
                    caption=f"vs {appkit.fmt_tokens(_b['baseline_tokens'])} to read it all", bordered=True),
        ], widths="equal", gap=0.6),
        mo.hstack([mo.md(f"<span style='color:#6b7785'>{_acc}</span>"), refine_btn], justify="space-between",
                  align="center"),
    ], gap=0.6)
    return


@app.cell(hide_code=True)
def _(appkit, mo, refine_btn, refine_count, refine_mod, review_repo, set_review, view):
    mo.stop(not refine_btn.value)
    if not refine_mod.available():
        _out = mo.callout(mo.md("The accurate search needs one more package. In a terminal, run "
                                "`pip install 'pr-review[refine]'`, then reopen this page."), kind="warn")
    else:
        _n = refine_count.value
        with mo.status.progress_bar(total=_n, title="Checking who uses each changed function",
                                    completion_title="Done", remove_on_exit=True) as _bar:
            _stats = refine_mod.refine(view, max_symbols=_n, repo=review_repo,
                                       progress=lambda i, t, name: _bar.update(subtitle=name))
        appkit.save(view, review_repo)
        set_review(dict(view))
        _out = mo.callout(mo.md(
            f"Checked **{_stats['checked']}** functions in {_stats['seconds']} s. "
            f"Confirmed {_stats['confirmed']} uses, found {_stats['added']} the quick search missed, "
            f"and dropped {_stats['dropped']} wrong guesses."
            + (f" {_stats['remaining']} lower-priority functions still use the quick search."
               if _stats["remaining"] else "")), kind="success")
    _out
    return


@app.cell(hide_code=True)
def _(clusters_by_id, files_by_path, get_nav):
    nav = get_nav()
    cur_area = clusters_by_id.get(nav["area"])
    cur_file = files_by_path.get(nav["file"])
    if cur_file and not cur_area and cur_file.get("cluster"):
        cur_area = clusters_by_id.get(cur_file["cluster"])
    return cur_area, cur_file, nav


@app.cell(hide_code=True)
def _(appkit, cur_area, cur_file, go, mo):
    _items = [mo.ui.button(label="All areas", on_click=lambda _: go())]
    if cur_area:
        _items.append(mo.ui.button(label=appkit.short(cur_area["name"], 40),
                                   on_click=lambda _, a=cur_area["id"]: go(a)))
    if cur_file:
        _items.append(mo.ui.button(label=appkit.short(cur_file["path"].split("/")[-1], 40),
                                   on_click=lambda _, a=cur_area["id"] if cur_area else None, f=cur_file["path"]: go(a, f)))
    crumbs = mo.ui.array(_items)
    _sep = mo.md("<span style='color:#b9c2cc'>›</span>")
    _parts = []
    for _i, _b in enumerate(crumbs):
        if _i:
            _parts.append(_sep)
        _parts.append(_b)
    crumb_bar = mo.hstack(_parts, justify="start", align="center", gap=0.4)
    return crumb_bar, crumbs


@app.cell(hide_code=True)
def _(appkit, cur_area, cur_file, go, mo, progress, view):
    # Level 1: every area of the change.
    if cur_area or cur_file:
        areas_chart_ui = area_open = None
        areas_view = None
    else:
        def _pick(v):
            if v is not None and len(v):
                go(list(v["id"])[0])

        areas_chart_ui = mo.ui.altair_chart(appkit.areas_chart(view), on_change=_pick, legend_selection=False)
        _order = sorted(view["clusters"], key=lambda c: (appkit.TIER_ORDER[c["tier"]], -c["stats"]["priority"]))
        area_open = mo.ui.array([mo.ui.button(label="Open", on_click=lambda _, a=c["id"]: go(a)) for c in _order])
        _cards = []
        for _c, _btn in zip(_order, area_open):
            _n = len([g for g in view["gaps"] if g.get("cluster") == _c["id"] and g["severity"] in ("high", "medium")])
            _done = progress.count(_c["files"])
            _nf = len(_c.get("findings", []))
            _info = (f"{_c['stats']['files']} files · +{_c['stats']['added']:,} / −{_c['stats']['removed']:,} lines"
                     + (f" · <b>{_n} to check</b>" if _n else "")
                     + (f" · {_nf} AI finding{'s' if _nf != 1 else ''}" if _nf else
                        (" · AI review out of date" if _c.get("previous") else ""))
                     + f" · {_done}/{len(_c['files'])} done")
            _cards.append(mo.hstack([
                mo.md(f"<div class='pr-card'>{appkit.badge(_c['tier'])} "
                      f"<span class='pr-card-title'>{appkit.short(_c['name'], 48)}</span><br>"
                      f"<span class='pr-muted'>{_info}</span></div>"),
                _btn], justify="space-between", align="center", widths=[8, 1]))
        areas_view = mo.vstack([
            mo.md("Each bubble is an **area**: files that belong together. Bigger means more changed lines; "
                  "lines show which areas use each other. **Click a bubble, or press Open**, to look inside. "
                  "Work from the top of the list down."),
            mo.hstack([areas_chart_ui, mo.vstack(_cards, gap=0.4)], widths=[5, 4], align="start", gap=1.2),
        ], gap=0.8)
    return area_open, areas_chart_ui, areas_view


@app.cell(hide_code=True)
def _(appkit, cur_area, cur_file, go, mo, progress, view):
    # Level 2: the files in one area.
    if not cur_area or cur_file:
        files_chart_ui = file_table = None
        area_view = None
    else:
        def _pick_file(v):
            if v is not None and len(v):
                go(cur_area["id"], list(v["id"])[0])

        files_chart_ui = mo.ui.altair_chart(appkit.files_chart(view, cur_area["id"]), on_change=_pick_file,
                                            legend_selection=False)
        _f = {f["path"]: f for f in view["files"]}
        _order = sorted(cur_area["files"], key=lambda p: (appkit.TIER_ORDER[_f[p]["tier"]], -_f[p]["score"], p))
        file_table = mo.ui.array([mo.ui.button(label="Open", on_click=lambda _, p=p: go(cur_area["id"], p))
                                  for p in _order])
        _rows = []
        for _p, _btn in zip(_order, file_table):
            _x = _f[_p]
            _n = sum(1 for g in view["gaps"] if (g.get("file") == _p or _p in g.get("files", [])) and g["severity"] != "info")
            _tick = ("<span style='color:#2f9e44;font-weight:700' title='You marked this as reviewed'>✓</span>"
                     if progress.done(_p) else "<span style='color:#d0d5db'>○</span>")
            _rows.append(mo.hstack([
                mo.md(f"<div class='pr-row'>{_tick} {appkit.badge(_x['tier'])} <code>{_p}</code><br>"
                      f"<span class='pr-muted'>+{_x['added']} / −{_x['removed']} lines · risk {_x['score']}"
                      + (f" · <b>{_n} to check</b>" if _n else "") + "</span></div>"),
                _btn], justify="space-between", align="center", widths=[9, 1]))
        _gaps = [g for g in view["gaps"] if g.get("cluster") == cur_area["id"] and g["severity"] != "info"]
        _checks = [mo.callout(mo.md(appkit.finding_md(f)), kind=appkit.FINDING_KIND[f["severity"]])
                   for f in cur_area.get("findings", [])[:8]]
        if cur_area.get("summary"):
            _checks.insert(0, mo.md(f"<span class='pr-muted'>AI review:</span> {cur_area['summary']}"))
        _checks += [mo.callout(mo.md(f"**{appkit.gap_help(g['kind'])[0]}**. {g['message']}"),
                              kind=appkit.SEVERITY[g["severity"]]["kind"]) for g in _gaps[:8]]
        area_view = mo.vstack([
            mo.md(f"## {cur_area['name']}  {appkit.badge(cur_area['tier'])}"),
            mo.md("<span class='pr-muted'>Why: " + "; ".join(cur_area["tier_reasons"]) + ".</span>"),
            mo.hstack([
                mo.vstack([mo.md("**How these files use each other.** Click a bubble to open a file."), files_chart_ui]),
                mo.vstack([mo.md("**Things to check here**"), *(_checks or [mo.md("<span class='pr-muted'>Nothing flagged.</span>")])]),
            ], widths=[5, 4], align="start", gap=1.2),
            mo.md("**Files, most important first.** Tests show what the code is supposed to do, so they help "
                  "when the code itself is hard to follow."),
            mo.vstack(_rows, gap=0.2),
            mo.accordion({"Ask Claude to review this area": mo.vstack([
                mo.md("Copy this into Claude Code while you are in the project folder:"),
                mo.ui.text_area(value=appkit.claude_prompt(view, cluster=cur_area), rows=5, full_width=True),
            ])}),
        ], gap=0.9)
    return area_view, file_table, files_chart_ui


@app.cell(hide_code=True)
def _(appkit, cur_area, cur_file, go, html, mo, nav, progress, set_tick, view):
    # Level 3: one file, its changed functions and the diff.
    if not cur_file:
        done_switch = symbol_table = None
        file_view = None
    else:
        _path = cur_file["path"]

        def _mark(v):
            progress.set(_path, v)
            set_tick(lambda t: t + 1)

        done_switch = mo.ui.switch(value=progress.done(_path), label="I've reviewed this file", on_change=_mark)
        _syms = [s for s in view.get("symbols", []) if s["path"] == _path and s["kind"] != "module"]
        _syms = sorted(_syms, key=appkit.symbol_rank)
        _sel = nav["symbol"]
        symbol_table = mo.ui.array([
            mo.ui.button(label="Hide" if s["id"] == _sel else "Where it's used",
                         on_click=lambda _, i=s["id"]: go(cur_area["id"] if cur_area else None, _path,
                                                         None if i == _sel else i))
            for s in _syms]) if _syms else None
        _sym = next((s for s in _syms if s["id"] == _sel), None)

        def _detail_for(s):
            _rows2 = [{"Where": f"{c['path']}:{c['line']}", "Inside": c.get("caller") or "(top of file)",
                       "Part of this change?": ("yes, line edited" if c.get("touched") else "yes, line not edited")
                       if c.get("in_pr") else "no",
                       "How sure": "certain" if c["confidence"] == "high" else "possible (same name)"}
                      for c in s["callers"]]
            _sig = ""
            if s["old_sig"] and s["new_sig"] and s["old_sig"] != s["new_sig"]:
                _sig = (f"<div class='pr-muted'>Before: <code>{html.escape(s['old_sig'])}</code></div>"
                        f"<div class='pr-muted'>After: <code>{html.escape(s['new_sig'])}</code></div>")
            _how = "accurate" if s.get("resolution") == "jedi" else "quick"
            return mo.vstack([
                mo.Html(f"<div style='margin-top:4px'>{_sig}</div>") if _sig else mo.md(""),
                mo.ui.table(_rows2, selection=None, show_download=False, show_column_summaries=False,
                            show_data_types=False, show_search=False, page_size=8) if _rows2 else
                mo.md("<span class='pr-muted'>Nothing else uses it.</span>"),
                mo.md(f"<span class='pr-muted'>Found by the {_how} search"
                      f"{'; some were left out because there are so many' if s.get('callers_truncated') else ''}. "
                      "Its lines are marked with a blue bar in the changes below.</span>"),
            ], gap=0.3)

        _sym_rows = []
        for _s, _btn in zip(_syms, symbol_table or []):
            _flag = appkit.pill(appkit.symbol_change_label(_s), "#fbe3e6", "#b42335") if _s["breaking"] else \
                appkit.pill(appkit.symbol_change_label(_s))
            _row = [mo.hstack([
                mo.md(f"<div class='pr-row'><code>{html.escape(_s['qualname'])}</code> {_flag}<br>"
                      f"<span class='pr-muted'>{appkit.usage_text(_s)}</span></div>"), _btn],
                justify="space-between", align="center", widths=[8, 2])]
            if _s is _sym:
                _row.append(_detail_for(_s))
            _sym_rows.append(mo.vstack(_row, gap=0.2))
        _top = 8 if not _sym else max(8, _syms.index(_sym) + 1)
        if len(_sym_rows) > _top:
            _sym_rows = _sym_rows[:_top] + [mo.accordion({f"{len(_sym_rows) - _top} more changed functions": mo.vstack(
                _sym_rows[_top:], gap=0.2)})]

        _reasons = [appkit.plain_reason(r) for r in cur_file["score_reasons"]]
        _reason_md = "\n".join(f"- {txt} <span class='pr-muted'>({pts})</span>" for pts, txt in _reasons)
        _gaps = [g for g in view["gaps"] if g.get("file") == _path or _path in g.get("files", [])]
        _gap_blocks = [mo.callout(mo.md(appkit.finding_md(f, with_where=False) + (
            f"  \n<span class='pr-muted'>line {f['line']}</span>" if f.get("line") else "")),
            kind=appkit.FINDING_KIND[f["severity"]]) for f in cur_file.get("findings", [])]
        for _g in _gaps:
            _t, _what, _do = appkit.gap_help(_g["kind"])
            _ev = "".join(f"<li><code>{e}</code></li>" for e in _g.get("evidence", [])[:8])
            _gap_blocks.append(mo.callout(mo.md(
                f"**{_t}**\n\n{_g['message']}" + (f"\n\n{_what}" if _what else "") + (f"\n\n*What to do:* {_do}" if _do else "")
                + (f"<ul>{_ev}</ul>" if _ev else "")), kind=appkit.SEVERITY[_g["severity"]]["kind"]))

        _signals = cur_file.get("signals", [])
        file_view = mo.vstack([
            mo.md(f"## `{_path}`  {appkit.badge(cur_file['tier'])}"),
            mo.hstack([mo.md("<span class='pr-muted'>" + "; ".join(cur_file.get("tier_reasons", [])) + ".</span>"),
                       done_switch], justify="space-between", align="center"),
            mo.hstack([
                mo.vstack([mo.md(f"**Why it was flagged** (risk score {cur_file['score']} of 100)"), mo.md(_reason_md or "-")]),
                mo.vstack([mo.md("**Things to check**"),
                           *(_gap_blocks or [mo.md("<span class='pr-muted'>Nothing flagged in this file.</span>")])]),
            ], widths=[1, 1], align="start", gap=1.2),
            mo.Html("<b>Risky patterns in the new lines</b><ul>" + "".join(
                f"<li>line {x['line']}: <code>{html.escape(x['detail'])}</code></li>" for x in _signals[:15])
                + "</ul>") if _signals else mo.md(""),
            mo.md("**Changed functions** <span class='pr-muted'>(the ones that can break other code come first)"
                  "</span>") if symbol_table else mo.md(""),
            mo.vstack(_sym_rows, gap=0.2) if _sym_rows else mo.md(""),
            mo.md("**The changes**  <span class='pr-muted'>green lines were added, red lines removed</span>"),
            mo.Html(appkit.diff_html(cur_file, focus=(_sym["line"], _sym["end"]) if _sym and _sym.get("line") else None)),
            mo.accordion({"Ask Claude to review this file": mo.ui.text_area(
                value=appkit.claude_prompt(view, file=cur_file), rows=5, full_width=True)}),
        ], gap=0.9)
    return done_switch, file_view, symbol_table


@app.cell(hide_code=True)
def _(appkit, go, mo, view):
    # "Things to check" tab: every flagged item, grouped by how urgent it is.
    _groups = {}
    for _g in view["gaps"]:
        _groups.setdefault(_g["severity"], []).append(_g)
    check_open = mo.ui.array([
        mo.ui.button(label="Open file", disabled=not g.get("file"),
                     on_click=lambda _, f=g.get("file"), c=g.get("cluster"): go(c, f) if f else go(c))
        for g in view["gaps"]])
    _idx = {id(_g): _i for _i, _g in enumerate(view["gaps"])}
    _sections = {}
    _af = appkit.all_findings(view)
    finding_open = mo.ui.array([
        mo.ui.button(label="Open file", disabled=not f.get("file"),
                     on_click=lambda _, f=f.get("file"), c=c: go(c, f) if f else go(c))
        for f, c in _af])
    if _af:
        _sections[f"From the AI review ({len(_af)})"] = mo.vstack([
            mo.hstack([mo.callout(mo.md(appkit.finding_md(f)), kind=appkit.FINDING_KIND[f["severity"]]), b],
                      justify="space-between", align="start", widths=[8, 1])
            for (f, _c), b in zip(_af, finding_open)], gap=0.6)
    for _sev in ("high", "medium", "low", "info"):
        _items = _groups.get(_sev, [])
        if not _items:
            continue
        _blocks = []
        for _g in _items:
            _t, _what, _do = appkit.gap_help(_g["kind"])
            _where = _g.get("file") or ", ".join(_g.get("files", [])[:3]) or "the whole change"
            _blocks.append(mo.hstack([
                mo.md(f"**{_t}**  \n{_g['message']}  \n<span class='pr-muted'>{_what} {('What to do: ' + _do) if _do else ''}"
                      f"</span>  \n<span class='pr-muted'>Where: <code>{_where}</code></span>"),
                check_open[_idx[id(_g)]]], justify="space-between", align="start", widths=[8, 1]))
        _sections[f"{'Automatic checks: ' if _af else ''}{appkit.SEVERITY[_sev]['label']} ({len(_items)})"] = \
            mo.vstack(_blocks, gap=0.8)
    checks_view = (mo.accordion(_sections, multiple=True, expanded=[k for k in _sections][:2]) if _sections
                   else mo.callout("Nothing was flagged.", kind="success"))
    return check_open, checks_view, finding_open


@app.cell(hide_code=True)
def _(appkit, go, html, mo, view):
    # "Structure" tab: how the shape of the code changes, beyond the lines.
    _d = view.get("delta") or {}
    _s = _d.get("symbols") or {}
    _brk_sig = sum(1 for x in view.get("symbols", []) if x["change"] == "signature" and x["breaking"])
    _lines = [
        f"- **{_s.get('added', 0)}** new functions or classes, **{_s.get('removed', 0)}** removed.",
        f"- **{_s.get('signature', 0)}** changed what inputs they take. **{_brk_sig}** of those can break code that "
        "uses them; the rest still accept the old calls.",
        f"- **{_s.get('modified', 0)}** changed only on the inside.",
        f"- Files now load **{_d.get('module_edges', {}).get('added_total', 0)}** other files they didn't before, and "
        f"stopped loading **{_d.get('module_edges', {}).get('removed_total', 0)}**.",
        f"- Changed functions call **{_d.get('call_edges', {}).get('added_total', 0)}** new things and stopped calling "
        f"**{_d.get('call_edges', {}).get('removed_total', 0)}**.",
    ]
    _parts = [mo.md("This is what changes in how the code fits together, as opposed to the lines themselves.\n\n"
                    + "\n".join(_lines))]
    _risky = sorted((x for x in view.get("symbols", []) if x["breaking"] and x["kind"] != "module"
                     and not x.get("class_removed")),
                    key=lambda x: (-x.get("untouched_callers", 0), -x.get("high_callers", 0), x["id"]))[:10]
    _files = {f["path"]: f for f in view["files"]}
    structure_open = mo.ui.array([
        mo.ui.button(label="Open", on_click=lambda _, x=x: go(_files.get(x["path"], {}).get("cluster"), x["path"], x["id"]))
        for x in _risky])
    if _risky:
        _parts.append(mo.md("**Changes most likely to break other code**"))
        _parts.append(mo.vstack([mo.hstack([
            mo.md(f"<div class='pr-row'><code>{html.escape(x['qualname'])}</code> "
                  f"{appkit.pill(appkit.symbol_change_label(x), '#fbe3e6', '#b42335')}<br>"
                  f"<span class='pr-muted'>in <code>{x['path']}</code> · {appkit.usage_text(x)}"
                  + (f" · <b>{x['untouched_callers']} not updated</b>" if x.get("untouched_callers") else "")
                  + "</span></div>"), b], justify="space-between", align="center", widths=[9, 1])
            for x, b in zip(_risky, structure_open)], gap=0.2))
    _cyc = _d.get("cycles") or []
    if _cyc:
        _parts.append(mo.callout(mo.md("**New import loops.** These files now depend on each other in a circle:\n\n" +
                                       "\n".join("- " + " → ".join(f"`{p}`" for p in c["files"]) for c in _cyc)), kind="warn"))
    else:
        _parts.append(mo.md("No new import loops."))
    _pk = (_d.get("package_edges") or {}).get("added") or []
    if _pk:
        _parts.append(mo.md("**Folders that now depend on each other for the first time**"))
        _parts.append(mo.ui.table([{"This folder": p["source"], "now uses": p["target"],
                                    "because of": ", ".join(f"{e['source'].split('/')[-1]} → {e['target'].split('/')[-1]}"
                                                            for e in p["edges"][:3])} for p in _pk],
                                  selection=None, show_download=False, show_data_types=False, show_column_summaries=False))
    _ce = (_d.get("call_edges") or {}).get("added") or []
    if _ce:
        _parts.append(mo.accordion({f"New calls between functions ({len(_ce)} shown)": mo.ui.table(
            [{"From": e["source"].split("::")[-1], "in file": e["source"].split("::")[0],
              "now calls": e["target"].split("::")[-1], "in": e["target"].split("::")[0]} for e in _ce],
            selection=None, show_download=False, show_data_types=False, show_column_summaries=False, page_size=15)}))
    structure_view = mo.vstack(_parts, gap=0.8)
    return structure_open, structure_view


@app.cell(hide_code=True)
def _(appkit, budget_pick, callers_slider, deep_slider, derive, mo, refine_btn, refine_count, save_settings_btn,
      skip_slider, view):
    _b = view["budget"]
    _rf = view["meta"].get("refine") or {}
    _rf_text = (f"Last run checked {_rf.get('checked')} functions in {_rf.get('seconds')} s."
                if _rf.get("status") == "done" else "Not run yet for this review.")
    settings_view = mo.vstack([
        mo.md("Turn these to trade care for time. Changes show up immediately everywhere on the page. "
              "Press **Save these settings** to keep them."),
        mo.hstack([
            mo.vstack([
                deep_slider,
                mo.md(f"<span class='pr-muted'>{derive.KNOBS['deep_score']} Lower means more reading.</span>"),
                skip_slider,
                mo.md(f"<span class='pr-muted'>{derive.KNOBS['skip_score']}</span>"),
                budget_pick,
                mo.md(f"<span class='pr-muted'>{derive.KNOBS['token_budget']}</span>"),
                callers_slider,
                mo.md(f"<span class='pr-muted'>{derive.KNOBS['context_callers']} More gives the AI better context "
                      "but costs more.</span>"),
                save_settings_btn,
            ], gap=0.5),
            mo.vstack([
                mo.md("**With these settings**"),
                mo.md(f"- Read closely: **{_b['files']['deep']} files** ({_b['lines_deep']:,} lines) in "
                      f"**{_b['clusters']['deep']} areas**\n"
                      f"- Skim: **{_b['files']['skim']} files** ({_b['lines_skim']:,} lines)\n"
                      f"- Skip: **{_b['files']['skip']} files**\n"
                      f"- Rough reading time: **{appkit.fmt_minutes(_b['est_minutes'])}**\n"
                      f"- AI review size: **{_b['est_tokens']:,} tokens**, against "
                      f"{_b['baseline_tokens']:,} for reading everything"
                      + (f"\n- Moved to skim to fit the limit: {', '.join(_b['demoted'])}" if _b["demoted"] else "")),
                mo.md("**Improve accuracy**"),
                mo.md("<span class='pr-muted'>The quick search finds who uses each changed function by name. "
                      "The accurate search follows the code the way an editor does, so it confirms real uses and "
                      "drops lookalikes. It takes about 15 seconds per 100 functions; the riskiest go first. "
                      f"{_rf_text}</span>"),
                refine_count,
                refine_btn,
                mo.accordion({"Make these the default for everyone on this project": mo.md(
                    "Add this to the project's `pyproject.toml`:\n\n```toml\n[tool.pr-review]\n"
                    f"deep_score = {deep_slider.value}\nskip_score = {skip_slider.value}\n"
                    f"token_budget = {budget_pick.value or 0}\ncontext_callers = {callers_slider.value}\n```")}),
            ], gap=0.5),
        ], widths=[1, 1], gap=2, align="start"),
    ], gap=1)
    return (settings_view,)


@app.cell(hide_code=True)
def _(appkit, mo, review_repo, save_settings_btn, view):
    mo.stop(not save_settings_btn.value)
    appkit.save(view, review_repo)
    mo.callout("Settings saved with this review.", kind="success")
    return


@app.cell(hide_code=True)
def _(mo):
    help_view = mo.md(
        """
        **Words used on this page**

        - **Version / branch**: a named copy of the code. A pull request asks to merge one version
          (the one with the changes) into another (usually `main`).
        - **Area**: a group of changed files that belong together, because they sit in the same folder or
          use each other. Review one area at a time.
        - **Risk score (0 to 100)**: how much attention a file probably needs. It goes up for big changes,
          sensitive places (logins, payments, settings, databases), code that many other files use,
          changes that can break other code, and missing tests. Every point is explained on the file's page.
        - **Read closely / Skim / Safe to skip**: the advice for each file and area. You can move the lines
          between them in *Cost & settings*.
        - **Function**: a named piece of code that does one job. When its inputs change, the places that use it
          may need changing too.
        - **Import loop**: two files that each need the other. It makes code harder to change.

        **Good habits**

        1. Start with *Things to check*. The "Must check" items are the most likely real problems.
        2. Then open areas from the top of the list. Read the tests first: they say what the code should do.
        3. Flip **I've reviewed this file** as you go. It's remembered until the file changes again.
        4. Skipped files are still listed, so you can check that skipping them was right.

        **What this tool can't see.** It reads the code without running it, so connections made at run time
        (settings files, plugins, names built from text) are invisible. Use it to decide where to look, not as
        proof that something is correct.
        """
    )
    return (help_view,)


@app.cell(hide_code=True)
def _(area_view, areas_view, crumb_bar, file_view, mo):
    map_view = mo.vstack([crumb_bar, file_view or area_view or areas_view], gap=1)
    return (map_view,)


@app.cell(hide_code=True)
def _(appkit, checks_view, get_tab, help_view, map_view, mo, set_tab, settings_view, structure_view, view):
    _n = (sum(1 for g in view["gaps"] if g["severity"] != "info")
          + sum(1 for f, _ in appkit.all_findings(view) if f["severity"] != "info"))
    _names = {"Map": "Map", "Checks": f"Things to check ({_n})", "Structure": "What changed in the structure",
              "Settings": "Cost & settings", "Help": "Help"}
    _lookup = {v: k for k, v in _names.items()}
    tabs = mo.ui.tabs({_names["Map"]: map_view, _names["Checks"]: checks_view, _names["Structure"]: structure_view,
                       _names["Settings"]: settings_view, _names["Help"]: help_view},
                      value=_names.get(get_tab(), _names["Map"]),
                      on_change=lambda v: set_tab(_lookup.get(v, "Map")))
    tabs
    return (tabs,)


if __name__ == "__main__":
    app.run()
