import json
import os
import sys
import time
import re
import base64
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

# Make the pipeline package importable when run directly or via the
# orchestrator.
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from config.experiment_paths import paths

# GitHub client with adaptive per-bucket pacing.
from utils import github_pacing as gh

from utils.logging_utils import tprint

# Atomic JSON writes and the read-side companion for crash-resilient
# checkpoints.
from utils.atomic_io import dump_json_atomic
from utils.checkpoint import load_progress

# English-only filter on the issue text.
from utils import lang_detect

sys.stdout.reconfigure(encoding='utf-8')

# Configuration.

INPUT_FILE  = str(paths.step1_repo_json)
OUTPUT_FILE = str(paths.step2_issues_json)
# Issues dropped in this phase, with a reason (currently: non-English text).
DISCARDED_FILE = str(paths.step2_dir / "issues_discarded.json")

MAX_WORKERS         = 4     # Repos processed in parallel; pacing is coordinated by the client.
MAX_DISCARD_RATIO    = 0.85  # Early-exit from a strategy discarding >85% of issues.

# Cutoff date filter, read from the 'dataset_config' section of
# config.json. Includes only issues with closed_at on or after
# cutoff_date_closed (and, optionally, on or before an end bound). The
# filter is applied server-side in the GitHub query and re-checked
# client-side. It uses closed_at, not created_at. Disabled when the key
# is null or absent.

def _load_cutoff_dates() -> "tuple[str | None, str | None]":
    """Read dataset_config.cutoff_date_closed and .cutoff_date_closed_end.
    Together they define the interval [start, end] on closed_at; either bound
    may be None (→ open on that side). Minimal format validation (YYYY-MM-DD);
    an invalid bound is treated as None (that side disabled)."""
    try:
        cfg = paths.load_config()
    except Exception as e:
        print(f"  ⚠️  Error reading config.json ({e}) — cutoff DISABLED")
        return None, None
    dataset_cfg = cfg.get("dataset_config", {}) or {}

    def _parse(key):
        val = dataset_cfg.get(key)
        if val is None or val == "":
            return None
        val = str(val).strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", val):
            print(f"  ⚠️  {key}='{val}' is invalid (expected YYYY-MM-DD) — that bound DISABLED")
            return None
        return val

    return _parse("cutoff_date_closed"), _parse("cutoff_date_closed_end")

CUTOFF_DATE_CLOSED, CUTOFF_DATE_CLOSED_END = _load_cutoff_dates()

# Per-repo cap on extracted user stories, read from 'dataset_config'.
# Applied after the strategy filters, so it limits the user stories taken
# from each repo. None (or an absent key) means no cap; the default is 15.

def _load_max_issues_per_repo() -> "int | None":
    """Read dataset_config.max_issue_per_repo from the config.json.
    Returns an int >= 1 (cap active), or None (no cap, download all of them).
    Default fallback if the key is absent: 15."""
    try:
        cfg = paths.load_config()
    except Exception as e:
        print(f"  ⚠️  Error reading config.json ({e}) — using default 15")
        return 15
    dataset_cfg = cfg.get("dataset_config", {}) or {}
    # Missing key: default cap of 15.
    if "max_issues_per_repo" not in dataset_cfg:
        return 15
    val = dataset_cfg["max_issues_per_repo"]
    # Explicit null: no cap.
    if val is None:
        return None
    try:
        val_int = int(val)
        if val_int < 1:
            raise ValueError
        return val_int
    except (TypeError, ValueError):
        print(f"  ⚠️  max_issue_per_repo='{val}' is invalid (expected int >= 1 or null) — using default 15")
        return 15

MAX_ISSUE_PER_REPO = _load_max_issues_per_repo()

# English-only filter, read from 'dataset_config': english_only (default
# true) and english_min_confidence (default 0.8). Applied per issue after
# the strategy regex, so langid runs only on candidate user stories.
# Dropped issues are recorded with reason="not_english".

def _load_english_filter_config() -> "tuple[bool, float]":
    try:
        cfg = paths.load_config()
    except Exception:
        return True, 0.8
    dc = cfg.get("dataset_config", {}) or {}
    enabled = dc.get("english_only", True)
    enabled = True if enabled is None else bool(enabled)
    try:
        thr = float(dc.get("english_min_confidence", 0.8))
    except (TypeError, ValueError):
        thr = 0.8
    return enabled, thr

ENGLISH_ONLY, ENGLISH_MIN_CONFIDENCE = _load_english_filter_config()

# If the filter is on but langid is missing, stop with a clear message
# instead of silently producing an unfiltered dataset.
if ENGLISH_ONLY and not lang_detect.available():
    print("=" * 70)
    print("❌ english_only is ON but the 'langid' library is not available:")
    print(f"   {lang_detect.import_error()}")
    print("   Install it in the pipeline env:  pip install langid")
    print("   (or disable the filter: dataset_config.english_only = false)")
    print("=" * 70)
    sys.exit(1)

# Cross-experiment blacklist, read from the top-level key
# 'exclude_issues_from_experiments': a list of experiment IDs whose
# already-extracted phase-2 issues are skipped. Their issues_user_story.json
# files build a set of repo#number keys, checked in process_repo() before
# cutoff and regex. Disabled when the list is empty or the key is absent.

def _load_issue_blacklist() -> "tuple[set[str], list[str]]":
    """Load the 'repo#number' keys from Phase 2 of the experiments listed in
    'exclude_issues_from_experiments'. Returns (set of keys, list of sources
    actually loaded successfully).
    """
    blacklist: set[str] = set()
    loaded_sources: list[str] = []
    try:
        cfg = paths.load_config()
    except Exception as e:
        print(f"  ⚠️  Error reading config.json ({e}) — issue exclusion DISABLED")
        return blacklist, loaded_sources
    exclude_list = cfg.get("exclude_issues_from_experiments", []) or []
    if not exclude_list:
        return blacklist, loaded_sources
    for other_exp_id in exclude_list:
        other_file = paths.base.parent / other_exp_id / "2_issue_discovery" / "issues_user_story.json"
        if not other_file.exists():
            print(f"⚠️  Issue exclusion: file not found for '{other_exp_id}' — skip.")
            continue
        try:
            with other_file.open("r", encoding="utf-8") as f:
                other_issues = json.load(f)
            keys = {
                f"{i['repo']}#{i['number']}"
                for i in other_issues
                if "repo" in i and "number" in i
            }
            blacklist |= keys
            loaded_sources.append(other_exp_id)
            print(f"📋 Loaded {len(keys)} issues to exclude (from {other_exp_id})")
        except Exception as e:
            print(f"⚠️  Error loading issue exclusions from '{other_exp_id}': {e}")
    if blacklist:
        print(f"📋 Total issues in the blacklist: {len(blacklist)} "
              f"(from {len(loaded_sources)} experiments)")
    return blacklist, loaded_sources


ISSUE_BLACKLIST, ISSUE_BLACKLIST_SOURCES = _load_issue_blacklist()
# Thread-safe counter of blacklist skips (protected by results_lock).
issue_skip_blacklist = 0

# results_lock serializes access to the shared result structures.

results_lock = Lock()

# Regexes are centralized in discovery_strategies.py, shared with phase 1.

from utils.discovery_strategies import (
    REGEX_TAG_COMBINED,
    REGEX_CONNEXTRA_PARTIAL,
)
from utils.stats_utils import median

# Keywords for the template analysis.

TEMPLATE_KEYWORD_PATTERNS = [
    ('"As a" "I want" "so that"',    ["As a", "I want", "so that"]),
    ('"As a" "I want"',              ["As a", "I want"]),
    ('"As a" "so that"',             ["As a", "so that"]),
    ('"As a" "I need"',              ["As a", "I need"]),
    ('"As a" "I would"',             ["As a", "I would"]),
    ('"As a" "I can"',               ["As a", "I can"]),
    ('"As a" "I should be able to"', ["As a", "I should be able to"]),
    ('"acceptance criteria"',        ["acceptance criteria"]),
    ('"user story"',                 ["user story"]),
    ('"User Story"',                 ["User Story"]),
]

# Strategies, ordered from the most precise to the broadest; max_pages
# is smaller for high-noise strategies.

STRATEGY_LABEL = {
    "name": "LABEL",
    "queries": [
        # Labels with spaces are quoted in a single comma-separated query.
        'label:"user story","user stories" is:issue is:closed',
        # Labels without spaces go unquoted, each in its own query, since
        # GitHub rejects mixing quoted and unquoted labels in one list.
        'label:user-story is:issue is:closed',
        'label:user-stories is:issue is:closed',
        'label:user_story is:issue is:closed',
        'label:userstory is:issue is:closed',
        # Generic enhancement label combined with Connextra in the body.
        'label:enhancement "as a" in:body is:issue is:closed',
    ],
    "regex_filter": None,
    "regex_target": None,
    "max_pages": 3,
}

STRATEGY_TAG = {
    "name": "TAG",
    "queries": [
        '"[US]" in:title is:issue is:closed',
        '"[User Story]" in:title is:issue is:closed',
        '"User Story" in:title is:issue is:closed',
        # Captures hybrid titles such as "US-42: User Story for X".
        'US "user story" in:title is:issue is:closed',
    ],
    "regex_filter": REGEX_TAG_COMBINED,
    "regex_target": "title",
    "delay_sec": 0.5,
    "max_pages": 5,
    "sort_order": "updated",
}

STRATEGY_TEMPLATE = {
    "name": "TEMPLATE",
    "queries": [],
    "regex_filter": None,
    "regex_target": None,
    "template_based": True,
    "sort_order": "updated",
    "max_pages": 3,
}

STRATEGY_CONNEXTRA = {
    "name": "CONNEXTRA",
    "queries": [
        # A single query with an OR on the verbs; REGEX_CONNEXTRA_PARTIAL
        # filters false positives on the body, so coverage is unchanged.
        '("as a" OR "as an") ("I want" OR "I need" OR "I should be able to" OR "I would like") in:body is:issue is:closed',
    ],
    "regex_filter": REGEX_CONNEXTRA_PARTIAL,
    "regex_target": "body",
    "sort_order": "updated",
    "max_pages": 3,
}

STRATEGY_MENTION = {
    "name": "MENTION",
    "queries": [
        '"user story" in:title,body is:issue is:closed',
        '"user stories" in:title,body is:issue is:closed',
    ],
    "regex_filter": None,
    "regex_target": None,
    "max_pages": 2,
}

# LABEL and TAG first (precise and fast), MENTION last (broadest).
_ALL_STRATEGIES = [
    STRATEGY_LABEL,
    STRATEGY_TAG,
    STRATEGY_TEMPLATE,
    STRATEGY_CONNEXTRA,
    STRATEGY_MENTION,
]

# When active_strategies is set in the config, use only those.
try:
    _exp_config = paths.load_config()
    _active_strategies_cfg = _exp_config.get("discovery_config", {}).get("active_strategies")
except Exception:
    _active_strategies_cfg = None

if _active_strategies_cfg:
    _active_set = {s.upper() for s in _active_strategies_cfg}
    STRATEGIES = [s for s in _ALL_STRATEGIES if s["name"] in _active_set]
    _inactive = [s["name"] for s in _ALL_STRATEGIES if s["name"] not in _active_set]
    if _inactive:
        print(f"⚠️  Strategies disabled by config: {', '.join(_inactive)}")
else:
    STRATEGIES = _ALL_STRATEGIES

# max_pages_override, read from discovery_config: when set, it overrides
# the max_pages of every strategy (used in Full Coverage mode to reach the
# GitHub ceiling of 10 pages). Absent or None keeps each strategy's own value.
try:
    _max_pages_override = (
        _exp_config.get("discovery_config", {}).get("max_pages_override")
    )
except Exception:
    _max_pages_override = None

if _max_pages_override is not None:
    try:
        _override_int = int(_max_pages_override)
        if _override_int < 1:
            raise ValueError("must be >= 1")
        for _s in STRATEGIES:
            _s["max_pages"] = _override_int
        print(f"⚙️  max_pages_override applied: all the strategies use max_pages={_override_int}")
    except (ValueError, TypeError) as _e:
        print(f"⚠️  max_pages_override='{_max_pages_override}' is invalid ({_e}) — ignored")

print("=" * 70)
print("PHASE 2 — EXTRACTION OF ISSUES WITH USER STORIES")
print("=" * 70)
print(f"  EXPERIMENT_ID:         {paths.experiment_id}")
print(f"  Input:                 {INPUT_FILE}")
print(f"  Output:                {OUTPUT_FILE}")
if MAX_ISSUE_PER_REPO is None:
    print("  MAX_ISSUE_PER_REPO:    NO CAP  (download all the user stories found)")
else:
    print(f"  MAX_ISSUE_PER_REPO:    {MAX_ISSUE_PER_REPO}")
print("  Pacing:                handled by github_pacing (search/core bucket)")
print(f"  MAX_WORKERS:           {MAX_WORKERS}")
print(f"  MAX_DISCARD_RATIO:      {MAX_DISCARD_RATIO:.0%}")
if CUTOFF_DATE_CLOSED or CUTOFF_DATE_CLOSED_END:
    print(f"  CUTOFF (closed in):    {CUTOFF_DATE_CLOSED or '−∞'} .. {CUTOFF_DATE_CLOSED_END or '+∞'}  ← issues closed outside are excluded")
else:
    print("  CUTOFF (closed in):    DISABLED  (both bounds absent or null)")
print(f"  Strategies:            {len(STRATEGIES)}")
for s in STRATEGIES:
    n_q = len(s["queries"])
    template_label = " [template-based]" if s.get("template_based") else ""
    delay_label    = f", delay={s['delay_sec']}s" if "delay_sec" in s else ""
    pages_label    = f", max_pages={s.get('max_pages', 10)}"
    regex_label    = f", regex={s['regex_target']}" if s.get("regex_filter") else ""
    n_label = "no static query" if n_q == 0 else f"{n_q} queries"
    print(f"    [{s['name']}] {n_label}{template_label}{delay_label}{pages_label}{regex_label}")
print()


# Load the repos from phase 1.

paths.ensure_step_dir(2)

if not os.path.exists(INPUT_FILE):
    raise FileNotFoundError(f"File {INPUT_FILE} not found. Run phase 1 first.")

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    repo_list = json.load(f)

print(f"Loaded {len(repo_list)} repos from {INPUT_FILE}")

# Guard on an empty repo list: the next phases need OUTPUT_FILE to exist.
if not repo_list:
    print("⚠️  No repo to process. Exiting with an empty output file.")
    dump_json_atomic([], OUTPUT_FILE)
    try:
        from pipeline_state import mark_phase_done
        mark_phase_done(2)
    except ImportError:
        pass
    sys.exit(0)

template_repos = set()
for r in repo_list:
    if r.get("strategy") == "TEMPLATE":
        template_repos.add(r["name"])

print(f"Repos found with TEMPLATE in phase 1: {len(template_repos)}")
print()


# Issue search.

def search_issues_repo(repo_name, query, seen_issues, delay_sec=None, max_pages=10,
                       sort_order="created", error_sink=None):
    """Search for issues in a repo via /search/issues. The pacing is now
    handled by the github_pacing module centrally (the "search" bucket shared
    among all the threads).

    `error_sink` (point 1): an optional mutable dict. If a GitHub call returns
    None (a transient API/network error, NOT a 422 end-of-results), we set
    error_sink["failed"]=True so the caller knows this repo's issue list is
    INCOMPLETE and must not be marked as fully processed.
    """
    page = 1
    per_page = 100
    # Query: `repo:` prefix + strategy query + optional cutoff filter. The
    # cutoff is applied server-side, so out-of-range issues are not sent.
    q = f"repo:{repo_name} {query}"
    # Build the closed: qualifier from the interval bounds (either side
    # may be open).
    if CUTOFF_DATE_CLOSED and CUTOFF_DATE_CLOSED_END:
        q = f"{q} closed:{CUTOFF_DATE_CLOSED}..{CUTOFF_DATE_CLOSED_END}"
    elif CUTOFF_DATE_CLOSED:
        q = f"{q} closed:>={CUTOFF_DATE_CLOSED}"
    elif CUTOFF_DATE_CLOSED_END:
        q = f"{q} closed:<={CUTOFF_DATE_CLOSED_END}"

    while True:
        # gh.get handles adaptive pacing, 5xx retries and 403 handling on
        # the globally coordinated "search" bucket.
        resp = gh.get(
            "https://api.github.com/search/issues",
            params={
                "q": q,
                "sort": sort_order,
                "order": "desc",
                "per_page": per_page,
                "page": page,
            },
        )
        if resp is None:
            tprint(f"    [REST] Call failed for {repo_name}, skipping.")
            # Flag the search as incomplete so the caller does not mark the
            # repo as fully processed.
            if error_sink is not None:
                error_sink["failed"] = True
            break

        if resp.status_code == 422:
            tprint("    [REST] 422 — end of results or invalid query.")
            break

        data = resp.json()

        if page == 1:
            total_count = data.get("total_count", 0)
            tprint(f"      → Estimated results: {total_count}")
            # No results: stop early.
            if total_count == 0:
                break

        items = data.get("items", [])
        if not items:
            break

        for item in items:
            issue_id = item["number"]

            if issue_id in seen_issues:
                continue
            seen_issues.add(issue_id)

            labels = [lbl["name"] for lbl in item.get("labels", [])]
            yield {
                "number": issue_id,
                "title": item.get("title", ""),
                "body": item.get("body", "") or "",
                "labels": labels,
                "url": item.get("html_url", ""),
                "author": item.get("user", {}).get("login", ""),
                "state": item.get("state", ""),
                "created_at": item.get("created_at", "")[:10],
                "closed_at": (item.get("closed_at") or "")[:10],
                "repo": repo_name,
            }

        if len(items) < per_page:
            break

        if page >= max_pages:
            tprint(f"      → Reached the limit of {max_pages} pages for this query.")
            break

        page += 1


# Template download and analysis.

def fetch_issue_templates(repo_name):
    """Download the issue templates from .github/ISSUE_TEMPLATE/.
    Uses gh.get → rate limiting + 403 handled centrally.
    Returns a list of strings (raw templates)."""
    templates = []

    # Attempt 1: the .github/ISSUE_TEMPLATE/ directory (modern repos).
    url_dir = f"https://api.github.com/repos/{repo_name}/contents/.github/ISSUE_TEMPLATE"
    resp = gh.get(url_dir, timeout=15)
    if resp is not None and resp.status_code == 200:
        try:
            files = resp.json()
            for f in files:
                if not isinstance(f, dict):
                    continue
                fname = f.get("name", "")
                if not fname.endswith((".md", ".yml", ".yaml")):
                    continue
                content_b64 = f.get("content")
                if content_b64:
                    try:
                        templates.append(
                            base64.b64decode(content_b64).decode("utf-8", errors="replace")
                        )
                    except Exception:
                        pass
                else:
                    # Content not inline: fetch the file.
                    resp2 = gh.get(f["url"], timeout=15)
                    if resp2 is not None and resp2.status_code == 200:
                        try:
                            data2 = resp2.json()
                            content_b64 = data2.get("content", "")
                            if content_b64:
                                templates.append(
                                    base64.b64decode(content_b64).decode("utf-8", errors="replace")
                                )
                        except Exception:
                            pass
        except Exception as e:
            tprint(f"      [Template] Error parsing the directory: {e}")

    # Attempt 2: the single .github/ISSUE_TEMPLATE.md file (legacy repos).
    if not templates:
        url_single = f"https://api.github.com/repos/{repo_name}/contents/.github/ISSUE_TEMPLATE.md"
        resp = gh.get(url_single, timeout=15)
        if resp is not None and resp.status_code == 200:
            try:
                data = resp.json()
                content_b64 = data.get("content", "")
                if content_b64:
                    templates.append(
                        base64.b64decode(content_b64).decode("utf-8", errors="replace")
                    )
            except Exception:
                pass

    return templates


def extract_queries_from_template(templates):
    testo = "\n".join(templates).lower()
    query_generate = []
    query_viste = set()

    for query_str, keywords in TEMPLATE_KEYWORD_PATTERNS:
        if all(kw.lower() in testo for kw in keywords):
            q = f'{query_str} in:body is:issue is:closed'
            if q not in query_viste:
                query_viste.add(q)
                query_generate.append(q)

    return query_generate


# Per-repo processing, with an early exit on a high regex discard rate.

def process_repo(repo_name, is_template_repo):
    seen_issues   = set()
    found_issues = []
    lang_discarded = []   # issues dropped by the English-only filter (this repo)
    regex_discarded_per_strategy = {}
    # Shared across this repo's searches: a transient failure sets
    # repo_error["failed"] so the repo is retried rather than marked done
    # with a partial list.
    repo_error = {"failed": False}

    for strategy in STRATEGIES:
        name         = strategy["name"]
        regex_filter = strategy.get("regex_filter")
        regex_target = strategy.get("regex_target")
        delay_sec    = strategy.get("delay_sec", None)
        max_pages    = strategy.get("max_pages", 10)
        sort_order   = strategy.get("sort_order", "created")
        is_template_based = strategy.get("template_based", False)

        if MAX_ISSUE_PER_REPO is not None and len(found_issues) >= MAX_ISSUE_PER_REPO:
            break

        if is_template_based and not is_template_repo:
            continue

        tprint(f"\n  [{name}] — {repo_name}")

        queries = strategy["queries"]

        if is_template_based:
            tprint("    Downloading the .github/ISSUE_TEMPLATE/ templates...")
            templates = fetch_issue_templates(repo_name)
            if not templates:
                tprint("    No template found, skipping the strategy.")
                continue
            tprint(f"    Found {len(templates)} template files")
            queries = extract_queries_from_template(templates)
            if not queries:
                tprint("    No relevant keyword in the templates, skipping the strategy.")
                continue
            tprint(f"    Generated {len(queries)} queries from the templates")

        # Track the discard rate for a strategy-level early exit.
        strat_found  = 0
        strat_discarded = 0

        for query in queries:
            if MAX_ISSUE_PER_REPO is not None and len(found_issues) >= MAX_ISSUE_PER_REPO:
                break

            # Early exit if this strategy's discard rate is too high.
            if regex_filter is not None and strat_found + strat_discarded >= 20:
                ratio = strat_discarded / (strat_found + strat_discarded)
                if ratio >= MAX_DISCARD_RATIO:
                    tprint(f"    ⚡ Early-exit [{name}]: discard rate {ratio:.0%} ≥ {MAX_DISCARD_RATIO:.0%}, "
                           "strategy too noisy for this repo.")
                    break

            tprint(f"    Query: {query[:80]}{'...' if len(query) > 80 else ''}")

            # Consecutive discards in the current query: a long run of them
            # means the query is paginating through noise, so we stop.
            consecutive_discards = 0
            MAX_CONSECUTIVE_DISCARDS = 30

            for issue in search_issues_repo(repo_name, query, seen_issues,
                                            delay_sec=delay_sec, max_pages=max_pages,
                                            sort_order=sort_order, error_sink=repo_error):
                if MAX_ISSUE_PER_REPO is not None and len(found_issues) >= MAX_ISSUE_PER_REPO:
                    tprint(f"      → Cap of {MAX_ISSUE_PER_REPO} issues reached.")
                    break

                # Blacklist check first (O(1) set lookup), so cutoff and regex
                # are not evaluated on issues we would discard anyway.
                if ISSUE_BLACKLIST:
                    key = f"{repo_name}#{issue['number']}"
                    if key in ISSUE_BLACKLIST:
                        global issue_skip_blacklist
                        with results_lock:
                            issue_skip_blacklist += 1
                        tprint(f"      ⏭️  #{issue['number']} skipped (already analyzed in a previous run)")
                        continue

                # Client-side cutoff check as a safety net for out-of-range
                # issues from stale indexes and for a missing closed_at. The
                # string comparison is valid because both are YYYY-MM-DD.
                if CUTOFF_DATE_CLOSED or CUTOFF_DATE_CLOSED_END:
                    closed_on = issue.get("closed_at", "")
                    out_of_range = (
                        not closed_on
                        or (CUTOFF_DATE_CLOSED and closed_on < CUTOFF_DATE_CLOSED)
                        or (CUTOFF_DATE_CLOSED_END and closed_on > CUTOFF_DATE_CLOSED_END)
                    )
                    if out_of_range:
                        tprint(f"      ✗ #{issue['number']} discarded: closed on '{closed_on}' "
                               f"(outside {CUTOFF_DATE_CLOSED or '−∞'}..{CUTOFF_DATE_CLOSED_END or '+∞'})")
                        continue

                if regex_filter is not None:
                    testo = issue["title"] if regex_target == "title" else issue.get("body", "")
                    if not regex_filter.search(testo):
                        regex_discarded_per_strategy[name] = regex_discarded_per_strategy.get(name, 0) + 1
                        strat_discarded += 1
                        consecutive_discards += 1
                        tprint(f"      ✗ #{issue['number']} discarded by the {name} regex")
                        # Bail out of the current query.
                        if consecutive_discards >= MAX_CONSECUTIVE_DISCARDS:
                            tprint(f"      ⚡ Query bail-out: {consecutive_discards} consecutive discards, "
                                   "query too noisy for this repo.")
                            break
                        continue

                # English-only filter, run only on issues that passed the
                # strategy regex, so langid is called rarely.
                if ENGLISH_ONLY:
                    verdict = lang_detect.classify_english(
                        issue.get("title", ""), issue.get("body", ""),
                        ENGLISH_MIN_CONFIDENCE,
                    )
                    if not verdict["keep"]:
                        issue["strategy"] = name
                        issue["discard_reason"] = "not_english"
                        issue["detected_language"] = verdict["language"]
                        issue["language_confidence"] = verdict["confidence"]
                        lang_discarded.append(issue)
                        tprint(f"      ✗ #{issue['number']} discarded: "
                               f"language='{verdict['language']}' "
                               f"(confidence {verdict['confidence']})")
                        continue

                issue["strategy"] = name
                found_issues.append(issue)
                strat_found += 1
                consecutive_discards = 0
                tprint(f"      ✅ #{issue['number']} — {issue['title'][:60]}")

    return found_issues, regex_discarded_per_strategy, lang_discarded, repo_error["failed"]


# Execution, parallelized with a ThreadPoolExecutor.

# Progress file for cumulative counters and timers across runs. It holds
# the meta-stats that cannot be reconstructed from OUTPUT_FILE alone
# (regex discards, per-strategy wall-clock, etc.).
_PROGRESS_FILE = paths.step2_dir / "_progress.json"

if os.path.exists(OUTPUT_FILE):
    with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
        total_issues = json.load(f)
    repos_already_processed = set(i["repo"] for i in total_issues)
    tprint(f"Resuming from an existing file: {len(total_issues)} issues, "
           f"{len(repos_already_processed)} repos already processed.")
else:
    total_issues = []
    repos_already_processed = set()

# Language-discarded issues, resumed from disk so a re-run keeps what was
# already recorded; already-processed repos are skipped, so no duplicates.
if os.path.exists(DISCARDED_FILE):
    try:
        with open(DISCARDED_FILE, "r", encoding="utf-8") as f:
            lang_discarded_issues = json.load(f)
    except (json.JSONDecodeError, OSError):
        lang_discarded_issues = []
else:
    lang_discarded_issues = []

start_total = time.time()
strategy_stats           = {}
regex_discarded_per_strategy_global = {}
time_per_strategy       = {}
issues_per_repo            = {}
regex_discarded_total        = 0
# Cumulative elapsed seconds across all jobs; start_total measures only
# the current process, so cumulative_elapsed is added at the end.
cumulative_elapsed_seconds = 0.0

# Restore counters that cannot be rebuilt from total_issues: regex
# discards, per-strategy wall-clock and cumulative elapsed. strategy_stats
# and issues_per_repo are recomputed at the end instead.
_prog2 = load_progress(str(_PROGRESS_FILE))
if _prog2:
    regex_discarded_total = int(_prog2.get("regex_discarded_total", 0))
    regex_discarded_per_strategy_global.update(
        _prog2.get("regex_discarded_per_strategy_global") or {})
    time_per_strategy.update(_prog2.get("time_per_strategy") or {})
    cumulative_elapsed_seconds = float(_prog2.get("cumulative_elapsed_seconds", 0.0))
    tprint(f"📂 RESUME: restored progress counters "
           f"(regex_discarded_total={regex_discarded_total}, "
           f"cumulative_elapsed={cumulative_elapsed_seconds:.1f}s)")


def _save_progress_phase2():
    """Persist cumulative counters + per-strategy timings to _progress.json.

    Called inside the same lock as the OUTPUT_FILE checkpoint, so the two
    files are always written one after the other in a consistent state.
    The cumulative elapsed is the time spent in PREVIOUS jobs PLUS the time
    spent since `start_total` in THIS job.
    """
    payload = {
        "regex_discarded_total": regex_discarded_total,
        "regex_discarded_per_strategy_global": regex_discarded_per_strategy_global,
        "time_per_strategy": time_per_strategy,
        "cumulative_elapsed_seconds": cumulative_elapsed_seconds
                                       + (time.time() - start_total),
    }
    try:
        paths.ensure_step_dir(2)
        dump_json_atomic(payload, _PROGRESS_FILE)
    except OSError as e:
        tprint(f"⚠️  Progress save failed: {e} (continuing)")

tprint()
tprint("=" * 70)
tprint("START OF ISSUE EXTRACTION (parallelized)")
tprint("=" * 70)

repos_to_process = [
    (idx + 1, r) for idx, r in enumerate(repo_list)
    if r["name"] not in repos_already_processed
]
tprint(f"Repos to process: {len(repos_to_process)} "
       f"(already processed: {len(repos_already_processed)})\n")


def process_repo_wrapper(args):
    """Wrapper for the thread pool."""
    repo_idx, repo_info = args
    repo_name = repo_info["name"]
    is_template_repo = repo_name in template_repos

    tprint()
    tprint(f"[{repo_idx}/{len(repo_list)}] REPO: {repo_name}")
    tprint(f"  Phase 1 strategy:    {repo_info.get('strategy', 'N/A')}")
    tprint(f"  TEMPLATE active:     {'YES' if is_template_repo else 'NO'}")

    start_repo = time.time()

    try:
        issue_repo, discarded_repo_dict, lang_discarded_repo, repo_failed = process_repo(repo_name, is_template_repo)
    except Exception as e:
        tprint(f"  ⚠️  Error on {repo_name}: {e}")
        traceback.print_exc()
        tprint("  → Skipping the repo.")
        # An unexpected exception leaves the issue list incomplete, so the
        # repo is marked failed and retried.
        return repo_name, [], {}, [], 0.0, True

    elapsed_repo = time.time() - start_repo
    return repo_name, issue_repo, discarded_repo_dict, lang_discarded_repo, elapsed_repo, repo_failed


# Repos whose discovery hit a transient error: their partial issue list is
# discarded so they are re-processed next run. The final gate exits 1 if any
# remain here.
failed_repos: list = []

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(process_repo_wrapper, args): args for args in repos_to_process}

    for future in as_completed(futures):
        repo_name, issue_repo, discarded_repo_dict, lang_discarded_repo, elapsed_repo, repo_failed = future.result()

        # A failed repo is not persisted or marked processed, so nothing
        # partial leaks into total_issues (which drives the resume).
        if repo_failed:
            failed_repos.append(repo_name)
            tprint()
            tprint(f"  ⚠️  [{repo_name}] discovery INCOMPLETE (transient error) "
                   f"— partial results discarded, will retry on next run.")
            continue

        with results_lock:
            for s_name, n in discarded_repo_dict.items():
                regex_discarded_per_strategy_global[s_name] = regex_discarded_per_strategy_global.get(s_name, 0) + n
                regex_discarded_total += n

            issues_per_repo[repo_name] = len(issue_repo)

            for issue in issue_repo:
                s = issue["strategy"]
                strategy_stats[s] = strategy_stats.get(s, 0) + 1

            strategies_in_repo = {issue["strategy"] for issue in issue_repo}
            if strategies_in_repo:
                time_per_strat_in_repo = elapsed_repo / len(strategies_in_repo)
                for s in strategies_in_repo:
                    time_per_strategy[s] = time_per_strategy.get(s, 0) + time_per_strat_in_repo

            total_issues.extend(issue_repo)
            lang_discarded_issues.extend(lang_discarded_repo)
            repos_already_processed.add(repo_name)

            discarded_total_repo = sum(discarded_repo_dict.values())
            tprint()
            tprint(f"  → [{repo_name}] Found {len(issue_repo)} issues in {elapsed_repo:.1f}s "
                   f"(regex discards: {discarded_total_repo}, "
                   f"non-English discards: {len(lang_discarded_repo)})")
            tprint(f"  → Total issues accumulated: {len(total_issues)}")

            # Atomically checkpoint data, discarded issues and progress counters.
            dump_json_atomic(total_issues, OUTPUT_FILE)
            dump_json_atomic(lang_discarded_issues, DISCARDED_FILE)
            _save_progress_phase2()
            tprint(f"  → Saved in {OUTPUT_FILE}")


# Guaranteed final save, in case every repo was already processed and the
# in-loop checkpoint wrote nothing.
paths.ensure_step_dir(2)
dump_json_atomic(total_issues, OUTPUT_FILE)
dump_json_atomic(lang_discarded_issues, DISCARDED_FILE)
_save_progress_phase2()

# Repos that failed on transient errors had their partial results
# discarded; they are logged and the phase continues.
if failed_repos:
    print()
    print("=" * 70)
    print(f"⚠️  PHASE 2: {len(failed_repos)} repos failed on transient errors "
          f"and were dropped (partial results discarded).")
    print(f"   Examples: {failed_repos[:10]}")
    print("   Policy: continue anyway (lost data logged in run_stats._lost_repos).")
    print("=" * 70)

# The main file is saved first; the run_stats save below is cosmetic.
try:
    from pipeline_state import mark_phase_done as _mark_phase2_done
    _mark_phase2_done(2)
    print("🏁 Signaled: phase 2 completed.")
except ImportError:
    pass

# Final summary.

# Cumulative elapsed across all jobs of this phase; the persisted progress
# adds the time from previous runs to this process's clock.
elapsed_total = cumulative_elapsed_seconds + (time.time() - start_total)

tprint()
tprint("=" * 70)
tprint("FINAL SUMMARY — PHASE 2")
tprint("=" * 70)
tprint(f"  Total time:            {elapsed_total:.1f}s")
tprint(f"  Repos processed:       {len(repos_already_processed)}")
tprint(f"  Total issues found:    {len(total_issues)}")
tprint(f"  Discarded (regex):     {regex_discarded_total}")
if ISSUE_BLACKLIST:
    tprint(f"  Skipped (blacklist):   {issue_skip_blacklist} "
           f"(from {len(ISSUE_BLACKLIST_SOURCES)} previous runs)")
tprint()
tprint("  Issues per strategy:")
for s in STRATEGIES:
    count = strategy_stats.get(s["name"], 0)
    tprint(f"    {s['name']}: {count}")
tprint()
tprint(f"  Output saved in:       {OUTPUT_FILE}")
tprint("=" * 70)


# Save the run statistics.

RUN_STATS_FILE = str(paths.step2_stats_json)


counts_per_repo = list(issues_per_repo.values())
empty_repos = sum(1 for c in counts_per_repo if c == 0)
issues_per_repo_distribution = {
    "min": min(counts_per_repo) if counts_per_repo else 0,
    "max": max(counts_per_repo) if counts_per_repo else 0,
    "mean": round(sum(counts_per_repo) / max(len(counts_per_repo), 1), 2),
    "median": median(counts_per_repo),
    "empty_repos": empty_repos,
    "repo_con_almeno_1_issue": len(counts_per_repo) - empty_repos,
}

top_10_repos = [
    {"repo": r, "issue": c}
    for r, c in sorted(issues_per_repo.items(), key=lambda kv: -kv[1])[:10]
]

per_strategy_dict = {}
for s in STRATEGIES:
    name = s["name"]
    n_issue = strategy_stats.get(name, 0)
    n_discarded = regex_discarded_per_strategy_global.get(name, 0)
    elapsed = time_per_strategy.get(name, 0)
    total_seen = n_issue + n_discarded
    per_strategy_dict[name] = {
        "issues_found": n_issue,
        "discarded_regex": n_discarded,
        "regex_discard_rate_pct": round(n_discarded * 100 / max(total_seen, 1), 1) if total_seen else 0,
        "time_seconds": round(elapsed, 1),
        "avg_time_per_issue_seconds": round(elapsed / max(n_issue, 1), 2) if n_issue else 0,
    }

run_stats = {
    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    # Uniform fields for external consumers.
    "_schema_version": 1,
    "_phase": 2,
        "_lost_repos": failed_repos,
    "total_time_seconds": round(elapsed_total, 1),
    "avg_time_per_repo_seconds": round(elapsed_total / max(len(repos_already_processed), 1), 2),
    "configuration": {
        "max_issues_per_repo": MAX_ISSUE_PER_REPO,
        "pacing": "github_pacing (adaptive per-bucket)",
        "max_workers": MAX_WORKERS,
        "cutoff_date_closed": CUTOFF_DATE_CLOSED,
        "cutoff_date_closed_end": CUTOFF_DATE_CLOSED_END,
        "active_strategies": [s["name"] for s in STRATEGIES],
        "exclude_issues_from_experiments": ISSUE_BLACKLIST_SOURCES,
    },
    "totals": {
        "processed_repos": len(repos_already_processed),
        "issues_found": len(total_issues),
        "discarded_regex": regex_discarded_total,
        "discarded_not_english": len(lang_discarded_issues),
        "skipped_blacklist": issue_skip_blacklist,
        "blacklist_size": len(ISSUE_BLACKLIST),
        "empty_repos": empty_repos,
    },
    "per_strategy": per_strategy_dict,
    "issues_per_repo_distribution": issues_per_repo_distribution,
    "top_10_repo_per_issue": top_10_repos,
}

dump_json_atomic(run_stats, RUN_STATS_FILE)