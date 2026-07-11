import json
import os
import sys
import time

# Make the pipeline package importable when run directly or via the
# orchestrator.
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from config.experiment_paths import paths

# GitHub client with adaptive per-bucket pacing.
from utils import github_pacing as gh

# Atomic JSON writes and the read-side companion for crash-resilient
# checkpoints.
from utils.atomic_io import dump_json_atomic
from utils.checkpoint import load_progress

# Force UTF-8 output (needed on Windows for non-ASCII characters).
sys.stdout.reconfigure(encoding='utf-8')

# GitHub GraphQL endpoint, used to fetch repo metadata in batches.
GRAPHQL_URL = "https://api.github.com/graphql"


# Configuration, loaded from the experiment's config.json; the values
# there override the defaults below, and missing fields fall back to them.

_exp_config = paths.load_config()
_dc = _exp_config.get("discovery_config", {})

MIN_STARS             = _dc.get("min_stars", 2)
# None means no cap (Full Coverage mode); the default is 20.
MAX_REPO_PER_STRATEGY = _dc.get("max_repos_per_strategy", 20)
# None means no cap (Full Coverage mode); the default is 100.
MAX_REPO_TOTAL        = _dc.get("max_total_repos", 100)
BATCH_SIZE             = _dc.get("batch_size", 80)  # Repos per GraphQL call.
SEARCH_DELAY_SEC       = _dc.get("search_delay_sec", 2.0)  # GitHub Search allows ~30 req/min.
ALLOWED_LANGUAGES      = set(_dc.get("languages", ["Java", "Python"]))

# Time-window granularity: "yearly" (default), "semester", "quarter",
# "monthly" or "custom". More windows work around GitHub's 1000-results
# limit at the cost of more API calls.
TIME_WINDOWS_GRANULARITY = _dc.get("time_windows_granularity", "yearly")
# Day-step for the "custom" granularity.
TIME_WINDOWS_CUSTOM_DAYS = _dc.get("time_windows_custom_days", 30)
if TIME_WINDOWS_GRANULARITY not in ("yearly", "semester", "quarter", "monthly", "custom"):
    print(f"⚠️  time_windows_granularity='{TIME_WINDOWS_GRANULARITY}' is invalid, "
          "falling back to 'yearly'")
    TIME_WINDOWS_GRANULARITY = "yearly"
# The custom day-step must be an int >= 1; otherwise fall back to 30.
if TIME_WINDOWS_GRANULARITY == "custom":
    try:
        TIME_WINDOWS_CUSTOM_DAYS = int(TIME_WINDOWS_CUSTOM_DAYS)
        if TIME_WINDOWS_CUSTOM_DAYS < 1:
            raise ValueError
    except (TypeError, ValueError):
        print(f"⚠️  time_windows_custom_days='{TIME_WINDOWS_CUSTOM_DAYS}' is invalid "
              "(expected int >= 1), falling back to 30")
        TIME_WINDOWS_CUSTOM_DAYS = 30

# Helpers to treat None as "no cap": comparisons use +Inf so the condition
# is never true, and the remaining-slots computation uses a very large
# number so process_batch accepts the whole batch.
_INF_REPO = float("inf")
_NO_CAP_BUDGET = 10**9   # Effectively unlimited budget for process_batch.

def _cap_value(cap):
    """Return the numeric value to use in `>=` comparisons for a cap.
    If cap is None → +Inf (= never capped)."""
    return _INF_REPO if cap is None else cap

def _remaining_slots(cap, already_accepted):
    """Compute how many slots are left in a cap. If cap is None → large budget."""
    if cap is None:
        return _NO_CAP_BUDGET
    return cap - already_accepted

# Language filter mode: "primary_only" accepts a repo only if its primary
# language is allowed; "any_above_threshold" accepts it if an allowed
# language reaches MIN_LANG_PCT of the bytes. Default: "primary_only".
LANGUAGE_MODE = _dc.get("language_mode", "primary_only")
if LANGUAGE_MODE not in ("primary_only", "any_above_threshold"):
    print(f"⚠️  language_mode='{LANGUAGE_MODE}' is invalid, falling back to 'primary_only'")
    LANGUAGE_MODE = "primary_only"

# Byte-percentage threshold for "any_above_threshold" (default 15%).
MIN_LANG_PCT = float(_dc.get("min_lang_pct", 15.0))

# Optional diagnostic: when enabled, the issues discarded by the TAG regex
# are collected and written to a JSON file at the end of the run.
SAVE_TAG_DISCARDS_DEBUG = True
_discarded_tag_debug: list = []

# Time windows for the issue search. GitHub Search returns at most 1000
# results per query, so splitting the range into windows works around the
# limit. The windows are generated from TIME_WINDOWS_GRANULARITY and span
# from REPO_CREATED_MIN up to today, since the "created:" qualifier filters
# on the issue creation date.

# Repo creation-date range accepted by this run, applied on the repo
# createdAt in fetch_repo_info_batch.
REPO_CREATED_MIN = "2025-01-01"  # Inclusive.
REPO_CREATED_MAX = "2026-05-01"  # Exclusive.

def _generate_time_windows(granularity: str, custom_days: int = 30) -> list[str]:
    """Generate the list of time windows in GitHub Search format.

    The windows cover REPO_CREATED_MIN..today, clipped to that range and
    ordered from most recent to oldest (consistent with the old behavior).
    """
    from datetime import date, timedelta
    range_start = date.fromisoformat(REPO_CREATED_MIN)
    range_end = date.today()

    pairs: list[tuple] = []
    y_min, y_max = range_start.year, range_end.year

    if granularity == "yearly":
        for year in range(y_max, y_min - 1, -1):
            pairs.append((date(year, 1, 1), date(year, 12, 31)))

    elif granularity == "semester":
        # H2 first (more recent), then H1.
        for year in range(y_max, y_min - 1, -1):
            pairs.append((date(year, 7, 1), date(year, 12, 31)))
            pairs.append((date(year, 1, 1), date(year, 6, 30)))

    elif granularity == "quarter":
        for year in range(y_max, y_min - 1, -1):
            pairs.append((date(year, 10, 1), date(year, 12, 31)))  # Q4
            pairs.append((date(year, 7, 1), date(year, 9, 30)))    # Q3
            pairs.append((date(year, 4, 1), date(year, 6, 30)))    # Q2
            pairs.append((date(year, 1, 1), date(year, 3, 31)))    # Q1

    elif granularity == "monthly":
        for year in range(y_max, y_min - 1, -1):
            for month in range(12, 0, -1):
                start = date(year, month, 1)
                next_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
                pairs.append((start, next_first - timedelta(days=1)))

    elif granularity == "custom":
        # Walk the range in custom_days-day chunks, most recent first.
        step = timedelta(days=custom_days)
        cur, chunks = range_start, []
        while cur <= range_end:
            chunk_end = min(cur + step - timedelta(days=1), range_end)
            chunks.append((cur, chunk_end))
            cur = chunk_end + timedelta(days=1)
        pairs.extend(reversed(chunks))

    # Clip every window to the range and drop the empty ones.
    windows: list[str] = []
    for start, end in pairs:
        if start > range_end or end < range_start:
            continue
        s, e = max(start, range_start), min(end, range_end)
        windows.append(f"created:{s.isoformat()}..{e.isoformat()}")

    return windows

TIME_WINDOWS = _generate_time_windows(TIME_WINDOWS_GRANULARITY, TIME_WINDOWS_CUSTOM_DAYS)

# This run accepts only the repo creation range
# [REPO_CREATED_MIN, REPO_CREATED_MAX); see the constants above.


# Client-side regex filters, applied after the GitHub response. The
# patterns are centralized in discovery_strategies.py, shared with phase 2.

from utils.discovery_strategies import (
    REGEX_TAG_COMBINED,
    REGEX_CONNEXTRA_PARTIAL,
    REGEX_CONNEXTRA_COMPLETE,
)
from utils.stats_utils import median


# Each strategy is a dict with: name, queries, search_type ("issues" or
# "code"), regex_filter (compiled regex for TAG and CONNEXTRA), time_windows
# (whether to iterate over TIME_WINDOWS) and the optional delay_sec and
# max_pages overrides. Strategies do not exclude each other; deduplication
# happens downstream via the global seen_repos set.

# Labels excluded from the TAG strategy because they already belong to
# LABEL, which runs first; excluding them upstream avoids downloading
# issues that dedup would discard anyway.
EXCL_LABEL = (
    ' -label:"user story"'
    ' -label:"user-story"'
    ' -label:user_story'
    ' -label:userstory'
    ' -label:"user stories"'
    ' -label:"user-stories"'
    ' -label:story'
    ' -label:stories'
    ' -label:enhancement'
)


STRATEGY_LABEL = {
    "name": "LABEL",
    "queries": [
    'label:"user story" is:issue is:closed',
    'label:"user stories" is:issue is:closed',
    'label:user-story is:issue is:closed',
    'label:user-stories is:issue is:closed',
    'label:user_story is:issue is:closed',
    'label:userstory is:issue is:closed',
],
    "search_type": "issues",
    "sort": "created",
    "regex_filter": None,
    "regex_target": None,
    "time_windows": True,
}

STRATEGY_TEMPLATE = {
    "name": "TEMPLATE",
    "queries": [
        # Repos whose issue template has a Connextra structure.
        '"user story" path:.github/ISSUE_TEMPLATE',
        '"As a" "I want" path:.github/ISSUE_TEMPLATE',
        '"As a" "so that" path:.github/ISSUE_TEMPLATE',
        '"As a" "I would" path:.github/ISSUE_TEMPLATE',
        '"As a" "I need" path:.github/ISSUE_TEMPLATE',
        '"As a" "I can" path:.github/ISSUE_TEMPLATE',
        '"As a" "I should be able to" path:.github/ISSUE_TEMPLATE',
        '"acceptance criteria" "user story" path:.github/ISSUE_TEMPLATE',
        '"acceptance criteria" "As a" path:.github/ISSUE_TEMPLATE',
        # [US] tag in the issue templates.
        '"[US]" path:.github/ISSUE_TEMPLATE',
        '"[User Story]" path:.github/ISSUE_TEMPLATE',
        '"US-" path:.github/ISSUE_TEMPLATE',
        '"US -" path:.github/ISSUE_TEMPLATE',
    ],
    "search_type": "code",  # Uses /search/code instead of /search/issues.
    "sort": "created",
    "regex_filter": None,
    "regex_target": None,
    "time_windows": False,
}

STRATEGY_TAG = {
    "name": "TAG",
    "queries": [
        # A single targeted "US-" in:title query, which is far more precise
        # than the broader "user story" queries (already covered by MENTION).
        # Issues with a user-story label are excluded, as they belong to LABEL.
        f'"US-" in:title is:issue is:closed{EXCL_LABEL}',
    ],
    "search_type": "issues",
    "sort": "updated",
    "regex_filter": REGEX_TAG_COMBINED,
    "regex_target": "title",
    "time_windows": True,
    "delay_sec": 0.5,
    "max_pages": 10 ,
}

STRATEGY_MENTION = {
    "name": "MENTION",
    "queries": [
        # Textual mention of "user story" in the title or body.
        '"user story" in:title,body is:issue is:closed',
        '"user stories" in:title,body is:issue is:closed',
    ],
    "search_type": "issues",
    "sort": "created",
    "regex_filter": None,
    "regex_target": None,
    "time_windows": True,
}

STRATEGY_CONNEXTRA = {
    "name": "CONNEXTRA",
    "queries": [
        '"as a" "I want" in:body is:issue is:closed',
        '"as an" "I want" in:body is:issue is:closed',
        '"as a" "I need" in:body is:issue is:closed',
        '"as a" "I should be able to" in:body is:issue is:closed',
        '"as a" "I would like" in:body is:issue is:closed',
    ],
    "search_type": "issues",
    "sort": "updated",
    "regex_filter": REGEX_CONNEXTRA_PARTIAL,
    "regex_target": "body",
    "time_windows": True,
}

_ALL_STRATEGIES = [
    STRATEGY_LABEL,
    STRATEGY_TEMPLATE,
    STRATEGY_TAG,
    STRATEGY_MENTION,
    STRATEGY_CONNEXTRA,
]

# When active_strategies is set in the config, use only those.
_active_strategies_cfg = _dc.get("active_strategies")
if _active_strategies_cfg:
    _active_set = {s.upper() for s in _active_strategies_cfg}
    STRATEGIES = [s for s in _ALL_STRATEGIES if s["name"] in _active_set]
    _inactive = [s["name"] for s in _ALL_STRATEGIES if s["name"] not in _active_set]
    if _inactive:
        print(f"⚠️  Strategies disabled by config: {', '.join(_inactive)}")
else:
    STRATEGIES = _ALL_STRATEGIES

# Blacklist of repos already found in other experiments, listed under
# 'exclude_from_experiments' in config.json. Their repo_user_story.json
# files seed seen_repos, so re-found duplicates are discarded at zero cost.

REPO_BLACKLIST = set()
_exclude_from = _exp_config.get("exclude_from_experiments", []) or []
for _other_exp_id in _exclude_from:
    _other_repo_file = paths.base.parent / _other_exp_id / "1_discovery" / "repo_user_story.json"
    if not _other_repo_file.exists():  # if the file does not exist, skip
        print(f"⚠️  Exclusion: file not found for '{_other_exp_id}' — skip.")
        continue
    try:
        with _other_repo_file.open("r", encoding="utf-8") as _f:
            _other_repos = json.load(_f)
        _names = {r["name"] for r in _other_repos if "name" in r}
        REPO_BLACKLIST |= _names
        print(f"📋 Loaded {len(_names)} repos to exclude (from {_other_exp_id})")
    except Exception as _e:
        print(f"⚠️  Error loading exclusions from '{_other_exp_id}': {_e}")


# Print a summary of the chosen configuration at the start of the run.
print("=" * 70)
print("CONFIGURATION")
print("=" * 70)
print(f"  EXPERIMENT_ID:          {paths.experiment_id}")
# Display "NO CAP" when the cap is None.
_label_max_strat = "NO CAP" if MAX_REPO_PER_STRATEGY is None else MAX_REPO_PER_STRATEGY
_label_max_tot   = "NO CAP" if MAX_REPO_TOTAL is None else MAX_REPO_TOTAL
print(f"  MAX_REPO_PER_STRATEGY: {_label_max_strat}")
print(f"  MAX_REPO_TOTAL:        {_label_max_tot}")
print(f"  MIN_STARS:             {MIN_STARS}")
print(f"  BATCH_SIZE:             {BATCH_SIZE}")
print(f"  SEARCH_DELAY_SEC:       {SEARCH_DELAY_SEC}")
print(f"  ALLOWED_LANGUAGES:      {', '.join(ALLOWED_LANGUAGES)}")
print(f"  LANGUAGE_MODE:            {LANGUAGE_MODE}"
      + (f" (threshold: ≥{MIN_LANG_PCT:.1f}% bytes)" if LANGUAGE_MODE == "any_above_threshold" else ""))
print(f"  TIME_WINDOWS:           {len(TIME_WINDOWS)} windows ({TIME_WINDOWS_GRANULARITY})")
print(f"  STRATEGIES:              {len(STRATEGIES)}")
for s in STRATEGIES:
    n_query = len(s["queries"])
    sort_label = s.get("sort", "created")
    delay_label = f", delay={s['delay_sec']}s" if "delay_sec" in s else ""
    pages_label = f", max_pages={s['max_pages']}" if "max_pages" in s else ""
    placeholder = " [PLACEHOLDER — no query]" if n_query == 0 else f" ({n_query} queries)"
    print(f"    [{s['name']}]{placeholder} (sort={sort_label}{delay_label}{pages_label})")
print()


# Functions.

# Completeness flag: set to True whenever a GitHub call in the current
# strategy returns None (a transient error, not an empty result). A
# strategy that hit an error is not marked done, so it is retried.
_api_failure_in_current_strategy = False


def fetch_repos_graphql(repo_names):  # takes a list of repo names
    """Retrieve the metadata of multiple repos in a single GraphQL call.

    Builds a query with dynamic aliases (r0, r1, r2, ...) to request
    nameWithOwner, stars, creation date, primaryLanguage, the full
    distribution of the languages (for the 'any_above_threshold' filter) and
    the URL. Returns the 'data' dictionary of the GraphQL response (key = alias).
    """
    print(f"    [GraphQL] Sending a batch of {len(repo_names)} repos...")
    parts = []
    for i, name in enumerate(repo_names):
        owner, repo = name.split("/")
        # The top 10 languages by bytes are enough to decide whether a target
        # language is substantially present; totalSize covers all languages so
        # the percentages are correct.
        parts.append(
            f'r{i}: repository(owner:"{owner}", name:"{repo}") {{ '
            'nameWithOwner stargazerCount createdAt url '
            f'primaryLanguage {{ name }} '
            f'languages(first: 10, orderBy: {{field: SIZE, direction: DESC}}) {{ '
            f'  totalSize edges {{ size node {{ name }} }} '
            f'}} '
            f'}}'
        )
    query = "query { " + " ".join(parts) + " }"
    start = time.time()
    # gh.graphql handles pacing, retries and 403 classification, returning
    # the GraphQL 'data' dict (None on definitive errors).
    data = gh.graphql(query)
    elapsed = time.time() - start
    if data is None:
        print("    [GraphQL] ❌ Failed after retries, returning an empty dict")
        global _api_failure_in_current_strategy
        _api_failure_in_current_strategy = True
        return {}
    nulli = sum(1 for v in data.values() if v is None)
    print(f"    [GraphQL] Response in {elapsed:.2f}s — {len(data)} repos, {nulli} null")
    return data


def extract_languages_pct(info: dict) -> dict:
    """Compute the percentage distribution of a repo's languages.

    Input: the 'info' dict returned by fetch_repos_graphql for a repo.
    Output: a dict {language_name: bytes_pct} sorted by descending pct.
    If the repo has no languages (rare: empty repo), returns an empty dict.
    """
    langs_data = info.get("languages") or {}
    total = langs_data.get("totalSize") or 0
    edges = langs_data.get("edges") or []
    if total <= 0 or not edges:
        return {}
    out = {}
    for edge in edges:
        node = edge.get("node") or {}
        name = node.get("name")
        size = edge.get("size") or 0
        if name and size > 0:
            out[name] = round(size * 100.0 / total, 2)
    return out


def accept_repo_by_language(info: dict) -> tuple[bool, str | None, dict]:   # returns a tuple with (accepted, chosen_language, distribution)
    """Decide whether to accept a repo based on the language configuration.

    Returns a tuple (accepted, main_target_language, pct_distribution):
      - accepted: bool — whether the repo passes the language filter
      - main_target_language: name of the target language with the highest pct
        in the repo (e.g. "Java" if the repo is 60% Java + 30% Python; None if rejected).
        Used to decide the destination folder in phase 5.
      - pct_distribution: dict {language: pct} saved in the repo record.

    Logic:
    - "primary_only": accept if primaryLanguage ∈ ALLOWED_LANGUAGES.
      Target language = primaryLanguage.
    - "any_above_threshold": accept if at least one language in ALLOWED_LANGUAGES
      has pct >= MIN_LANG_PCT. Target language = the one with the highest pct
      among the targets present (in case of a tie, primaryLanguage if it is among them).
    """
    distribution = extract_languages_pct(info)
    primary = (info.get("primaryLanguage") or {}).get("name")

    if LANGUAGE_MODE == "primary_only":    # accept only if it is the repo's primary language
        if primary in ALLOWED_LANGUAGES:
            return True, primary, distribution
        return False, None, distribution

    # any_above_threshold
    targets_present = {
        lang: pct for lang, pct in distribution.items()
        if lang in ALLOWED_LANGUAGES and pct >= MIN_LANG_PCT
    }
    if not targets_present:
        return False, None, distribution

    # Choose the target language with the highest percentage.
    chosen_lang = max(targets_present.items(), key=lambda kv: kv[1])[0]
    return True, chosen_lang, distribution

# this is where it filters
def process_batch(batch, valid_repos, stats, strategy_name, max_acceptable):
    """Process a batch of repos. Accepts at most max_acceptable new repos.

    batch is a list of tuples (repo_name, query_match): for each repo we also
    know which specific query found it, so we can save it in the output.

    For each repo in the batch:
    - retrieve the metadata via GraphQL
    - apply the filters (stars, date, language, cap)
    - if it passes everything, add it to repo_validi with the originating
      strategy and query
    - update the counters in stats
    """
    # First it splits the batch into three dicts: names, originating queries, and
    # the trigger data (the issue or file that led to finding the repo).
    batch_names = [name for name, _, _ in batch]
    batch_queries = {name: q for name, q, _ in batch}
    batch_triggers = {name: t for name, _, t in batch}

    print(f"\n  --- PROCESSING BATCH ({len(batch_names)} repos, remaining cap: {max_acceptable}) ---")
    data = fetch_repos_graphql(batch_names)

    accepted = 0
    discarded_stars = 0
    discarded_date = 0
    discarded_null = 0
    discarded_cap = 0
    discarded_language = 0

    # Fetch the metadata and apply the filters in cascade:
    # not null
    # enough stars
    # not too recent
    # allowed language
    # not over the cap
    for key, info in data.items():
        if info is None:
            discarded_null += 1
            continue

        stars = info["stargazerCount"]
        created_date = info["createdAt"][:10]

        if stars < MIN_STARS:
            discarded_stars += 1
            continue
        # Keep only repos created in [REPO_CREATED_MIN, REPO_CREATED_MAX);
        # ISO date strings compare correctly lexicographically.
        if not (REPO_CREATED_MIN <= created_date < REPO_CREATED_MAX):
            discarded_date += 1
            continue

        # Language filter (see accept_repo_by_language). The result is kept
        # in its own variable so it does not shadow the batch counter.
        lang_ok, language, languages_distribution = accept_repo_by_language(info)
        if not lang_ok:
            discarded_language += 1
            continue

        if accepted >= max_acceptable:
            discarded_cap += 1
            continue

        accepted += 1
        repo_name = info["nameWithOwner"]
        trigger_data = batch_triggers.get(repo_name, {})

        if "issue_number" in trigger_data:
            issue_trigger = {
                "type": "issue",
                "number": trigger_data["issue_number"],
                "title": trigger_data["issue_title"],
                "url": trigger_data["issue_url"],
            }
        elif "file_path" in trigger_data:
            issue_trigger = {
                "type": "file",
                "path": trigger_data["file_path"],
                "url": trigger_data["file_url"],
            }
        else:
            issue_trigger = None

        repo_dict = {
            "name": repo_name,
            "url": info["url"],
            "stars": stars,
            "language": language,  # Target language for phase 5 (java/ or python/).
            "primary_language": (info.get("primaryLanguage") or {}).get("name"),
            "languages_pct": languages_distribution,
            "created_at": info["createdAt"][:10],
            "strategy": strategy_name,
            "query_match": batch_queries.get(repo_name, ""),
            "issue_trigger": issue_trigger,
        }

        if strategy_name == "CONNEXTRA":
            body = trigger_data.get("issue_body", "")
            repo_dict["connextra_partial"] = True
            repo_dict["connextra_complete"] = bool(REGEX_CONNEXTRA_COMPLETE.search(body))

        valid_repos.append(repo_dict)
        print(
            f"    ✅ [{len(valid_repos)}] {repo_name} - "
            f"{stars}★ - {language} - "
            f"{info['url']}"
        )

    stats["accepted"] += accepted
    stats["discarded_stars"] += discarded_stars
    stats["discarded_date"] += discarded_date
    stats["discarded_null"] += discarded_null
    stats["discarded_language"] += discarded_language
    print(f"  --- BATCH: {accepted} accepted, {discarded_stars} too few stars, "
          f"{discarded_date} too recent, {discarded_null} null, "
          f"{discarded_language} language not allowed"
          f"{f', {discarded_cap} over cap' if discarded_cap else ''} ---")
    return accepted


# Incremental checkpoint using two files: step1_repo_json holds the
# partial list of accepted repos (rewritten after every batch), and
# _progress.json holds the completed strategies and their stats (written
# after each strategy). Both use atomic writes so a kill cannot truncate
# them; completed strategies are skipped on resume.

_PROGRESS_FILE = paths.step1_dir / "_progress.json"


def _save_valid_repos(valid_repos_list):
    """Incremental save of valid_repos. Called after each process_batch."""
    try:
        paths.ensure_step_dir(1)
        dump_json_atomic(valid_repos_list, paths.step1_repo_json)
    except OSError as e:
        print(f"  ⚠️  Checkpoint save failed: {e} (continuing)")


def _save_progress(strategies_done_set, stats_per_strategy_dict,
                   global_stats_dict):
    """Save the strategies completed AND their cumulative stats.

    Called after each strategy. On restart, load_progress() reads this
    file back so the final summary reflects ALL the strategies ever run,
    not just the ones from the current SLURM job.
    """
    payload = {
        "strategies_done":     sorted(strategies_done_set),
        "stats_per_strategy":  stats_per_strategy_dict,
        "global_stats":        global_stats_dict,
        # Cumulative wall-clock across all jobs: previous jobs plus the time
        # since start_total, so the final total reflects every job.
        "cumulative_elapsed_seconds":
            cumulative_elapsed_seconds + (time.time() - start_total),
    }
    try:
        paths.ensure_step_dir(1)
        dump_json_atomic(payload, _PROGRESS_FILE)
    except OSError as e:
        print(f"  ⚠️  Progress save failed: {e} (continuing)")


# calls the GitHub REST API to search for issues
# Paginate until one of these stop conditions:
# null response or 422 error
# no results
# incomplete page (last page)
# reached max_pages

def search_issues_rest(query, time_window, title_filter=None, sort="created",
                       delay_sec=None, max_pages=10):
    """Search for issues via the GitHub REST API /search/issues with pagination.

    Combines query + time_window to stay under the 1000-results-per-query limit.
    If title_filter is a list of strings, it yields only the issues whose title
    contains at least one of them (client-side post-filter).
    Yields the names of the repos (owner/name) found.

    Optional parameters:
        delay_sec  — IGNORED (legacy): the pacing is handled by github_pacing
                     adaptively on the "search" bucket (30 req/min).
        max_pages  — page cap for this query/window (default: 10 = 1000 results)
    """
    page = 1
    per_page = 100
    q = f'{query} {time_window}'

    while True:
        print(f"    [REST] Page {page}/{max_pages} — query: {q}")

        # gh.get handles pacing, 5xx retries and 403 handling on the search
        # bucket, returning None on definitive errors.
        resp = gh.get(
            "https://api.github.com/search/issues",
            params={"q": q, "sort": sort, "order": "desc",
                    "per_page": per_page, "page": page},
        )
        if resp is None:
            print("    [REST] Call failed, skipping this window.")
            global _api_failure_in_current_strategy
            _api_failure_in_current_strategy = True
            break

        if resp.status_code == 422:
            print("    [REST] 422 — end of results for this query/window.")
            break

        # A 200 response is handled here; unrecoverable errors already
        # returned None above.
        data = resp.json()

        if page == 1:
            print(f"    [REST] Estimated results: {data['total_count']}")

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            if title_filter:
                title = item.get("title", "")
                if not any(tf.lower() in title.lower() for tf in title_filter):
                    continue
            repo_url = item["repository_url"]
            repo_name = "/".join(repo_url.split("/")[-2:])
            yield {   # yields, so it does not return a list but the results one at a time
                "repo_name": repo_name,
                "issue_number": item["number"],
                "issue_title": item["title"],
                "issue_body": item.get("body", "") or "",
                "issue_url": item["html_url"],
            }

        if len(items) < per_page:
            break

        if page >= max_pages:
            print(f"    [REST] Reached the limit of {max_pages} pages for this window.")
            break

        page += 1


def search_code_rest(query):
    """Search for files via the GitHub REST API /search/code with pagination.

    Unlike search_issues_rest, it does not use time windows
    (the code search does not support them in the same way).
    Yields unique repo names (owner/name) found in the query.
    """
    page = 1
    per_page = 100
    repos_seen_query = set()  # dedup: a repo can have multiple files matching the query

    while True:
        print(f"    [CODE] Page {page} — query: {query}")

        resp = gh.get(
            "https://api.github.com/search/code",
            params={"q": query, "per_page": per_page, "page": page},
        )
        if resp is None:
            print("    [CODE] Call failed, skipping.")
            global _api_failure_in_current_strategy
            _api_failure_in_current_strategy = True
            break

        if resp.status_code == 422:
            print("    [CODE] 422 — end of results for this query.")
            break

        data = resp.json()

        if page == 1:
            print(f"    [CODE] Estimated results: {data['total_count']}")

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            repo_name = item["repository"]["full_name"]
            if repo_name not in repos_seen_query:
                repos_seen_query.add(repo_name)
                yield {
                    "repo_name": repo_name,
                    "file_path": item["path"],
                    "file_url": item["html_url"],
                }

        if len(items) < per_page:
            break

        if page >= 10:
            print("    [CODE] Reached the 1000-results limit.")
            break

        page += 1


def run_strategy(strategy, seen_repos, valid_repos):
    """Run all the queries of a strategy and collect the repos found.

    - For each query of the strategy, it iterates over all the time windows
      (if time_windows=True and search_type=issues) or runs the search
      directly (search_type=code or time_windows=False).
    - It accumulates the repo names in a batch (with the originating query);
      when the batch reaches BATCH_SIZE it calls processa_batch().
    - It stops when it reaches MAX_REPO_PER_STRATEGY new accepted repos.
    - The repos_seen set is global: deduplication across strategies guaranteed.
    - It returns the stats dictionary with the strategy counters.
    """
    name = strategy["name"]
    queries = strategy["queries"]
    # Reset the completeness flag for this strategy; a None from any GitHub
    # call flips it, and it is read at the end to report success or failure.
    global _api_failure_in_current_strategy
    _api_failure_in_current_strategy = False
    search_type = strategy.get("search_type", "issues")
    sort_order = strategy.get("sort", "created")
    use_time_windows = strategy.get("time_windows", True)
    regex_filter = strategy.get("regex_filter")
    # Read the optional per-strategy parameters, defaulting to the globals.
    delay_sec = strategy.get("delay_sec", None)
    max_pages = strategy.get("max_pages", 10)

    print()
    print("=" * 70)
    print(f"STRATEGY: {name}")
    print("=" * 70)
    print(f"  Queries: {len(queries)}")
    print(f"  Search type: {search_type}")
    print(f"  Time windows: {use_time_windows}")
    print(f"  Delay per call: {delay_sec if delay_sec is not None else SEARCH_DELAY_SEC}s")
    print(f"  Max pages per window: {max_pages}")
    if regex_filter:
        print("  Regex filter: active")
    print(f"  Repos already seen (from previous strategies): {len(seen_repos)}")
    print(f"  Valid repos so far: {len(valid_repos)}")
    _target_label = "unlimited" if MAX_REPO_PER_STRATEGY is None else f"+{MAX_REPO_PER_STRATEGY}"
    print(f"  Target: {_target_label} new repos")
    print()

    if not queries:
        print(f"  ⏭  No query configured for {name} — strategy skipped.")
        # No queries is a clean no-op, not a failure.
        return ({"accepted": 0, "discarded_stars": 0, "discarded_date": 0,
                 "discarded_null": 0, "discarded_language": 0, "discarded_regex": 0},
                True)

    start_strategy = time.time()
    batch = []
    new_this_strategy = 0
    issues_this_strategy = 0
    duplicate_issues_this_strategy = 0
    discarded_regex = 0
    stats = {"accepted": 0, "discarded_stars": 0, "discarded_date": 0,
             "discarded_null": 0, "discarded_language": 0, "discarded_regex": 0,
             "issues_scanned": 0, "duplicate_issues": 0, "time_seconds": 0}
    cap_reached = False

    for query in queries:
        if cap_reached:
            break

        if search_type == "code":
            print(f"\n{'─' * 70}")
            print(f"  QUERY (code): {query}")
            print(f"{'─' * 70}")

            for file_data in search_code_rest(query):
                if new_this_strategy >= _cap_value(MAX_REPO_PER_STRATEGY):
                    print(f"\n  🎯 Reached the cap of {MAX_REPO_PER_STRATEGY} new repos for this strategy!")
                    cap_reached = True
                    break

                issues_this_strategy += 1
                stats["issues_scanned"] += 1
                repo_name = file_data["repo_name"]

                if repo_name in seen_repos:
                    duplicate_issues_this_strategy += 1
                    stats["duplicate_issues"] += 1
                    continue

                seen_repos.add(repo_name)
                batch.append((repo_name, query, file_data))

                print(f"  [Code #{issues_this_strategy}] New: {repo_name} "
                      f"(batch {len(batch)}/{BATCH_SIZE})")

                if len(batch) >= BATCH_SIZE:
                    remaining_slots = _remaining_slots(MAX_REPO_PER_STRATEGY, new_this_strategy)
                    accepted = process_batch(batch, valid_repos, stats, name, remaining_slots)
                    new_this_strategy += accepted
                    batch = []
                    _save_valid_repos(valid_repos)   # intra-strategy checkpoint
                    elapsed = time.time() - start_strategy
                    _target_label = "∞" if MAX_REPO_PER_STRATEGY is None else MAX_REPO_PER_STRATEGY
                    print(f"\n  📊 +{new_this_strategy}/{_target_label} new | "
                          f"{len(valid_repos)} total | {issues_this_strategy} results | "
                          f"{elapsed:.1f}s\n")
        else:
            windows = TIME_WINDOWS if use_time_windows else [""]

            for window in windows:
                if cap_reached:
                    break

                print(f"\n{'─' * 70}")
                if window:
                    print(f"  WINDOW: {window}")
                print(f"  QUERY: {query}")
                print(f"{'─' * 70}")

                for issue_data in search_issues_rest(query, window, sort=sort_order,
                                                     delay_sec=delay_sec,
                                                     max_pages=max_pages):
                    if new_this_strategy >= _cap_value(MAX_REPO_PER_STRATEGY):
                        print(f"\n  🎯 Reached the cap of {MAX_REPO_PER_STRATEGY} new repos for this strategy!")
                        cap_reached = True
                        break

                    issues_this_strategy += 1
                    stats["issues_scanned"] += 1
                    repo_name = issue_data["repo_name"]

                    if repo_name in seen_repos:
                        duplicate_issues_this_strategy += 1
                        stats["duplicate_issues"] += 1
                        continue

                    if regex_filter is not None:
                        target_field = strategy.get("regex_target")
                        if target_field == "title":
                            testo = issue_data["issue_title"]
                        elif target_field == "body":
                            testo = issue_data["issue_body"]
                        else:
                            testo = ""
                        if not regex_filter.search(testo):
                            discarded_regex += 1
                            # Collect the TAG discards for the optional diagnostic.
                            if SAVE_TAG_DISCARDS_DEBUG and name == "TAG":
                                _discarded_tag_debug.append({
                                    "repo": repo_name,
                                    "number": issue_data["issue_number"],
                                    "title": issue_data["issue_title"],
                                    "url": issue_data["issue_url"],
                                    "query": query,
                                    "window": window,
                                })
                            print(f"  [Issue #{issue_data['issue_number']}] "
                                  f"Repo {repo_name} discarded by the {name} regex")
                            continue

                    seen_repos.add(repo_name)
                    batch.append((repo_name, query, issue_data))

                    print(f"  [Issue #{issues_this_strategy}] New: {repo_name} "
                          f"(batch {len(batch)}/{BATCH_SIZE})")

                    if len(batch) >= BATCH_SIZE:
                        remaining_slots = _remaining_slots(MAX_REPO_PER_STRATEGY, new_this_strategy)
                        accepted = process_batch(batch, valid_repos, stats, name, remaining_slots)
                        new_this_strategy += accepted
                        batch = []
                        _save_valid_repos(valid_repos)   # intra-strategy checkpoint
                        elapsed = time.time() - start_strategy
                        _target_label = "∞" if MAX_REPO_PER_STRATEGY is None else MAX_REPO_PER_STRATEGY
                        print(f"\n  📊 +{new_this_strategy}/{_target_label} new | "
                              f"{len(valid_repos)} total | {issues_this_strategy} issues | "
                              f"{elapsed:.1f}s\n")

    # Flush the last batch
    if batch:
        remaining_slots = _remaining_slots(MAX_REPO_PER_STRATEGY, new_this_strategy)
        accepted = process_batch(batch, valid_repos, stats, name, remaining_slots)
        new_this_strategy += accepted
        _save_valid_repos(valid_repos)   # intra-strategy checkpoint (flush)

    elapsed_strategy = time.time() - start_strategy
    stats["discarded_regex"] = discarded_regex
    stats["time_seconds"] = round(elapsed_strategy, 1)
    print(f"\n  {'─' * 50}")
    print(f"  STRATEGY SUMMARY: {name}")
    print(f"  {'─' * 50}")
    print(f"  Time:                {elapsed_strategy:.1f}s")
    print(f"  Issues scanned:      {issues_this_strategy}")
    print(f"  Duplicate issues:    {duplicate_issues_this_strategy}")
    print(f"  Discarded (regex):   {discarded_regex}")
    print(f"  New accepted:        {stats['accepted']}")
    print(f"  Discarded (stars):   {stats['discarded_stars']}")
    print(f"  Discarded (date):    {stats['discarded_date']}")
    print(f"  Discarded (null):    {stats['discarded_null']}")
    print(f"  Discarded (language):{stats['discarded_language']}")
    print(f"  Total valid repos:   {len(valid_repos)}")

    # ok is True only if no GitHub call failed; otherwise the caller does
    # not mark the strategy done and it is retried next run.
    return stats, (not _api_failure_in_current_strategy)


# Execution.

def print_rate_limit(label):
    """Print the state of the 3 GitHub buckets (core, search, graphql).
    Wrapper on gh.print_state for compatibility with the existing code."""
    gh.print_state(label)



print_rate_limit("START")

start_total = time.time()
# Baseline of cumulative wall-clock from previous jobs, added to this
# process's clock so the total is not undercounted after a resume.
cumulative_elapsed_seconds = 0.0
seen_repos = set(REPO_BLACKLIST)  # pre-populated with the repos already seen in other experiments
valid_repos = []
global_stats = {"accepted": 0, "discarded_stars": 0, "discarded_date": 0,
                 "discarded_null": 0, "discarded_language": 0, "discarded_regex": 0}
stats_per_strategy = {}

# Resume from a previous run: step1_repo_json restores the accepted repos
# (their names seed seen_repos), and _PROGRESS_FILE restores the completed
# strategies and their stats so the final summary covers all jobs.
strategies_done: set = set()

if paths.step1_repo_json.exists():
    try:
        with paths.step1_repo_json.open("r", encoding="utf-8") as f:
            _resumed = json.load(f)
        if isinstance(_resumed, list) and _resumed:
            valid_repos = _resumed
            seen_repos |= {r["name"] for r in valid_repos
                           if isinstance(r, dict) and "name" in r}
            print(f"📂 RESUME: loaded {len(valid_repos)} repos already accepted "
                  f"from {paths.step1_repo_json.name}")
    except (json.JSONDecodeError, OSError) as e:
        print(f"⚠️  RESUME: error reading {paths.step1_repo_json}: {e}. "
              "Starting from scratch.")
        valid_repos = []

_prog = load_progress(str(_PROGRESS_FILE))
if _prog:
    strategies_done = set(_prog.get("strategies_done", []))
    cumulative_elapsed_seconds = float(_prog.get("cumulative_elapsed_seconds", 0.0) or 0.0)
    stats_per_strategy.update(_prog.get("stats_per_strategy") or {})
    for k, v in (_prog.get("global_stats") or {}).items():
        global_stats[k] = global_stats.get(k, 0) + v
    if strategies_done:
        print(f"📂 RESUME: {len(strategies_done)} strategies already completed "
              f"and will be skipped: {sorted(strategies_done)}")
        print(f"📂 RESUME: stats of {len(_prog.get('stats_per_strategy') or {})} "
              "previous strategies restored for the final summary.")

# Strategies that ran but hit a transient API error. The final gate exits
# 1 only if this list is non-empty.
failed_strategies: list = []

for strategy in STRATEGIES:
    if strategy["name"] in strategies_done:
        print(f"\n⏭  Skipping strategy '{strategy['name']}' — "
              "already completed in a previous run.")
        continue
    if len(valid_repos) >= _cap_value(MAX_REPO_TOTAL):
        print(f"\n🎯 Reached the global cap of {MAX_REPO_TOTAL} repos. Done.")
        break
    stats, ok = run_strategy(strategy, seen_repos, valid_repos)
    stats_per_strategy[strategy["name"]] = stats
    for k in global_stats:
        global_stats[k] += stats.get(k, 0)
    # Mark the strategy done only if it completed without API errors, so a
    # failed strategy is replayed next run; already-saved repos are kept and
    # dedup via seen_repos avoids duplicates on replay.
    if ok:
        strategies_done.add(strategy["name"])
    else:
        failed_strategies.append(strategy["name"])
        print(f"\n⚠️  Strategy '{strategy['name']}' did NOT complete cleanly "
              "(transient API error) — it will be retried on the next run.")
    _save_progress(strategies_done, stats_per_strategy, global_stats)
    _save_valid_repos(valid_repos)

elapsed_total = (time.time() - start_total) + cumulative_elapsed_seconds

print_rate_limit("END")

# Optional diagnostic: write the issues discarded by the TAG regex to a
# dedicated JSON file.
if SAVE_TAG_DISCARDS_DEBUG and _discarded_tag_debug:
    from pathlib import Path
    _debug_dir = Path(paths.step1_dir)
    _debug_dir.mkdir(parents=True, exist_ok=True)
    _debug_path = _debug_dir / "DEBUG_tag_discarded_regex.json"
    with open(_debug_path, "w", encoding="utf-8") as f:
        json.dump({
            "total_discarded": len(_discarded_tag_debug),
            "experiment_id": paths.experiment_id,
            "regex_pattern": REGEX_TAG_COMBINED.pattern,
            "discards": _discarded_tag_debug,
        }, f, indent=2, ensure_ascii=False)
    print(f"\n  📁 [DEBUG] Saved {len(_discarded_tag_debug)} issues discarded by TAG in:")
    print(f"      {_debug_path}")

print()
print("=" * 70)
print("FINAL GLOBAL SUMMARY")
print("=" * 70)
print(f"  Total time:            {elapsed_total:.1f}s")
print(f"  Unique repos seen:     {len(seen_repos)}")
print(f"  Total accepted:        {global_stats['accepted']}")
print(f"  Discarded (stars):     {global_stats['discarded_stars']}")
print(f"  Discarded (date):      {global_stats['discarded_date']}")
print(f"  Discarded (null):      {global_stats['discarded_null']}")
print(f"  Discarded (language):  {global_stats['discarded_language']}")
print(f"  Discarded (regex):     {global_stats['discarded_regex']}")
print(f"  Allowed languages:     {', '.join(ALLOWED_LANGUAGES)}")
print(f"  Final valid repos:     {len(valid_repos)}")
print()
print("  Repos per strategy:")
counts = {}
for r in valid_repos:
    s = r["strategy"]
    counts[s] = counts.get(s, 0) + 1
for s, count in counts.items():
    print(f"    {s}: {count}")
print("=" * 70)

# Save the final result.
paths.ensure_step_dir(1)
dump_json_atomic(valid_repos, paths.step1_repo_json)
print(f"\nSaved in {paths.step1_repo_json}")

# Strategies that failed on transient errors are dropped and logged; the
# phase continues.
if failed_strategies:
    print()
    print("=" * 70)
    print(f"⚠️  PHASE 1: {len(failed_strategies)} strategies failed on transient "
          f"API errors and were dropped: {failed_strategies}")
    print("   Policy: continue anyway (lost data logged in run_stats._lost_strategies).")
    print("=" * 70)

# The main file is saved first; the stats aggregation below is cosmetic.
try:
    from pipeline_state import mark_phase_done as _mark_phase1_done
    _mark_phase1_done(1)
    print("🏁 Signaled: phase 1 completed.")
except ImportError:
    pass

# Extra aggregations for the statistics, computed on in-memory data.

stars_list = [r.get("stars", 0) for r in valid_repos]
stars_list_sorted = sorted(stars_list)
n = len(stars_list_sorted)

stars_distribution = {
    "min": min(stars_list) if stars_list else 0,
    "max": max(stars_list) if stars_list else 0,
    "mean": round(sum(stars_list) / n, 1) if n else 0,
    "median": median(stars_list_sorted),
    "buckets": {
        "0-10":      sum(1 for s in stars_list if s < 10),
        "10-100":    sum(1 for s in stars_list if 10 <= s < 100),
        "100-1k":    sum(1 for s in stars_list if 100 <= s < 1000),
        "1k-10k":    sum(1 for s in stars_list if 1000 <= s < 10000),
        "10k+":      sum(1 for s in stars_list if s >= 10000),
    },
}

year_counts = {}
for r in valid_repos:
    data_creazione = r.get("created_at", "")
    if len(data_creazione) >= 4:
        year = data_creazione[:4]
        year_counts[year] = year_counts.get(year, 0) + 1
year_distribution = dict(sorted(year_counts.items()))

language_counts = {}
for r in valid_repos:
    language = r.get("language", "Unknown")
    language_counts[language] = language_counts.get(language, 0) + 1
language_distribution = dict(sorted(language_counts.items(), key=lambda x: -x[1]))

top_10_stars = [
    {"name": r["name"], "stars": r.get("stars", 0), "language": r.get("language", "")}
    for r in sorted(valid_repos, key=lambda x: -x.get("stars", 0))[:10]
]

acceptance_rates = {}
for name, s in stats_per_strategy.items():
    scorse = s.get("issues_scanned", 0)
    acc = s.get("accepted", 0)
    acceptance_rates[name] = round(acc * 100 / max(scorse, 1), 2) if scorse else 0.0

run_stats = {
    "total_time_seconds": round(elapsed_total, 1),
    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    # Uniform fields for external consumers.
    "_schema_version": 1,
    "_phase": 1,
    # strategies dropped on transient API errors
    "_lost_strategies": failed_strategies,
    "configuration": {
        "min_stars": MIN_STARS,
        "max_repos_per_strategy": MAX_REPO_PER_STRATEGY,
        "max_total_repos": MAX_REPO_TOTAL,
        "allowed_languages": list(ALLOWED_LANGUAGES),
        "language_mode": LANGUAGE_MODE,
        "min_lang_pct": MIN_LANG_PCT if LANGUAGE_MODE == "any_above_threshold" else None,
        "n_time_windows": len(TIME_WINDOWS),
        "time_windows_granularity": TIME_WINDOWS_GRANULARITY,
        # Repo creation-date range, for traceability.
        "repo_created_min": REPO_CREATED_MIN,
        "repo_created_max": REPO_CREATED_MAX,
    },
    "global_totals": {
        "unique_repos_seen": len(seen_repos),
        "accepted_repos": len(valid_repos),
        "discarded_stars": global_stats["discarded_stars"],
        "discarded_date": global_stats["discarded_date"],
        "discarded_null": global_stats["discarded_null"],
        "discarded_language": global_stats["discarded_language"],
        "discarded_regex": global_stats["discarded_regex"],
    },
    "per_strategy": stats_per_strategy,
    "acceptance_rates_per_strategy_pct": acceptance_rates,
    "stars_distribution": stars_distribution,
    "creation_year_distribution": year_distribution,
    "language_distribution": language_distribution,
    "top_10_repo_by_stars": top_10_stars,
}
try:
    dump_json_atomic(run_stats, paths.step1_stats_json)
    print(f"Saved run statistics in {paths.step1_stats_json}")
except Exception as e:
    print(f"⚠️  Error saving run_stats: {e}")