import argparse
import requests
import json
import os
import sys
import time
import traceback
import urllib.parse
from datetime import datetime, timezone

# Make the pipeline package importable when run directly or via the
# orchestrator.
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from config.experiment_paths import paths

# GitHub client with adaptive per-bucket pacing.
from utils import github_pacing as gh
from utils.stats_utils import median

# Atomic JSON writes for crash-resilient checkpoints.
from utils.atomic_io import dump_json_atomic

# GitHub fetch helpers (PR details, files, commits) with an in-memory cache.
from dataset_builder import github_fetch as _gf

# Force UTF-8 output (needed on Windows for non-ASCII characters).
sys.stdout.reconfigure(encoding="utf-8")

# Configuration.

INPUT_FILE  = str(paths.step4_validate_json)
REPO_FILE   = str(paths.step1_repo_json)
OUTPUT_DIR  = str(paths.step5_dir)

# Save progress every N issues so a crash can be resumed.
CHECKPOINT_EVERY = 10
CHECKPOINT_FILE  = str(paths.step5_checkpoint_json)

# Caps to avoid processing PRs with thousands of files.
MAX_FILES_PER_PR     = 1000  # Intentional cap, flagged in data.json.
MAX_COMMITS_PER_PR   = 250   # GitHub limit for the /commits endpoint.

# Extensions used only as a per-file metadata flag; nothing is excluded.
USEFUL_EXTENSIONS = {
    "java":   {".java", ".xml", ".gradle", ".properties", ".kts"},
    "python": {".py", ".cfg", ".toml", ".ini"},
}

# PR-level language filter: requires each issue's PR to touch a
# meaningful number of target-language files, avoiding near-empty
# dataset folders. Configured under the 'dataset_config' section of
# config.json; disabled by default (0 files required).
_dataset_cfg = {}
try:
    _dataset_cfg = paths.load_config().get("dataset_config", {}) or {}
except Exception:
    _dataset_cfg = {}

MIN_LANGUAGE_FILES      = int(_dataset_cfg.get("min_language_files", 0))
MIN_LANGUAGE_FRACTION   = float(_dataset_cfg.get("min_language_fraction", 0.0))

if not (0.0 <= MIN_LANGUAGE_FRACTION <= 1.0):
    print(f"⚠️  min_language_fraction={MIN_LANGUAGE_FRACTION} outside [0,1], reset to 0.0")
    MIN_LANGUAGE_FRACTION = 0.0
if MIN_LANGUAGE_FILES < 0:
    MIN_LANGUAGE_FILES = 0

# Result codes, distinguishing permanent failures (never retried) from
# transient ones (safe to retry).
RESULT_OK              = "ok"
RESULT_SKIP_PERMANENT  = "skip_permanent"   # Never retry (404, language, no files).
RESULT_RETRY_LATER     = "retry_later"      # Transient error, safe to retry.


# Quality gate mirroring phase 4: a record whose role and benefit are
# both placeholders is not a real user story and gets no folder. Both
# fields must be placeholders, so legitimate partial stories are kept.
_PLACEHOLDER_VALUES = {"", "none", "n/a", "unknown", "null"}
_PLACEHOLDER_SUBSTR = ("not specified", "not explicitly", "implied by the changes")

def _is_placeholder(value) -> bool:
    s = (value or "").strip().lower()
    return s in _PLACEHOLDER_VALUES or any(p in s for p in _PLACEHOLDER_SUBSTR)


# When enabled in config, records failing the grounding check are dropped.
try:
    _GROUNDING_ENABLED = bool(
        (paths.load_config().get("validation_config", {}) or {}).get("grounding_enabled", False)
    )
except Exception:
    _GROUNDING_ENABLED = False






# Setup.

# Create the root folder and the two language subfolders.
os.makedirs(os.path.join(OUTPUT_DIR, "java"),   exist_ok=True)
os.makedirs(os.path.join(OUTPUT_DIR, "python"), exist_ok=True)

# Thin wrapper over github_pacing.

def github_get(url: str, params: dict = None) -> "requests.Response | None":
    """
    Thin wrapper over gh.get(). I keep the name so as not to touch the ~50
    call sites in the rest of the file. All the pacing/retry/403 logic is
    inside github_pacing.
    """
    return gh.get(url, params=params)


# Wrappers over dataset_builder.github_fetch that inject the GET function
# and this script's constants, keeping the original call sites unchanged.

select_primary_pr     = _gf.select_primary_pr
_to_utc_aware         = _gf.to_utc_aware
_compute_delta_days = _gf.compute_delta_days


def get_pr_details(owner: str, repo: str, pr_number: int) -> dict | None:
    """Wrapper: injects github_get."""
    return _gf.get_pr_details(owner, repo, pr_number, gh_get=github_get)


def get_pr_files(owner: str, repo: str, pr_number: int) -> tuple[list, dict]:
    """Wrapper: injects github_get and MAX_FILES_PER_PR. Returns (files, meta)."""
    return _gf.get_pr_files(
        owner, repo, pr_number,
        gh_get=github_get, max_files=MAX_FILES_PER_PR,
    )


def get_pr_commits(owner: str, repo: str, pr_number: int) -> list:
    """Wrapper: injects github_get and MAX_COMMITS_PER_PR."""
    return _gf.get_pr_commits(
        owner, repo, pr_number,
        gh_get=github_get, max_commits=MAX_COMMITS_PER_PR,
    )


def get_pr_metadata_cached(repo_full: str, owner: str, repo_name: str, pr_number: int) -> dict | None:
    """Wrapper: injects github_get and the pagination caps.

    Note: the cache is module-level inside dataset_builder.github_fetch,
    so it is automatically shared between all calls.
    """
    return _gf.get_pr_metadata_cached(
        repo_full, owner, repo_name, pr_number,
        gh_get=github_get,
        max_files_per_pr=MAX_FILES_PER_PR,
        max_commits_per_pr=MAX_COMMITS_PER_PR,
    )




# Building the folder for one issue.

def _build_diff_entry(file_info: dict, repo_full: str,
                      base_sha: str, head_sha: str,
                      lang_folder: str) -> dict:
    """Build the data.json entry for a single PR file.

    No file content is downloaded: we store metadata + the URLs needed to
    fetch the original (at base_sha) and modified (at head_sha) versions later.
    On renames the original lives at previous_filename @ base_sha.
    """
    filename          = file_info.get("filename", "")
    status            = file_info.get("status", "")
    previous_filename = file_info.get("previous_filename")  # Set by GitHub on renames.

    original_path = previous_filename or filename

    def _enc(path: str) -> str:
        return urllib.parse.quote(path, safe="/")

    has_original = status != "added"
    has_modified = status != "removed"

    fetch = {
        "original_api": (
            f"https://api.github.com/repos/{repo_full}/contents/{_enc(original_path)}?ref={base_sha}"
            if has_original else None
        ),
        "modified_api": (
            f"https://api.github.com/repos/{repo_full}/contents/{_enc(filename)}?ref={head_sha}"
            if has_modified else None
        ),
        "original_raw": (
            f"https://raw.githubusercontent.com/{repo_full}/{base_sha}/{_enc(original_path)}"
            if has_original else None
        ),
        "modified_raw": (
            f"https://raw.githubusercontent.com/{repo_full}/{head_sha}/{_enc(filename)}"
            if has_modified else None
        ),
    }

    ext = os.path.splitext(filename)[1].lower()
    relevant = ext in USEFUL_EXTENSIONS.get(lang_folder, set())

    return {
        "filename":              filename,
        "previous_filename":     previous_filename,   # null unless renamed
        "status":                status,
        "blob_sha":              file_info.get("sha", ""),
        "additions":             file_info.get("additions", 0),
        "deletions":             file_info.get("deletions", 0),
        "changes":               file_info.get("changes", 0),
        "patch":                 file_info.get("patch", None),
        "relevant_for_language": relevant,
        "blob_url":              file_info.get("blob_url", ""),
        "raw_url":               file_info.get("raw_url", ""),
        "contents_url":          file_info.get("contents_url", ""),
        "fetch":                 fetch,
    }


def build_issue_folder(issue: dict, repo_lang_map: dict, output_dir: str) -> str:
    """
    Builds the dataset folder for a single validated issue.

    Returns one of the codes:
      - RESULT_OK             : issue processed successfully (folder created)
      - RESULT_SKIP_PERMANENT : issue NOT to be retried (PR 404, unsupported
                                language, PR-level filter failed).
                                While polling it is added to `processed`.
      - RESULT_RETRY_LATER    : transient error (rate-limit, network).
                                While polling it is NOT added to `processed`.

    Folder structure:
        <output_dir>/
            java/   <owner>_<repo>/issue_<number>/   data.json
            python/ <owner>_<repo>/issue_<number>/   data.json
    """
    repo_full  = issue["repo"]
    owner, repo_name = repo_full.split("/", 1)
    issue_num  = issue["number"]

    # Determine the subfolder based on the repo's language
    language = repo_lang_map.get(repo_full, "unknown")
    lang_folder = language.lower()

    # Only Java and Python are supported; repos in other languages are
    # skipped with a clear message.
    if lang_folder not in ("java", "python"):
        print(f"  ⚠️  Skip {repo_full} #{issue_num}: language "
              f"'{language}' not supported (expected: java|python)")
        return RESULT_SKIP_PERMANENT

    # Quality gate: skip records whose role and benefit are both
    # placeholders; they are not real user stories.
    av = issue.get("ai_validation") or {}
    if _is_placeholder(av.get("role")) and _is_placeholder(av.get("benefit")):
        print(f"  ⏭️  Skip {repo_full} #{issue_num}: not a user story "
              f"(role/benefit are placeholders) — permanent")
        return RESULT_SKIP_PERMANENT

    if _GROUNDING_ENABLED:
        _grd = av.get("grounding") or {}
        if _grd.get("passed") is False:
            print(f"  ⏭️  Skip {repo_full} #{issue_num}: ungrounded user story "
                  f"({_grd.get('reason')}) — permanent")
            return RESULT_SKIP_PERMANENT
    
    # Structure: <lang>/<owner>_<repo>/issue_<number>/
    repo_folder = repo_full.replace("/", "_")   # e.g. "NixOS_nix-security-tracker"
    issue_folder = f"issue_{issue_num}"
    folder_path = os.path.join(output_dir, lang_folder, repo_folder, issue_folder)

    # Skip if the folder already has data.json (resume from checkpoint).
    if os.path.exists(os.path.join(folder_path, "data.json")):
        print(f"  ⏭️  Skip (already exists): {lang_folder}/{repo_folder}/{issue_folder}")
        return RESULT_OK

    # Primary PR selection.
    primary_pr_meta = select_primary_pr(issue["pr"])
    pr_number = primary_pr_meta["number"]

    print(f"  📥 [{lang_folder}/{repo_folder}] Downloading PR #{pr_number} for issue #{issue_num}")

    # PR metadata (details, files, commits), reused from cache when the
    # same PR was already fetched for another issue.
    pr_meta = get_pr_metadata_cached(repo_full, owner, repo_name, pr_number)
    if pr_meta is None:
        # github_pacing already retries internally: a None result here is
        # permanent and must not be retried.
        print(f"    ❌ Could not retrieve PR #{pr_number} details — issue skipped (permanent)")
        return RESULT_SKIP_PERMANENT

    pr_details   = pr_meta["details"]
    pr_files_raw = pr_meta["files"]
    pr_commits   = pr_meta["commits"]

    base_sha = pr_details["base_sha"]
    head_sha = pr_details["head_sha"]

    # PR-level language filter: skip PRs that do not touch enough
    # target-language files. Active only when configured.
    if MIN_LANGUAGE_FILES > 0 or MIN_LANGUAGE_FRACTION > 0.0:
        extensions_target = USEFUL_EXTENSIONS.get(lang_folder, set())
        n_total = len(pr_files_raw)
        n_lang = sum(
            1 for f in pr_files_raw
            if os.path.splitext(f.get("filename", ""))[1].lower() in extensions_target
        )

        # PR with no files (empty or merged with no useful diff).
        if n_total == 0:
            print(f"    ⏭️  Skip: PR #{pr_number} has no files (empty PR) — issue skipped (permanent)")
            return RESULT_SKIP_PERMANENT

        if n_lang < MIN_LANGUAGE_FILES:
            print(f"    ⏭️  Skip: PR #{pr_number} only touches {n_lang} {lang_folder} files "
                  f"(minimum required: {MIN_LANGUAGE_FILES}) — issue skipped (permanent)")
            return RESULT_SKIP_PERMANENT

        fraction = n_lang / n_total
        if fraction < MIN_LANGUAGE_FRACTION:
            print(f"    ⏭️  Skip: PR #{pr_number} has {n_lang}/{n_total} {lang_folder} files "
                  f"({fraction*100:.0f}% < threshold {MIN_LANGUAGE_FRACTION*100:.0f}%) "
                  "— issue skipped (permanent)")
            return RESULT_SKIP_PERMANENT

    # All filters passed: create the issue folder now, so discarded
    # issues leave no empty folders.
    os.makedirs(folder_path, exist_ok=True)

    # Build diff entries from PR file metadata (no download).
    repo_full_str = f"{owner}/{repo_name}"
    diff_entries = [
        _build_diff_entry(fi, repo_full_str, base_sha, head_sha, lang_folder)
        for fi in pr_files_raw
    ]
    files_meta = pr_meta.get("files_meta") or {}

    # Build data.json.
    data_json = {
        "repository":         repo_full,
        "issue_number":       issue_num,
        "issue_title":        issue["title"],
        "issue_body":         issue.get("body"),
        "issue_url":          issue["url"],
        "issue_author":       issue.get("author"),
        "issue_state":        issue.get("state"),
        "issue_created_at":   issue.get("created_at"),
        "issue_closed_at":    issue.get("closed_at"),  # NEW
        "issue_labels":       issue.get("labels", []),
        "discovery_strategy": issue.get("strategy"),
        "link_source":        issue.get("link_source"),

        "ai_validation": issue.get("ai_validation"),

        "primary_pr": {
            "number":      pr_number,
            "title":       primary_pr_meta["title"],
            "url":         primary_pr_meta["url"],
            "merged_at":   primary_pr_meta.get("merged_at"),
            "source":       primary_pr_meta.get("source"),
            "source_branch": pr_details["source_branch"],
            "base_branch":   pr_details["base_branch"],
            "additions":     pr_details["additions"],
            "deletions":     pr_details["deletions"],
            "changed_files": pr_details["changed_files"],
        },

        "base_sha":         base_sha,                              # state BEFORE development
        "last_commit_sha":  head_sha,                              # FINAL state of the PR branch
        "merge_commit_sha": pr_details.get("merge_commit_sha"),    # NEW: MERGE commit

        # mergeable/mergeable_state are reliable mainly on open PRs; for
        # already merged PRs GitHub often returns null, so had_conflicts
        # stays None.
        "merge_info": {
            "merge_commit_sha": pr_details.get("merge_commit_sha"),
            "merged":           pr_details.get("merged"),
            "mergeable":        pr_details.get("mergeable"),
            "mergeable_state":  pr_details.get("mergeable_state"),
            "had_conflicts":    (
                True if pr_details.get("mergeable_state") in ("dirty", "conflicting")
                else False if pr_details.get("mergeable_state") in ("clean", "unstable", "has_hooks")
                else None
            ),
        },

        # Timeline: how close the PR merge is to the issue closing.
        "timeline": {
            "issue_created_at": issue.get("created_at"),
            "issue_closed_at":  issue.get("closed_at"),
            "pr_merged_at":     primary_pr_meta.get("merged_at"),
            "delta_days_issue_closed_to_pr_merged": _compute_delta_days(
                issue.get("closed_at"),
                primary_pr_meta.get("merged_at"),
            ),
        },

        "diff_between_base_and_last": diff_entries,
        # Completeness flags for the file list: capped means truncated at
        # the cap on purpose; not-complete and not-capped means a page failed.
        "files_complete": files_meta.get("complete", True),
        "files_capped":   files_meta.get("capped", False),
        "files_fetched":  len(diff_entries),

        "commits": pr_commits,

        "all_linked_prs": issue.get("pr", []),

        "pull_request_url": primary_pr_meta["url"],
        "commits_url":      f"https://github.com/{repo_full}/pull/{pr_number}/commits",
        "files_url":        f"https://github.com/{repo_full}/pull/{pr_number}/files",

        "language":           language,
        "dataset_build_date": datetime.now(timezone.utc).isoformat(),
    }

    data_json_path = os.path.join(folder_path, "data.json")
    dump_json_atomic(data_json, data_json_path)

    _cap = " (capped at %d)" % MAX_FILES_PER_PR if files_meta.get("capped") else ""
    _inc = " (INCOMPLETE: a /files page failed)" if (files_meta.get("complete") is False and not files_meta.get("capped")) else ""
    print(f"    ✅ {len(diff_entries)} files in diff{_cap}{_inc}")

    return RESULT_OK


# Checkpoint.

def load_checkpoint() -> tuple[set, dict | None]:
    """Load the checkpoint file.

    Returns (processed_keys_set, stats_dict_or_None).

    Before this refactor only the `processed` set was returned and the
    `stats` field already present in the checkpoint was silently dropped;
    the main() function then rebuilt a fresh stats dict starting from
    zeros, so on every resume the run_stats summary only reflected the
    SLURM job that actually printed it, not the whole phase.
    """
    if not os.path.exists(CHECKPOINT_FILE):
        return set(), None
    try:
        with open(CHECKPOINT_FILE) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        # Tolerate a corrupt checkpoint by starting clean.
        print(f"⚠️  load_checkpoint: {CHECKPOINT_FILE} unreadable ({e}). "
              "Starting from scratch.")
        return set(), None
    return set(data.get("processed", [])), data.get("stats")


def save_checkpoint(processed: set, stats: dict):
    """Save the checkpoint atomically.

    Both `processed` and `stats` are persisted together so a future
    load_checkpoint() returns a fully consistent state.
    """
    dump_json_atomic(
        {"processed": list(processed), "stats": stats},
        CHECKPOINT_FILE,
    )


# Main.

def _load_input_phase5() -> list | None:
    """Re-reads the input file. Robust to a missing or corrupt file.
    Returns None on an I/O error (vs [] for an empty/absent file)."""
    if not os.path.exists(INPUT_FILE):
        return []
    try:
        with open(INPUT_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"   ⚠️  {INPUT_FILE} not readable: {e}.")
        return None


def _load_repo_lang_map() -> dict:
    """Loads the repo→language map. Stable (produced by phase 1)."""
    with open(REPO_FILE, encoding="utf-8") as f:
        repos_raw = json.load(f)
    return {r["name"]: r["language"] for r in repos_raw}


def process_batch_phase5(issues, repo_lang_map, processed, stats,
                         start_total, prev_total_seconds):
    """Process a batch of issues, skipping those already in the checkpoint.

    Updates ``processed`` and ``stats`` and returns the number of issues
    processed in this batch. The per-issue result is handled as a tristate:
    RESULT_OK and RESULT_SKIP_PERMANENT are added to ``processed`` (the
    latter counted as a permanent skip), while RESULT_RETRY_LATER is left
    out so it is retried on the next pass.
    """
    to_process = [
        i for i in issues
        if f"{i['repo']}#{i['number']}" not in processed
    ]
    if not to_process:
        return 0

    n_processed_batch = 0

    for i, issue in enumerate(to_process, 1):
        issue_id = f"{issue['repo']}#{issue['number']}"
        prefix   = f"[{i}/{len(to_process)}]"

        # Re-check in case the issue was processed in a previous pass.
        if issue_id in processed:
            continue

        print(f"\n{prefix} ▶️  {issue_id}")

        try:
            result = build_issue_folder(issue, repo_lang_map, OUTPUT_DIR)
        except Exception as e:
            print(f"    💥 Unexpected error: {e}")
            traceback.print_exc()
            # Treat unexpected errors as transient so the issue can be
            # retried; a deterministic failure eventually exits the pipeline.
            result = RESULT_RETRY_LATER

        if result == RESULT_OK:
            processed.add(issue_id)
            stats["completed"] += 1
        elif result == RESULT_SKIP_PERMANENT:
            processed.add(issue_id)
            stats["permanent_skip"] = stats.get("permanent_skip", 0) + 1
        else:  # RESULT_RETRY_LATER
            stats["failed"] += 1

        n_processed_batch += 1

        if n_processed_batch % CHECKPOINT_EVERY == 0:
            # Persist the cumulative wall-clock so a mid-batch kill does not
            # lose this session's time.
            _now_total = (time.time() - start_total) + prev_total_seconds
            stats["total_time_seconds"] = round(_now_total, 1)
            _n_done = stats["completed"] + stats["failed"]
            stats["avg_time_per_issue_seconds"] = round(_now_total / max(_n_done, 1), 2)
            stats["last_updated"] = datetime.now().isoformat()
            save_checkpoint(processed, stats)
            print(f"\n💾 Checkpoint saved ({stats['completed']} completed, "
                  f"{stats.get('permanent_skip', 0)} permanent skips, "
                  f"{stats['failed']} transient failures)")

    # Checkpoint at the end of the batch.
    _now_total = (time.time() - start_total) + prev_total_seconds
    stats["total_time_seconds"] = round(_now_total, 1)
    _n_done = stats["completed"] + stats["failed"]
    stats["avg_time_per_issue_seconds"] = round(_now_total / max(_n_done, 1), 2)
    stats["last_updated"] = datetime.now().isoformat()
    save_checkpoint(processed, stats)
    return n_processed_batch


def main():
    parser = argparse.ArgumentParser(description="Phase 5 — Build Dataset")
    parser.parse_args()

    print("=" * 70)
    print("PHASE 5 — Build Dataset")
    print(f"Start: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📁 Current experiment: {paths.experiment_id}")
    if MIN_LANGUAGE_FILES > 0 or MIN_LANGUAGE_FRACTION > 0.0:
        print("🔍 PR-level language filter ACTIVE: "
              f"min_language_files={MIN_LANGUAGE_FILES}, "
              f"min_language_fraction={MIN_LANGUAGE_FRACTION*100:.0f}%")
    else:
        print("🔍 PR-level language filter: DISABLED (legacy default)")
    print("=" * 70)

    paths.ensure_step_dir(5)

    # Load the repo-to-language map from phase 1.
    if not os.path.exists(REPO_FILE):
        print(f"❌ {REPO_FILE} not found. Run phase 1 first.")
        sys.exit(1)
    repo_lang_map = _load_repo_lang_map()
    print(f"📂 Language map loaded: {len(repo_lang_map)} repos")

    if not os.path.exists(INPUT_FILE):
        print(f"❌ {INPUT_FILE} not found. Run phase 4 first.")
        sys.exit(1)

    issues = _load_input_phase5()
    print(f"📂 Validated issues loaded: {len(issues)}")

    java_count   = sum(1 for i in issues if repo_lang_map.get(i["repo"], "").lower() == "java")
    python_count = sum(1 for i in issues if repo_lang_map.get(i["repo"], "").lower() == "python")
    other_count  = len(issues) - java_count - python_count
    print(f"   ├─ Java:   {java_count}")
    print(f"   ├─ Python: {python_count}")
    if other_count:
        print(f"   └─ Other:  {other_count}")

    processed, prev_stats = load_checkpoint()
    if processed:
        print(f"🔄 Checkpoint found: {len(processed)} issues already processed — resuming")

    start_total = time.time()

    # When resuming, build the stats dict from the persisted state so the
    # cumulative counters cover all previous jobs, not just this one.
    if prev_stats:
        stats = dict(prev_stats)
        stats["total_input"]        = len(issues)
        stats["skipped_checkpoint"] = len(processed)
        # Keep the original "started" timestamp; last_updated is refreshed
        # at every checkpoint.
        print(f"📂 RESUME: restored stats "
              f"(completed={stats.get('completed', 0)}, "
              f"permanent_skip={stats.get('permanent_skip', 0)}, "
              f"failed={stats.get('failed', 0)}, "
              f"prior_elapsed={stats.get('total_time_seconds', 0)}s)")
    else:
        stats = {
            "total_input":        len(issues),
            "completed":          0,
            "skipped_checkpoint": len(processed),
            "permanent_skip":     0,    # Issues that must not be retried (404, language, etc.).
            "failed":             0,    # Only transient errors that can be retried.
            "started":               datetime.now().isoformat(),
            "last_updated": None,
            "total_time_seconds": 0,
            "avg_time_per_issue_seconds": 0,
        }
    # Baseline of cumulative elapsed time before this session.
    _prev_total_seconds = float(stats.get("total_time_seconds", 0) or 0)

    # Execution: single sequential pass.
    process_batch_phase5(issues, repo_lang_map, processed, stats,
                         start_total, _prev_total_seconds)

    # Finalize stats: total elapsed = this session plus previous jobs.
    elapsed_total = (time.time() - start_total) + _prev_total_seconds
    stats["total_time_seconds"] = round(elapsed_total, 1)
    n_proc = stats["completed"] + stats["failed"]
    stats["avg_time_per_issue_seconds"] = round(elapsed_total / max(n_proc, 1), 2)
    stats["last_updated"] = datetime.now().isoformat()
    save_checkpoint(processed, stats)

    print("\n" + "=" * 70)
    print("COMPLETED")
    print(f"  Total input:           {stats['total_input']}")
    print(f"  Completed:             {stats['completed']}")
    print(f"  Skip (checkpoint):     {stats['skipped_checkpoint']}")
    print(f"  Permanent skips:       {stats.get('permanent_skip', 0)}  "
          "(PR 404, PR-level language filter, unsupported language)")
    print(f"  Failed (transient):    {stats['failed']}")

    lang_counts = {}
    for lang in ("java", "python"):
        lang_dir = os.path.join(OUTPUT_DIR, lang)
        if os.path.isdir(lang_dir):
            folders = [d for d in os.listdir(lang_dir) if os.path.isdir(os.path.join(lang_dir, d))]
            lang_counts[lang] = len(folders)
            print(f"  {lang.capitalize():6} folders: {len(folders)}")
        else:
            lang_counts[lang] = 0

    print("=" * 70)

    # Aggregate statistics over the final dataset.
    print("\n📊 Aggregating statistics on the final dataset...")

    files_per_pr = []
    additions_per_pr = []
    deletions_per_pr = []
    diff_size_buckets = {"<10": 0, "10-100": 0, "100-500": 0, "500-1000": 0, "1000+": 0}
    extensions_counts = {}
    issues_per_strategy_dataset = {}
    issue_per_repo_dataset = {}

    for lang in ("java", "python"):
        lang_dir = os.path.join(OUTPUT_DIR, lang)
        if not os.path.isdir(lang_dir):
            continue
        for repo_folder in os.listdir(lang_dir):
            repo_path = os.path.join(lang_dir, repo_folder)
            if not os.path.isdir(repo_path):
                continue
            for issue_folder in os.listdir(repo_path):
                issue_path = os.path.join(repo_path, issue_folder)
                if not os.path.isdir(issue_path) or not issue_folder.startswith("issue_"):
                    continue
                data_file = os.path.join(issue_path, "data.json")
                if not os.path.exists(data_file):
                    continue
                try:
                    with open(data_file, "r", encoding="utf-8") as f:
                        d = json.load(f)
                except Exception:
                    continue

                strat = d.get("discovery_strategy", "UNKNOWN")
                issues_per_strategy_dataset[strat] = issues_per_strategy_dataset.get(strat, 0) + 1

                repo = d.get("repository", "")
                if repo:
                    issue_per_repo_dataset[repo] = issue_per_repo_dataset.get(repo, 0) + 1

                pr = d.get("primary_pr") or {}
                add = pr.get("additions", 0)
                dele = pr.get("deletions", 0)
                n_files = pr.get("changed_files", 0)

                if n_files > 0:
                    files_per_pr.append(n_files)
                if add > 0 or dele > 0:
                    additions_per_pr.append(add)
                    deletions_per_pr.append(dele)

                    total_diff = add + dele
                    if total_diff < 10:
                        diff_size_buckets["<10"] += 1
                    elif total_diff < 100:
                        diff_size_buckets["10-100"] += 1
                    elif total_diff < 500:
                        diff_size_buckets["100-500"] += 1
                    elif total_diff < 1000:
                        diff_size_buckets["500-1000"] += 1
                    else:
                        diff_size_buckets["1000+"] += 1

                diff = d.get("diff_between_base_and_last", []) or []
                for f_entry in diff:
                    fname = f_entry.get("filename", "")
                    if "." in fname:
                        ext = "." + fname.rsplit(".", 1)[-1].lower()
                        if len(ext) <= 10:  # Drop anomalously long extensions.
                            extensions_counts[ext] = extensions_counts.get(ext, 0) + 1

    distribution_files_per_pr = {
        "min": min(files_per_pr) if files_per_pr else 0,
        "max": max(files_per_pr) if files_per_pr else 0,
        "mean": round(sum(files_per_pr) / max(len(files_per_pr), 1), 1),
        "median": median(files_per_pr),
        "sample_size": len(files_per_pr),
    }

    distribution_additions = {
        "min": min(additions_per_pr) if additions_per_pr else 0,
        "max": max(additions_per_pr) if additions_per_pr else 0,
        "mean": round(sum(additions_per_pr) / max(len(additions_per_pr), 1), 1),
        "median": median(additions_per_pr),
    }

    distribution_deletions = {
        "min": min(deletions_per_pr) if deletions_per_pr else 0,
        "max": max(deletions_per_pr) if deletions_per_pr else 0,
        "mean": round(sum(deletions_per_pr) / max(len(deletions_per_pr), 1), 1),
        "median": median(deletions_per_pr),
    }

    top_20_extensions = [
        {"extension": e, "count": c}
        for e, c in sorted(extensions_counts.items(), key=lambda kv: -kv[1])[:20]
    ]

    top_10_repo_dataset = [
        {"repo": r, "issue": n}
        for r, n in sorted(issue_per_repo_dataset.items(), key=lambda kv: -kv[1])[:10]
    ]

    stats["count_by_language"] = lang_counts
    stats["issues_per_discovery_strategy"] = issues_per_strategy_dataset
    stats["distribution_files_per_pr"] = distribution_files_per_pr
    stats["distribution_additions_per_pr"] = distribution_additions
    stats["distribution_deletions_per_pr"] = distribution_deletions
    stats["distribution_diff_size"] = diff_size_buckets
    stats["top_20_file_extensions"] = top_20_extensions
    stats["top_10_repo_per_issue"] = top_10_repo_dataset
# Uniform fields for external consumers.
    stats["_schema_version"] = 1
    stats["_phase"] = 5

    # Issues still unprocessed after retries are dropped and logged.
    _lost_items_p5 = [
        f"{i['repo']}#{i['number']}" for i in issues
        if f"{i['repo']}#{i['number']}" not in processed
    ]
    stats["_lost_items"] = _lost_items_p5

    print(f"  Files per PR (avg):    {distribution_files_per_pr['mean']}")
    print(f"  Average additions:     {distribution_additions['mean']}")
    print(f"  Average deletions:     {distribution_deletions['mean']}")
    print(f"  Unique extensions:     {len(extensions_counts)}")

    stats_path = os.path.join(OUTPUT_DIR, "_run_stats.json")
    dump_json_atomic(stats, stats_path)
    print(f"\n📊 Statistics saved to {stats_path}")
    # Alias without the leading underscore, kept alongside _run_stats.json.
    run_stats_alias_path = os.path.join(OUTPUT_DIR, "run_stats.json")
    dump_json_atomic(stats, run_stats_alias_path)

    # Transient failures are logged and the phase finishes without
    # blocking the pipeline.
    if _lost_items_p5:
        examples = _lost_items_p5[:10]
        print()
        print("=" * 70)
        print(f"⚠️  PHASE 5: {len(_lost_items_p5)} issues were NOT built "
              f"(transient errors) and were dropped. The dataset is PARTIAL.")
        print(f"   Examples: {examples}")
        print("   Policy: continue anyway (lost data logged in run_stats._lost_items).")
        print("=" * 70)

    print("🏁 Phase 5 complete. Pipeline finished.")


if __name__ == "__main__":
    main()