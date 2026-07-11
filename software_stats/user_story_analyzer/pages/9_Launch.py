"""
Launch page — Launch a new experiment from the UI.

Three tabs:
  1. Configuration — form to pick parameters + live JSON preview
  2. Run & Log    — pipeline start, live KPIs per phase, log tail
  3. Active runs  — experiments currently running or started but not completed
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import streamlit as st

from core.ui import inject_css, page_hero, badge, PLOTLY_COLORS
from core.launcher import (
    VALID_STRATEGIES,
    build_config,
    write_config,
    launch_pipeline,
    save_runtime,
    load_runtime,
    is_pid_alive,
    stop_pipeline,
    read_live_state,
    list_active_runs,
)
from core.queue_runner import (
    load_queue,
    add_to_queue,
    remove_from_queue,
    move_in_queue,
    clear_completed,
    is_worker_running,
    load_worker_info,
    start_worker,
    stop_worker,
    detect_active_external_run,
    stop_running_item,
)

inject_css()
st.set_page_config(page_title="Launch", page_icon="🚀", layout="wide")

page_hero(
    "🚀 Launch experiment",
    "Configure the parameters, launch the pipeline and follow the results live.",
)


# Auto-detect the default paths by locating run_pipeline.py, so the page
# works regardless of where the project is installed.

def _find_pipeline_root() -> Path:
    """Search for the pipeline/ folder walking up from the cwd.

    run_pipeline.py lives in pipeline/orchestration/, so it looks for
    `orchestration/run_pipeline.py` (its parent is the pipeline root) and
    also scans first-level subfolders to cover custom layouts.
    """
    here = Path.cwd().resolve()

    def _is_pipeline_root(p: Path) -> bool:
        return (p / "orchestration" / "run_pipeline.py").exists()

    # Walk up looking for orchestration/run_pipeline.py.
    for parent in [here, *here.parents]:
        if _is_pipeline_root(parent):
            return parent
        # Then explore first-level subfolders.
        try:
            for sub in parent.iterdir():
                if sub.is_dir() and _is_pipeline_root(sub):
                    return sub
        except (PermissionError, OSError):
            continue

    # Fallback: two levels up.
    return here.parent.parent


_DETECTED_PIPELINE_ROOT = _find_pipeline_root()
# experiments/ is a sibling of pipeline/, at _DETECTED_PIPELINE_ROOT.parent.
_DETECTED_EXPERIMENTS_ROOT = _DETECTED_PIPELINE_ROOT.parent / "experiments"


# Sidebar: paths.

with st.sidebar:
    st.header("📁 Paths")

    pipeline_found = (_DETECTED_PIPELINE_ROOT / "orchestration" / "run_pipeline.py").exists()
    if pipeline_found:
        st.caption(f"✅ Auto-detected: `{_DETECTED_PIPELINE_ROOT.name}/`")
    else:
        st.caption("⚠️  Auto-detect failed — check the paths below.")

    pipeline_cwd_str = st.text_input(
        "pipeline folder",
        value=str(_DETECTED_PIPELINE_ROOT),
        help="Root folder of the pipeline/ package (contains steps/, "
             "orchestration/, config/, utils/, etc.).",
        key="launch_pipeline_cwd",
    )
    experiments_root_str = st.text_input(
        "experiments/ folder",
        value=str(_DETECTED_EXPERIMENTS_ROOT),
        help="Where experiment configs and outputs get saved.",
        key="launch_experiments_root",
    )

pipeline_cwd = Path(pipeline_cwd_str)
experiments_root = Path(experiments_root_str)

# Path validation: warn only, do not block.
if not pipeline_cwd.exists():
    st.sidebar.warning(f"⚠️  Pipeline folder does not exist: `{pipeline_cwd}`")
elif not (pipeline_cwd / "orchestration" / "run_pipeline.py").exists():
    st.sidebar.warning(f"⚠️  `orchestration/run_pipeline.py` not found in `{pipeline_cwd}`")


# Session state.

# The experiment just launched, pre-selected in the "Run & Log" tab.
if "launch_active_exp" not in st.session_state:
    st.session_state.launch_active_exp = None


# Read run_mode from an experiment's config.json, used to distinguish
# testing from full_coverage. Defensive: a missing or malformed file
# returns "testing".

def _read_run_mode(experiments_root: Path, exp_id: str) -> str:
    """Read the top-level 'run_mode' field from the experiment's config.json.
    Default: 'testing' (backwards compat: legacy configs without the field).
    """
    cfg_path = experiments_root / exp_id / "config.json"
    if not cfg_path.exists():
        return "testing"
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            return str(json.load(f).get("run_mode", "testing"))
    except (json.JSONDecodeError, OSError):
        return "testing"


def _run_mode_badge(mode: str) -> str:
    """Return a short emoji+label for a run_mode."""
    if mode == "full_coverage":
        return "🚀 Full"
    return "🧪 Test"


# Main tabs.

tab_config, tab_run, tab_active, tab_queue = st.tabs([
    "📝 Configuration",
    "▶️  Run & Log",
    "📡 Active runs",
    "📋 Queue",
])


# Tab 1: configuration.

with tab_config:
    # Run-mode selector. Two mutually exclusive modes: Testing (default)
    # keeps all caps configurable with yearly windows and historical
    # max_pages, for quick controlled runs; Full Coverage removes the caps,
    # uses quarterly windows and forces max_pages to the GitHub ceiling for
    # an exhaustive dataset. Fields disabled in Full Coverage stay visible
    # but greyed out with a tooltip.
    run_mode_label = st.radio(
        "🎛️  Run mode",
        options=[
            "🧪 Testing — configurable parameters, quick run",
            "🚀 Full Coverage — no caps, quarterly windows, exhaustive run",
        ],
        index=0,
        help=(
            "Testing: you choose the caps and time-window granularity. "
            "Good for trials or controlled datasets.\n\n"
            "Full Coverage: no caps on repos and user stories, quarterly "
            "windows (37 windows), max_pages at maximum. The pipeline only "
            "stops when GitHub queries run out of results. WARNING: can take "
            "many hours or days and burns through the rate limit. Only use "
            "for final datasets."
        ),
        key="cfg_run_mode_label",
        horizontal=False,
    )
    run_mode = "testing" if "Testing" in run_mode_label else "full_coverage"
    is_full = run_mode == "full_coverage"

    if is_full:
        st.warning(
            "⚠️  **Full Coverage mode active.** All caps are disabled "
            "automatically. Time windows are forced to **quarterly** "
            "(37 windows, max recall). All strategies use `max_pages=10` "
            "(GitHub ceiling: 1000 results/query). The run can take hours "
            "or days: launch it only when you are sure."
        )

    st.divider()

    col_form, col_preview = st.columns([3, 2], gap="large")

    with col_form:
        st.subheader("Identity")
        exp_id = st.text_input(
            "Experiment ID",
            value=f"exp_{int(time.time())}",
            help="Will be the folder name in experiments/. No spaces.",
            key="cfg_exp_id",
        )
        description = st.text_area(
            "Description",
            value="",
            height=68,
            help="Free-form note to remind you what this experiment tests.",
            key="cfg_description",
        )

        st.divider()
        st.subheader("Languages")
        all_langs = [
            "Java", "Python", "JavaScript", "TypeScript",
            "Go", "Rust", "C++", "C#", "Ruby", "PHP",
            "Kotlin", "Swift",
        ]
        languages = st.multiselect(
            "Allowed languages",
            options=all_langs,
            default=["Java", "Python"],
            help="Only repos that satisfy the language filter below will be accepted.",
            key="cfg_languages",
        )

        language_mode_label = st.radio(
            "Language filter mode",
            options=["🎯 Primary language only", "📊 Presence above threshold"],
            index=0,
            help=(
                "Primary only: legacy behaviour, accepts repos whose GitHub primary "
                "language is one of the chosen ones.\n\n"
                "By percentage: also accepts polyglot repos where the chosen languages "
                "are present above a threshold (more recall, more noise)."
            ),
            key="cfg_language_mode_label",
        )
        language_mode = (
            "primary_only" if "Primary" in language_mode_label else "any_above_threshold"
        )

        if language_mode == "any_above_threshold":
            min_lang_pct = st.slider(
                "Minimum %",
                min_value=5, max_value=50, value=15, step=1,
                help="Minimum percentage of repo bytes in the language. "
                     "15-25% = reasonable. <10% = very permissive.",
                key="cfg_min_lang_pct",
            )
        else:
            min_lang_pct = 15.0  # Ignored in primary_only.

        st.divider()
        st.subheader("Active strategies")
        st.caption(
            "Toggle ON = strategy executed. "
            "All active = max recall. CONNEXTRA only = complete user stories only."
        )
        strat_cols = st.columns(5)
        strategies_state: dict[str, bool] = {}
        descriptions = {
            "LABEL": "Issues with `user story` label or variants",
            "TEMPLATE": "Repos with Connextra/Gherkin templates",
            "TAG": "Tag [US] / US-N: in the title",
            "MENTION": "Text mention of `user story`",
            "CONNEXTRA": "Pattern `as a / I want / so that`",
        }
        for i, strat in enumerate(["LABEL", "TEMPLATE", "TAG", "MENTION", "CONNEXTRA"]):
            with strat_cols[i]:
                strategies_state[strat] = st.toggle(
                    strat, value=True, help=descriptions[strat],
                    key=f"cfg_strat_{strat}",
                )
        active_strategies = [s for s, on in strategies_state.items() if on]

        st.divider()
        st.subheader("Repository filters")
        c1, c2, c3 = st.columns(3)
        with c1:
            min_stars = st.number_input(
                "⭐ Min stars", min_value=0, max_value=10000, value=2,
                key="cfg_min_stelle",
            )
        with c2:
            # Shown as "no cap" and disabled in full coverage; the real
            # value is forced to None in build_config.
            max_repo = st.number_input(
                "📦 Max total repos", min_value=1, max_value=10000, value=100,
                disabled=is_full,
                help=("Forced to NO CAP in Full Coverage mode: phase 1 stops only "
                      "when GitHub queries run out of results."
                      if is_full else "Global cap: phase 1 stops once this number is reached."),
                key="cfg_max_repo",
            )
            if is_full:
                st.caption("🔒 Forced to **NO CAP**")
        with c3:
            max_per_strategy = st.number_input(
                "📊 Max per strategy", min_value=1, max_value=1000, value=20,
                disabled=is_full,
                help=("Forced to NO CAP in Full Coverage mode: each strategy "
                      "grinds until its queries are exhausted."
                      if is_full else "How many new repos to accept per strategy."),
                key="cfg_max_per_strat",
            )
            if is_full:
                st.caption("🔒 Forced to **NO CAP**")

        # Issue cap per repo (step 2).
        st.markdown("##### 🎯 User-story cap per repo (Step 2)")
        st.caption(
            "Cap on the number of user stories extracted from EACH repository. "
            "Filters only user-story issues (not all of the repo's issues). "
            "Check the box for no cap: if a repo has 500 user stories, all 500 "
            "will be taken."
        )
        c_iss1, c_iss2 = st.columns([1, 2])
        with c_iss1:
            # Forced on and disabled in full coverage.
            download_all_issues = st.checkbox(
                "📥 Take all (no cap)",
                value=True if is_full else False,
                disabled=is_full,
                help=(
                    "Forced ON in Full Coverage mode: all user stories found by "
                    "the active strategies are kept."
                    if is_full else
                    "If active, no cap on the number of user stories per repo: "
                    "all those the strategies find are kept. "
                    "Default OFF → cap at 15 user stories per repo."
                ),
                key="cfg_download_all_issues",
            )
            if is_full:
                st.caption("🔒 Forced to **NO CAP**")
        with c_iss2:
            max_issue_per_repo_input = st.number_input(
                "Max user stories per repo",
                min_value=1, max_value=10000, value=15, step=1,
                disabled=download_all_issues or is_full,
                help="Cap on the number of user stories extracted per repo. "
                     "Disabled if 'Take all' is active.",
                key="cfg_max_issue_per_repo",
            )
        max_issue_per_repo = None if download_all_issues else int(max_issue_per_repo_input)

        # Max pages per strategy (step 2). In testing the user sets a single
        # value applied to every strategy; in full coverage it is forced to
        # the GitHub ceiling of 10 and disabled.
        st.markdown("##### 📄 Max pages per strategy (Step 2)")
        st.caption(
            "How many GitHub pages to grind for each active strategy in phase 2. "
            "Each page = up to 100 results. More pages = more coverage but more "
            "API calls. GitHub ceiling: 10 pages (1000 results/query)."
        )
        # The widget key depends on is_full: separate keys give each mode
        # its own session_state slot, so switching modes shows the right
        # default instead of a stale value.
        max_pages_per_strategy = st.number_input(
            "Pages per strategy",
            min_value=1, max_value=10,
            value=10 if is_full else 3,
            step=1,
            disabled=is_full,
            help=(
                "Forced to 10 in Full Coverage mode (GitHub ceiling, max recall)."
                if is_full else
                "Default 3 = quick run. Increase it for more coverage "
                "(max 10, GitHub ceiling). Overrides the max_pages of ALL "
                "active strategies with this single value."
            ),
            key="cfg_max_pages_per_strategy_full" if is_full else "cfg_max_pages_per_strategy",
        )
        if is_full:
            st.caption("🔒 Forced to **10** (GitHub ceiling)")

        st.divider()
        # Time-window granularity (phase 1).
        st.subheader("🕐 Time-window granularity (Step 1)")
        st.caption(
            "GitHub Search returns **at most 1000 results per query**. "
            "To work around this limit, phase 1 splits each search into "
            "time windows. More windows = more coverage but more API calls.\n\n"
            "• **Yearly** (10 windows) — historical default, quick run\n"
            "• **Semester** (19 windows) — intermediate coverage\n"
            "• **Quarterly** (37 windows) — max coverage\n"
            "• **Monthly** (109 windows) — fine coverage, slow run\n"
            "• **Custom** — you choose the exact number of days per window"
        )
        time_windows_custom_days = 30  # Default, overridden only for "custom".
        if is_full:
            st.info("🔒 In Full Coverage mode granularity is **forced to Quarterly** "
                    "to maximize coverage.")
            granularity_label = "📅 Quarterly (37 windows)"
        else:
            granularity_label = st.radio(
                "Pick the granularity",
                options=[
                    "📆 Yearly (10 windows) — historical default",
                    "📅 Semester (19 windows)",
                    "📅 Quarterly (37 windows) — max coverage",
                    "🗓️ Monthly (109 windows)",
                    "⚙️ Custom (choose the days)",
                ],
                index=0,
                key="cfg_tw_granularity_label",
                horizontal=False,
            )
            if "Custom" in granularity_label:
                time_windows_custom_days = int(st.number_input(
                    "Days per window",
                    min_value=1, max_value=365, value=30, step=1,
                    help="Each window covers this many days. Fewer days = more "
                         "windows = more coverage but a longer run.",
                    key="cfg_tw_custom_days",
                ))
        # Map the label to the value expected by build_config.
        if "Yearly" in granularity_label:
            time_windows_granularity = "yearly"
        elif "Semester" in granularity_label:
            time_windows_granularity = "semester"
        elif "Quarterly" in granularity_label:
            time_windows_granularity = "quarter"
        elif "Monthly" in granularity_label:
            time_windows_granularity = "monthly"
        else:
            time_windows_granularity = "custom"

        st.divider()
        st.subheader("🎯 PR-level filter (Step 5 — Build Dataset)")
        st.caption(
            "**Purpose.** The user stories in the dataset are linked to a PR. "
            "Sometimes the linked PR modifies ONLY non-code files (e.g. `README.md`, "
            "CI workflows `.yml`, docs). If your goal is a "
            "**user story → code** dataset, those PRs are noise to exclude.\n\n"
            "**What it does.** This filter discards from the final dataset the "
            "user stories whose linked PR does not modify enough files of the "
            "target language (Java: `.java/.xml/.gradle/.properties/.kts`; "
            "Python: `.py/.cfg/.toml/.ini`). It is applied AFTER extraction "
            "and AI validation, before writing the final dataset.\n\n"
            "**Why it's off by default.** To avoid losing borderline PRs. "
            "Enable it only if you need a code-only dataset."
        )
        c4, c5 = st.columns(2)
        with c4:
            min_language_files = st.number_input(
                "Min language files in the PR",
                min_value=0, max_value=50, value=0,
                help="0 = filter off. 1 = at least one Java/Python file touched. "
                     "Values >1 are stricter.",
                key="cfg_min_files_language",
            )
        with c5:
            min_language_fraction_pct = st.slider(
                "Min % language files",
                min_value=0, max_value=100, value=0, step=5,
                help="0% = off. 20-30% = typical filter (the PR must touch at "
                     "least 20-30% of language target files over the total).",
                key="cfg_min_language_fraction_pct",
            )
        min_language_fraction = min_language_fraction_pct / 100.0

        st.divider()
        st.subheader("📅 Time filter (Step 2)")
        st.caption(
            "Filters issues by **closing date** (`closed_at`):\n\n"
            "• **Disabled** — no time filter\n"
            "• **From a date** — only issues closed on/after the date (`closed_at >= start`)\n"
            "• **Interval** — only issues closed between two dates (`start <= closed_at <= end`)\n\n"
            "Applied both server-side (GitHub query) and client-side."
        )
        from datetime import date as _date
        cutoff_mode = st.radio(
            "Cutoff mode",
            options=["Disabled", "From a date (≥)", "Interval (between two dates)"],
            index=0,
            horizontal=True,
            key="cfg_cutoff_mode",
        )
        cutoff_date_closed = None
        cutoff_date_closed_end = None
        if cutoff_mode != "Disabled":
            c6, c7 = st.columns(2)
            with c6:
                start_obj = st.date_input(
                    "Start (closed_at ≥)",
                    value=_date(2023, 1, 1),
                    key="cfg_cutoff_start",
                )
            cutoff_date_closed = start_obj.strftime("%Y-%m-%d")
            if cutoff_mode.startswith("Interval"):
                with c7:
                    end_obj = st.date_input(
                        "End (closed_at ≤)",
                        value=_date(2025, 12, 31),
                        key="cfg_cutoff_end",
                    )
                cutoff_date_closed_end = end_obj.strftime("%Y-%m-%d")

        st.divider()
        with st.expander("⚙️  Exclusions (advanced)"):
            available_exps = []
            if experiments_root.exists():
                available_exps = sorted([
                    d.name for d in experiments_root.iterdir() if d.is_dir()
                ])

            exclude_from = st.multiselect(
                "Exclude repos already taken by:",
                options=available_exps, default=[],
                help="Repos already accepted in these experiments will be skipped in phase 1.",
                key="cfg_exclude_from",
            )
            exclude_issues_from = st.multiselect(
                "Exclude issues already analyzed by:",
                options=available_exps, default=[],
                help="Issues already extracted in Phase 2 of these experiments will be skipped. "
                     "Repos are NOT touched: Phase 1 runs normally, only Phase 2 "
                     "(issue extraction) is filtered on the repo#number key. "
                     "Useful for incremental runs: launch a new run and analyze only "
                     "previously unseen issues.",
                key="cfg_exclude_issues_from",
            )
            exclude_rejected_from = st.multiselect(
                "Exclude issues already rejected by:",
                options=available_exps, default=[],
                help="Issues rejected by the AI in these experiments will be skipped.",
                key="cfg_exclude_rejected_from",
            )
            exclude_validated_from = st.multiselect(
                "Exclude issues already validated by:",
                options=available_exps, default=[],
                help="Already-validated issues will be skipped (useful for complementary datasets).",
                key="cfg_exclude_validated_from",
            )

    # Build the config in memory. This is the single place where the mode
    # affects the real values: Full Coverage forces caps to None, quarterly
    # granularity and max_pages_override to 10, while Testing uses the value
    # the user chose and applies it to every active strategy.
    if is_full:
        eff_max_repo = None
        eff_max_per_strategy = None
        eff_max_issue_per_repo = None
        eff_time_windows_granularity = "quarter"
        eff_max_pages_override: int | None = 10
    else:
        eff_max_repo = int(max_repo)
        eff_max_per_strategy = int(max_per_strategy)
        eff_max_issue_per_repo = max_issue_per_repo
        eff_time_windows_granularity = time_windows_granularity
        eff_max_pages_override = int(max_pages_per_strategy)

    try:
        config_preview = build_config(
            experiment_id=exp_id,
            description=description,
            max_repo=eff_max_repo,
            max_per_strategy=eff_max_per_strategy,
            min_stars=int(min_stars),
            languages=languages or ["Java", "Python"],
            language_mode=language_mode,
            min_lang_pct=float(min_lang_pct),
            strategies=active_strategies or list(VALID_STRATEGIES),
            min_language_files=int(min_language_files),
            min_language_fraction=float(min_language_fraction),
            cutoff_date_closed=cutoff_date_closed,
            cutoff_date_closed_end=cutoff_date_closed_end,
            max_issue_per_repo=eff_max_issue_per_repo,
            exclude_from=exclude_from,
            exclude_issues_from=exclude_issues_from,
            exclude_rejected_from=exclude_rejected_from,
            exclude_validated_from=exclude_validated_from,
            run_mode=run_mode,
            time_windows_granularity=eff_time_windows_granularity,
            time_windows_custom_days=time_windows_custom_days,
            max_pages_override=eff_max_pages_override,
        )
        config_error = None
    except ValueError as e:
        config_preview = None
        config_error = str(e)

    # Right-side preview.
    with col_preview:
        st.subheader("`config.json` preview")
        if config_error:
            st.error(f"❌ Invalid config: {config_error}")
        else:
            st.json(config_preview, expanded=False)

        warnings = []
        if not active_strategies:
            warnings.append("⚠️  No strategy active! At least one is required.")
        if not languages:
            warnings.append("⚠️  No language selected.")
        if exp_id and (" " in exp_id or "/" in exp_id):
            warnings.append("⚠️  ID with invalid characters (no spaces, no /).")
        # The cap-saturation warning only applies in testing.
        if not is_full and max_per_strategy * len(active_strategies) < max_repo and active_strategies:
            warnings.append(
                f"ℹ️  max_per_strategy × n_strategies ({max_per_strategy}×"
                f"{len(active_strategies)}={max_per_strategy*len(active_strategies)}) "
                f"< max_repo_totali ({max_repo}). You probably won't saturate the cap."
            )
        for w in warnings:
            if w.startswith("⚠️"):
                st.warning(w)
            else:
                st.info(w)

        st.divider()

        target_path = experiments_root / exp_id if exp_id else None
        already_exists = target_path.exists() if target_path else False

        force = False
        if already_exists:
            st.warning(f"⚠️  The folder `{exp_id}` already exists.")
            force = st.checkbox("Overwrite", key="cfg_force")

        save_only_btn = st.button(
            "💾 Just create config (no launch)",
            use_container_width=True,
            disabled=config_error is not None or not active_strategies,
            key="cfg_save_only",
        )

        # Heads-up when other full coverage runs are already queued, since
        # each can take hours or days; shown before the button, not blocking.
        if is_full:
            _queue_state = load_queue(experiments_root)
            _full_in_queue = []
            for _item in _queue_state.items:
                if _item.status in ("pending", "running"):
                    if _read_run_mode(experiments_root, _item.experiment_id) == "full_coverage":
                        _full_in_queue.append((_item.experiment_id, _item.status))
            if _full_in_queue:
                _lines = "\n".join(
                    f"  • `{eid}` ({status})" for eid, status in _full_in_queue
                )
                st.warning(
                    f"⚠️  You are about to queue a **🚀 Full Coverage** but "
                    f"there are already **{len(_full_in_queue)}** in the queue:\n\n{_lines}\n\n"
                    f"Each Full Coverage can take hours or days. Adding this one, "
                    f"the PC could stay busy for a long time. Proceed only if you "
                    f"are aware of this."
                )

        save_and_queue_btn = st.button(
            "➕ Create & Add to queue",
            use_container_width=True,
            disabled=config_error is not None or not active_strategies,
            key="cfg_save_queue",
            help="Create the config and add the experiment to the queue. It launches "
                 "automatically when its turn comes (see Queue tab).",
        )

        # Warn if a run is already active, so the user picks "Add to queue"
        # instead of "Launch now" and avoids accidental parallel runs.
        _existing_run = detect_active_external_run(experiments_root)
        if _existing_run:
            st.warning(
                f"⚠️  A pipeline is already active: **{_existing_run['experiment_id']}** "
                f"(PID {_existing_run['pid']}). "
                f"Launching another in parallel can saturate GitHub/Ollama. "
                f"Recommended: use **➕ Add to queue** (the worker will wait)."
            )

        save_and_launch_btn = st.button(
            "🚀 Create & Launch now",
            type="primary",
            use_container_width=True,
            disabled=config_error is not None or not active_strategies,
            key="cfg_save_launch",
        )

        # Actions.
        if save_only_btn or save_and_launch_btn or save_and_queue_btn:
            try:
                target = write_config(config_preview, experiments_root, force=force)
                st.success(f"✅ Config written: `{target}/config.json`")

                if save_and_launch_btn:
                    if not (pipeline_cwd / "orchestration" / "run_pipeline.py").exists():
                        st.error(
                            f"❌ `orchestration/run_pipeline.py` not found in `{pipeline_cwd}`. "
                            "Fix the path in the sidebar and try again."
                        )
                    else:
                        info = launch_pipeline(
                            exp_id, pipeline_cwd,
                            experiments_root=experiments_root,
                        )
                        save_runtime(target, info)
                        st.session_state.launch_active_exp = exp_id
                        st.success(
                            f"🚀 Pipeline launched! PID {info.pid}. "
                            "Go to the **Run & Log** tab to follow the progress."
                        )

                if save_and_queue_btn:
                    ok, msg = add_to_queue(experiments_root, exp_id)
                    if ok:
                        st.success(f"➕ {msg}. Go to the **Queue** tab to manage it.")
                        # Suggest starting the worker if it is not running.
                        if not is_worker_running(experiments_root):
                            st.info(
                                "ℹ️  The queue worker is not running. "
                                "Go to the **Queue** tab and click **▶️ Start worker** "
                                "to process the queued experiments."
                            )
                    else:
                        st.error(f"❌ {msg}")
            except FileExistsError as e:
                st.error(f"❌ {e}. Tick 'Overwrite' if you want to redo it.")
            except Exception as e:
                st.error(f"❌ Error: {type(e).__name__}: {e}")


# Tab 2: run and log (live).

with tab_run:
    # Selector for the experiment to monitor.
    available = []
    if experiments_root.exists():
        available = sorted(
            [d.name for d in experiments_root.iterdir() if d.is_dir()],
            reverse=True,
        )

    if not available:
        st.info("No experiment found. Create the first one in the **Configuration** tab.")
        st.stop()

    default_idx = 0
    if st.session_state.launch_active_exp in available:
        default_idx = available.index(st.session_state.launch_active_exp)

    sel_exp = st.selectbox(
        "Experiment to monitor",
        options=available,
        index=default_idx,
        key="run_sel_exp",
    )

    auto_refresh = st.toggle(
        "🔄 Auto-refresh every 5s",
        value=True,
        help="Disable to freeze the view (e.g. to copy a log).",
        key="run_autorefresh",
    )

    exp_dir = experiments_root / sel_exp

    # Read-only fragment that refreshes on its own.
    @st.fragment(run_every=5 if auto_refresh else None)
    def render_live_panel():
        state = read_live_state(exp_dir, log_lines=80)

        # Status header.
        col_st1, col_st2, col_st3, col_st4 = st.columns(4)
        with col_st1:
            if state.pipeline_done:
                st.markdown(badge("✅ Completed", "green"), unsafe_allow_html=True)
            elif state.is_running:
                st.markdown(badge("⏳ Running", "violet"), unsafe_allow_html=True)
            elif state.pid:
                st.markdown(badge("⏸️  Interrupted", "amber"), unsafe_allow_html=True)
            else:
                st.markdown(badge("· Not started", "muted"), unsafe_allow_html=True)
        with col_st2:
            if state.pid:
                st.caption(f"PID: `{state.pid}`")
        with col_st3:
            if state.elapsed_sec > 0:
                m, s = divmod(int(state.elapsed_sec), 60)
                st.caption(f"⏱️  {m}m {s}s")
        with col_st4:
            if state.is_running:
                if st.button("🛑 Stop", key=f"stop_{sel_exp}"):
                    rt = load_runtime(exp_dir)
                    if rt and stop_pipeline(rt):
                        st.success("Stop signal sent.")
                    else:
                        st.error("Stop failed or process already dead.")

        st.divider()

        # Phase progress.
        st.subheader("Phase progress")
        phase_cols = st.columns(5)
        phase_titles = {
            1: "1 · Discovery", 2: "2 · Issues", 3: "3 · PR linking",
            4: "4 · AI Validation", 5: "5 · Dataset",
        }
        for n in range(1, 6):
            with phase_cols[n - 1]:
                done = state.phases_done.get(n, False)
                metrics = state.phase_metrics.get(n, {})
                icon = "✅" if done else ("⏳" if metrics else "⏸️")
                st.markdown(f"**{icon} {phase_titles[n]}**")
                if metrics:
                    for k, v in metrics.items():
                        if isinstance(v, float):
                            v = f"{v:.1f}"
                        st.caption(f"{k}: **{v}**")
                else:
                    st.caption("_waiting..._")

        st.divider()

        # Log tail.
        st.subheader("Log (last lines)")
        if state.log_tail:
            # Shown in a code block that auto-scrolls to the bottom.
            log_text = "".join(state.log_tail)
            st.code(log_text, language="log")
        else:
            st.caption("_Log not yet available._")

    render_live_panel()


# Tab 3: active runs.

with tab_active:
    st.subheader("Experiments running or interrupted")
    st.caption(
        "Experiments with a registered PID that are still alive, "
        "or that never produced `_done.txt`."
    )

    active = list_active_runs(experiments_root)
    if not active:
        st.info("No active or interrupted run found.")
    else:
        for run in active:
            with st.container(border=True):
                col_a, col_b, col_c, col_d = st.columns([3, 2, 2, 2])
                with col_a:
                    st.markdown(f"**{run['experiment_id']}**")
                    st.caption(f"PID `{run['pid']}` · started `{run['started_at'][:19]}`")
                with col_b:
                    if run["is_alive"]:
                        st.markdown(badge("⏳ Running", "violet"), unsafe_allow_html=True)
                    elif run["is_done"]:
                        st.markdown(badge("✅ Completed", "green"), unsafe_allow_html=True)
                    else:
                        st.markdown(badge("⏸️  Interrupted", "amber"), unsafe_allow_html=True)
                with col_c:
                    if st.button("👁️  Monitor", key=f"go_{run['experiment_id']}"):
                        st.session_state.launch_active_exp = run["experiment_id"]
                        st.info("Go to the **Run & Log** tab for the live detail.")
                with col_d:
                    if run["is_alive"]:
                        if st.button("🛑 Stop", key=f"kill_{run['experiment_id']}"):
                            rt = load_runtime(Path(run["path"]))
                            if rt and stop_pipeline(rt):
                                st.success("Stop signal sent.")
                                st.rerun()
                            else:
                                st.error("Stop failed.")

# Tab 4: queue.

with tab_queue:
    st.subheader("📋 Experiment queue")
    st.caption(
        "Add experiments from the **Configuration** tab using the "
        "**➕ Create & Add to queue** button, then start the worker below: "
        "it will run experiments one at a time in order. "
        "The worker is a separate subprocess and survives Streamlit being closed."
    )

    # Worker status and controls.
    queue_state = load_queue(experiments_root)
    worker_running = is_worker_running(experiments_root)
    worker_info = load_worker_info(experiments_root)
    # Detect whether a manually-launched run is currently active.
    external_run = detect_active_external_run(experiments_root)

    col_status, col_ctrl1, col_ctrl2, col_ctrl3 = st.columns([2, 1, 1, 1])

    with col_status:
        if worker_running and worker_info:
            if external_run:
                # Worker active but waiting for a manual run to finish.
                st.markdown(
                    badge(f"⏸️  Worker waiting (PID {worker_info.pid})", "orange"),
                    unsafe_allow_html=True,
                )
                st.caption(
                    f"Manual run active: `{external_run['experiment_id']}`. "
                    f"The worker will start as soon as this one finishes."
                )
            else:
                st.markdown(badge(f"🟢 Worker active (PID {worker_info.pid})", "green"),
                            unsafe_allow_html=True)
                try:
                    from datetime import datetime as _dt
                    t0 = _dt.fromisoformat(worker_info.started_at)
                    elapsed = (_dt.now() - t0).total_seconds() / 60
                    st.caption(f"Started {elapsed:.1f} min ago")
                except Exception:
                    pass
        else:
            st.markdown(badge("⚫ Worker not active", "gray"), unsafe_allow_html=True)
            if external_run:
                st.caption(
                    f"⚠️  There is a manual run active: `{external_run['experiment_id']}`"
                )

    with col_ctrl1:
        if not worker_running:
            if st.button("▶️ Start worker", type="primary",
                         use_container_width=True, key="q_start_worker"):
                # queue_worker.py lives in pipeline/orchestration/.
                qw_target = pipeline_cwd / "orchestration" / "queue_worker.py"
                if not qw_target.exists():
                    st.error(
                        f"❌ `orchestration/queue_worker.py` not found in `{pipeline_cwd}`. "
                        "Check the structure of the pipeline/ package."
                    )
                else:
                    ok, msg, _ = start_worker(experiments_root, pipeline_cwd)
                    if ok:
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
        else:
            if st.button("⏸️ Stop worker", use_container_width=True,
                         key="q_stop_worker",
                         help="Stop the worker. The current experiment keeps running "
                              "until completion, but no further one will be started."):
                ok, msg = stop_worker(experiments_root)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

    with col_ctrl2:
        if st.button("🧹 Clear completed", use_container_width=True,
                     key="q_clear_completed",
                     help="Remove completed/failed experiments from the queue."):
            n = clear_completed(experiments_root)
            st.success(f"Removed {n} items")
            st.rerun()

    with col_ctrl3:
        auto_refresh_q = st.toggle("🔄 Auto-refresh", value=True, key="q_autorefresh")

    st.divider()

    # Queue list.
    if not queue_state.items:
        st.info("📭 Empty queue. Add experiments from the **Configuration** tab.")
    else:
        n_pending  = sum(1 for i in queue_state.items if i.status == "pending")
        n_running  = sum(1 for i in queue_state.items if i.status == "running")
        n_done     = sum(1 for i in queue_state.items if i.status == "completed")
        n_failed   = sum(1 for i in queue_state.items if i.status == "failed")

        kc1, kc2, kc3, kc4 = st.columns(4)
        kc1.metric("⏳ Pending", n_pending)
        kc2.metric("🟢 Running", n_running)
        kc3.metric("✅ Completed", n_done)
        kc4.metric("❌ Failed", n_failed)

        # Banner counting the full coverage runs still pending or running,
        # so the remaining operational load is visible at the top of the list.
        _full_active = []
        _test_active = []
        for _i in queue_state.items:
            if _i.status in ("pending", "running"):
                _m = _read_run_mode(experiments_root, _i.experiment_id)
                if _m == "full_coverage":
                    _full_active.append(_i.experiment_id)
                else:
                    _test_active.append(_i.experiment_id)

        if _full_active:
            st.warning(
                f"🚀 The queue contains **{len(_full_active)} Full Coverage runs** "
                f"({len(_test_active)} Testing). Each Full Coverage can take "
                f"hours or days and burns through the GitHub rate limit. "
                f"Consider whether queueing more Full Coverage runs before completion "
                f"is really what you want."
            )

        st.markdown("##### Items")

        for idx, item in enumerate(queue_state.items):
            status_emoji = {
                "pending":   "⏳",
                "running":   "🟢",
                "completed": "✅",
                "failed":    "❌",
                "skipped":   "⏭️",
            }.get(item.status, "❓")

            # 8-column layout: num, ID, mode, status, duration and the
            # move/delete controls.
            cols = st.columns([0.5, 2.5, 0.9, 1.6, 1, 0.7, 0.7, 0.7])

            with cols[0]:
                st.markdown(f"**{idx+1}.**")
            with cols[1]:
                st.markdown(f"`{item.experiment_id}`")
            with cols[2]:
                # run_mode badge from config.json.
                _mode = _read_run_mode(experiments_root, item.experiment_id)
                st.markdown(_run_mode_badge(_mode))
            with cols[3]:
                st.markdown(f"{status_emoji} {item.status}")
                if item.error:
                    st.caption(f"⚠️ {item.error[:80]}")
            with cols[4]:
                if item.started_at and item.finished_at:
                    try:
                        from datetime import datetime as _dt
                        t0 = _dt.fromisoformat(item.started_at)
                        t1 = _dt.fromisoformat(item.finished_at)
                        dur = (t1 - t0).total_seconds() / 60
                        st.caption(f"{dur:.1f} min")
                    except Exception:
                        pass
                elif item.started_at:
                    try:
                        from datetime import datetime as _dt
                        t0 = _dt.fromisoformat(item.started_at)
                        elapsed = (_dt.now() - t0).total_seconds() / 60
                        st.caption(f"{elapsed:.1f} min running")
                    except Exception:
                        pass

            # Controls, only for pending items.
            with cols[5]:
                if item.status == "pending":
                    if st.button("⬆️", key=f"q_up_{idx}",
                                 disabled=(idx == 0
                                           or queue_state.items[idx-1].status != "pending"),
                                 help="Move up"):
                        ok, _ = move_in_queue(experiments_root, idx, -1)
                        if ok:
                            st.rerun()
            with cols[6]:
                if item.status == "pending":
                    if st.button("⬇️", key=f"q_dn_{idx}",
                                 disabled=(idx == len(queue_state.items) - 1
                                           or queue_state.items[idx+1].status != "pending"),
                                 help="Move down"):
                        ok, _ = move_in_queue(experiments_root, idx, 1)
                        if ok:
                            st.rerun()
            with cols[7]:
                if item.status == "pending":
                    if st.button("🗑️", key=f"q_rm_{idx}", help="Remove from queue"):
                        ok, msg = remove_from_queue(experiments_root, idx)
                        if ok:
                            st.rerun()
                        else:
                            st.error(msg)
                elif item.status == "running":
                    # Stop button for the run currently in progress.
                    if st.button("🛑", key=f"q_stop_{idx}",
                                 help="Stop: terminate the run and move to the next"):
                        ok, msg = stop_running_item(experiments_root, item.experiment_id)
                        if ok:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

    st.divider()

    # Worker log.
    log_path = experiments_root / "_queue_worker.log"
    if log_path.exists():
        with st.expander("📜 Worker log (last 100 lines)", expanded=False):
            try:
                with log_path.open("r", encoding="utf-8") as f:
                    lines = f.readlines()
                tail = lines[-100:]
                st.code("".join(tail), language="log")
            except Exception as e:
                st.error(f"Cannot read log: {e}")

    # Light auto-refresh for this tab only.
    if auto_refresh_q and worker_running:
        time.sleep(3)
        st.rerun()