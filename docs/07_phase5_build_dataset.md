# 07 — Phase 5: Build Dataset

> The final phase. For every validated issue, fetch the PR's source
> code (before and after), the commit chain, the merge info, and
> write a self-contained folder per issue. Slow, checkpoint-heavy,
> Windows-MAX_PATH-aware.

## 1. Goal

Phase 5 turns the metadata-only output of phase 4 into a
**self-contained dataset on disk**. For every validated issue it
produces a folder containing:

- `data.json` — full record (issue + PR + commits + merge + diff
  summary + everything from upstream phases).
- `original_files/` — source files at the PR's base SHA.
- `modified_files/` — same files at the PR's head SHA.

The output lives under `experiments/<id>/5_dataset/<language>/<owner>_<repo>/issue_<number>/`.

This is the **deliverable** of the pipeline. The other four phases
produce intermediate JSON; phase 5 produces the dataset you actually
publish.

## 2. Input and output at a glance

**Input:** `4_ai_validation/validated_issues.json` — produced by phase 4.

**Output (per issue):**

```
5_dataset/
├── java/
│   └── AxonIQ_AxonFramework/
│       └── issue_3879/
│           ├── data.json
│           ├── original_files/
│           │   ├── common__src__main__java__Property.java
│           │   └── ...
│           └── modified_files/
│               ├── common__src__main__java__Property.java
│               └── ...
├── python/
│   └── ...same structure...
├── _checkpoint.json
└── _run_stats.json
```

On a typical 100-repo run: ~750 issues processed, ~8000 file pairs
written, ~3 hours wall-clock.

## 3. The per-issue workflow

For each validated issue, `build_issue_folder(issue, repo_lang_map, output_dir)`:

1. **Pick the primary PR.** If the issue has multiple linked PRs (from
   phase 3), pick the merged one. If multiple are merged, pick the
   one with the most file changes.
2. **Fetch PR details** via REST: `base.sha`, `head.sha`,
   `merge_commit_sha`, `additions`, `deletions`, `changed_files`,
   branch names.
3. **Fetch the commit list** via REST: every commit between `base` and
   `head`, with author, date, message.
4. **Fetch the merge commit info** if the PR was merged.
5. **Fetch the file diff** via REST `/pulls/{n}/files`: per-file
   `status`, `additions`, `deletions`, `patch`, `blob_url`, `raw_url`.
6. **For every modified file, fetch the source code twice:**
   - `get_file_content(owner, repo, path, base_sha)` → goes to
     `original_files/`.
   - `get_file_content(owner, repo, path, head_sha)` → goes to
     `modified_files/`.
7. **Write `data.json`** with the consolidated record.

Each step hits the GitHub REST API, so the rate limiter is engaged
constantly. With four file-content fetches per file and ~10 files per
issue, a single issue can easily use ~40 API calls.

## 4. The `data.json` "fat record"

This is the heart of the dataset. Its top-level keys are:

```
repository, issue_number, issue_title, issue_body, issue_url,
issue_author, issue_state, issue_created_at, issue_closed_at,
issue_labels, discovery_strategy, link_source,

ai_validation,            ← copied verbatim from phase 4

primary_pr,               ← chosen PR with all metadata
base_sha, last_commit_sha, merge_commit_sha,
merge_info,               ← mergeable/had_conflicts/state

timeline,                 ← computed deltas issue↔PR

diff_between_base_and_last,  ← list of file-level diffs
commits,                  ← list of commits in the PR
all_linked_prs,           ← all PRs from phase 3, not just primary

pull_request_url, commits_url, files_url,
language, dataset_build_date
```

A few non-obvious points:

- **`timeline.delta_days_issue_closed_to_pr_merged` can be negative.**
  Happens when the PR is merged before the issue is officially closed
  (e.g. issue stays open as tracker, PR ships separately).
- **`merge_info.had_conflicts` is often `null`.** GitHub stops exposing
  the mergeability state once the PR is merged. The field is included
  for completeness but should not be relied on for analysis.
- **`primary_pr.source`** is one of `closing`, `cross_reference`,
  `timeline_event` — same provenance tags as phase 3. Useful to filter
  out the noisier `timeline_event` links if needed.

## 5. File path flattening

GitHub repos have deep file paths
(`common/src/main/java/org/axonframework/common/property/Property.java`).
Writing them verbatim under `original_files/` would recreate the whole
directory tree per issue, which is wasteful and risks name collisions
on different repos.

The convention is to flatten `/` to `__`:

```python
safe_name = filename.replace("/", "__")
# common/src/main/java/org/axonframework/common/property/Property.java
# becomes
# common__src__main__java__org__axonframework__common__property__Property.java
```

The mapping is one-way (you can recover the original path by replacing
back) and avoids any collision between files with the same basename in
different subdirectories. The dataset becomes flat, easier to grep, and
plays nicer with tools that expect flat directories.

## 6. The Windows MAX_PATH workaround

The flattening creates very long filenames. On Windows, the default
`MAX_PATH` is 260 characters (including drive letter, full path,
filename, null terminator). When the full path
`experiments/<exp_id>/5_dataset/<lang>/<owner>_<repo>/issue_<n>/modified_files/<flat_name>`
exceeds 260 chars, `os.makedirs` or `open()` crash with
`OSError [Errno 22]`.

Before the fix, this caused a particularly bad failure mode: the
crashing issue was retried forever (`RESULT_RETRY_LATER`), saturating
the GitHub rate limit and never making progress.

The current solution truncates the filename when needed, while keeping
the extension and adding a short MD5 hash for uniqueness:

```python
def _shorten_if_needed(safe: str, base_folder: str, subdir: str) -> str:
    full = os.path.join(base_folder, subdir, safe)
    if len(full) <= 250:        # 10-char margin
        return safe

    max_safe_len = 250 - len(os.path.join(base_folder, subdir)) - 1
    if max_safe_len < 30:
        return None              # signal "skip this file"

    root, ext_part = os.path.splitext(safe)
    h = hashlib.md5(safe.encode("utf-8")).hexdigest()[:8]
    head_len = max_safe_len - len(ext_part) - 1 - len(h)
    if head_len < 10:
        return None

    return f"{root[:head_len]}_{h}{ext_part}"
```

The hash makes the result deterministic across runs (the same input
file always produces the same shortened name), and unique enough to
avoid collisions between two long-path files that happen to share a
prefix.

When even truncation isn't enough (the base folder itself is too
long), the file is skipped and counted in `total_skipped_files`.

## 7. Checkpointing

Phase 5 is the most time-consuming phase (hours on a 100-repo run).
A naive restart would re-fetch and re-write everything from scratch.

The checkpoint mechanism saves progress to `_checkpoint.json` every N
issues:

```python
CHECKPOINT_EVERY = 10

def save_checkpoint(processed: set, stats: dict):
    with open(CHECKPOINT_FILE, "w") as f:
        json.dump({
            "processed": sorted(processed),
            "stats": stats,
        }, f, indent=2)
```

The format is simply a list of `<repo>#<number>` strings:

```json
{
  "processed": [
    "bcgov/STRR#1142",
    "Genesis-Embodied-AI/genesis-world#1365",
    "HHS/simpler-grants-gov#8596",
    ...
  ],
  "stats": { ... }
}
```

On startup, the phase reads the checkpoint and skips issues already
processed:

```python
def load_checkpoint() -> set:
    if not os.path.exists(CHECKPOINT_FILE):
        return set()
    with open(CHECKPOINT_FILE) as f:
        return set(json.load(f).get("processed", []))
```

In addition to `processed`, the script tracks `permanent_skip` — issues
that failed in a non-retryable way (e.g. PR deleted from GitHub, repo
made private). These are recorded so they are not retried on next run.

## 8. Binary file handling

Some PR diffs include binary files (PNGs, PDFs, compiled archives).
Trying to fetch them as text and JSON-encode them would either bloat
the dataset or crash on encoding errors:

```python
def is_binary_extension(filename: str) -> bool:
    """Returns True for extensions that should not be fetched as source."""
```

Binary files are listed in `diff_between_base_and_last` for
completeness (filename, status, blob_url) but their content is not
fetched. The `skip_reason` field on the diff entry records why.

## 9. Statistics

`5_dataset/_run_stats.json`:

```json
{
  "_schema_version": 1, "_phase": 5,
  "total_input": 779,
  "completed": 753,
  "skipped_checkpoint": 0,
  "permanent_skip": 26,
  "failed": 0,
  "total_time_seconds": 8030.5,
  "avg_time_per_issue_seconds": 10.66,
  "count_by_language": { "java": 23, "python": 42 },
  "issues_per_discovery_strategy": {
    "MENTION": 80, "CONNEXTRA": 187, "TAG": 61, "LABEL": 366, "TEMPLATE": 59
  },
  "distribution_files_per_pr": {
    "min": 1, "max": 1620, "mean": 34.0, "median": 11.0
  },
  "distribution_additions_per_pr": {
    "median": 342.0, "max": 424370
  },
  "distribution_deletions_per_pr": {
    "median": 45.0, "max": 272033
  },
  "distribution_diff_size": {
    "<10": 23, "10-100": 132, "100-500": 254, "500-1000": 130, "1000+": 213
  },
  "top_20_file_extensions": [
    { "extension": ".py", "count": 3344 },
    { "extension": ".java", "count": 2463 },
    ...
  ],
  "total_skipped_files": 8815
}
```

Worth flagging:

- **`distribution_diff_size`** spans a huge range. Median ~340 added
  lines but max 424,370. The dataset has a long tail of giant PRs.
- **`total_skipped_files`** is non-trivial (8800 in the benchmark).
  Most are binary files (`.png`, `.pdf`) and Windows-MAX_PATH cases.
- **`top_20_file_extensions`** is dominated by the target languages
  (Python, Java, TypeScript) but also includes config files (`.yml`,
  `.json`) that are part of the PRs.

## 10. Configuration

Phase 5 has no per-experiment configuration in `config.json`. Its
constants are at the top of `5_build_dataset.py`:

```python
CHECKPOINT_EVERY = 10
CHECKPOINT_FILE  = str(paths.step5_checkpoint_json)
```

The phase respects the global rate limiter and the experiment paths
the same way other phases do. The `_run_stats.json` and `run_stats.json`
files are duplicates — `run_stats.json` exists for backward
compatibility with the dashboard's earlier file-naming conventions.

## 11. What the dataset looks like end-to-end

After phase 5 completes, the deliverable artifact is the
`5_dataset/<lang>/<owner>_<repo>/issue_<n>/` folder. Each folder is
self-contained: `data.json` carries all the metadata, and the two
`*_files/` subdirectories carry the source code state before and
after the change.

A researcher loading the dataset can:

```python
from pathlib import Path
import json

DATASET = Path("experiments/<id>/5_dataset")

for data_file in DATASET.rglob("data.json"):
    record = json.loads(data_file.read_text())
    # record["ai_validation"]["connextra"] == "complete"
    # record["primary_pr"]["url"] == "..."
    # ...

    for orig in (data_file.parent / "original_files").glob("*"):
        modified = data_file.parent / "modified_files" / orig.name
        if modified.exists():
            diff = compute_diff(orig.read_text(), modified.read_text())
            ...
```

The flat directory + `data.json` design makes the dataset trivially
shardable, packable into archives, and queryable with off-the-shelf
JSON tools.
