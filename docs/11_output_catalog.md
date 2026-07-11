# 11 — Output Catalog

> Exhaustive catalog of every file the pipeline writes. For each output:
> phase, producing script, format, schema, real-world example, typical
> volume, downstream consumers.

## Legend

| Symbol | Meaning |
|--------|---------|
| [F] | **Final output** — part of the deliverable dataset |
| [I] | **Intermediate** — consumed by the next phase |
| [S] | **Statistics** — `run_stats.json`, used by the dashboard |
| [L] | **Log file** — human-readable, for debugging |
| [D] | **Debug** — diagnostic dumps, safe to ignore for the final result |
| [C] | **Coordination** — coordination/checkpoint marker |

## General map

Every experiment lives under `experiments/<experiment_id>/`. The layout
below is taken from a real run (`run_statistica_prova_100Repo`, 99
repositories processed):

```
experiments/run_statistica_prova_100Repo/
├── config.json                          [C]  experiment parameters (immutable once set)
├── pipeline_state.json                  [C]  per-phase done flags + timestamps
├── _runtime.json                        [C]  PID + timestamp of the running run
├── _done.txt                            [C]  marker written only when all 5 phases complete
├── _pipeline.log                        [L]  unified log of all phases (~2.5 MB on a 100-repo run)
├── _bootstrap.log                       [L]  bootstrap log (orchestrator startup messages)
├── _logs/
│   ├── phase_1.log                      [L]  ~1 MB
│   ├── phase_2.log                      [L]  ~440 KB
│   ├── phase_3.log                      [L]  ~12 KB
│   ├── phase_4.log                      [L]  ~315 KB
│   └── phase_5.log                      [L]  ~220 KB
│
├── 1_discovery/
│   ├── repo_user_story.json             [I]  list of accepted repos
│   ├── run_stats.json                   [S]  phase 1 stats
│   └── DEBUG_tag_discarded_regex.json   [D]  TAG-discarded titles (~5.5 MB)
│
├── 2_issue_discovery/
│   ├── issues_user_story.json           [I]  all issues found (one record per issue)
│   └── run_stats.json                   [S]  phase 2 stats
│
├── 3_pr_linking/
│   ├── issues_with_pr.json              [I]  issues that resolved to at least one PR
│   ├── issues_without_pr.json           [I]  issues with no closing PR found
│   └── run_stats.json                   [S]  phase 3 stats
│
├── 4_ai_validation/
│   ├── validated_issues.json            [I]  issues confirmed to be user stories by Ollama
│   ├── discarded_issues_ai.json         [I]  issues rejected by Ollama
│   └── run_stats.json                   [S]  phase 4 stats
│
└── 5_dataset/
    ├── _checkpoint.json                 [C]  resume marker (list of processed issues)
    ├── _run_stats.json                  [S]  phase 5 stats
    ├── run_stats.json                   [S]  duplicate of _run_stats.json (legacy)
    ├── java/
    │   └── <owner>_<repo>/
    │       └── issue_<number>/
    │           ├── data.json            [F]  full issue+PR+commits+merge record
    │           ├── original_files/      [F]  source files at base SHA
    │           └── modified_files/      [F]  source files at head SHA
    └── python/
        └── ...same structure...
```

---

## Phase 1 — Discovery

### [I] `1_discovery/repo_user_story.json`

**Format:** JSON array. Each element is a candidate repository.

**Schema:**

```json
{
  "name": "Netcracker/qubership-dbaas",
  "url": "https://github.com/Netcracker/qubership-dbaas",
  "stars": 3,
  "language": "Java",
  "primary_language": "Java",
  "languages_pct": {
    "Java": 90.71,
    "Go": 7.43,
    "Makefile": 1.14
  },
  "created_at": "2024-12-27",
  "strategy": "LABEL",
  "query_match": "label:\"user story\" is:issue is:closed",
  "issue_trigger": {
    "type": "issue",
    "number": 213,
    "title": "Logical Backup and Restore with filtering and additional operations",
    "url": "https://github.com/Netcracker/qubership-dbaas/issues/213"
  }
}
```

Field meanings:
- `strategy` — which discovery strategy accepted this repository (one of
  `LABEL`, `TEMPLATE`, `TAG`, `MENTION`, `CONNEXTRA`). Each repo appears
  under exactly one strategy (global deduplication).
- `query_match` — the exact GitHub query that found the repo.
- `issue_trigger` — the specific issue (or file, for TEMPLATE) that led
  to discovering the repo.
- `languages_pct` — percentage breakdown of source languages, used by
  the language filter (`any_above_threshold` mode).

**Typical volume:** ~100 entries on a "100 repo" run; ~75 KB.

**Consumed by:** phase 2 (`issues_user_story.json` builder).

### [S] `1_discovery/run_stats.json`

Schema sketch (top-level keys):

```json
{
  "_schema_version": 1,
  "_phase": 1,
  "total_time_seconds": 687.0,
  "timestamp": "2026-05-19 11:38:05",
  "configuration": { ... full discovery_config snapshot ... },
  "global_totals": {
    "unique_repos_seen": 1568,
    "accepted_repos": 99,
    "discarded_stars": 917,
    "discarded_date": 177,
    "discarded_language": 360,
    "discarded_regex": 11658
  },
  "per_strategy": {
    "LABEL":    { "accepted": 20, "time_seconds": 117.3, ... },
    "TEMPLATE": { "accepted": 20, "time_seconds": 38.0, ... },
    "TAG":      { "accepted": 19, "time_seconds": 383.2, ... },
    "MENTION":  { "accepted": 20, "time_seconds": 80.1, ... },
    "CONNEXTRA":{ "accepted": 20, "time_seconds": 68.5, ... }
  },
  "acceptance_rates_per_strategy_pct": { ... },
  "stars_distribution": { ... },
  "creation_year_distribution": { ... },
  "language_distribution": { "Python": 63, "Java": 36 },
  "top_10_repo_by_stars": [ ... ]
}
```

**Consumed by:** the dashboard page `1_Discovery.py`.

### [D] `1_discovery/DEBUG_tag_discarded_regex.json`

Diagnostic dump of titles discarded by the `REGEX_TAG_COMBINED` regex
inside the TAG strategy. Useful to tune the regex if too many "real" user
stories are being thrown away. Safe to delete after analysis. Large
(several MB on big runs).

---

## Phase 2 — Issue Extraction

### [I] `2_issue_discovery/issues_user_story.json`

**Format:** JSON array. One element per issue extracted from a repo found
in phase 1.

**Schema:**

```json
{
  "number": 935,
  "title": "Specify start and end date for bulk CVE ingestion",
  "body": "As a contributor to the security tracker, for testing locally I don't want to download all CVEs from the beginning of history, but only for a time bracket.\n\n...",
  "labels": ["good first issue", "user story", "contributor experience"],
  "url": "https://github.com/NixOS/nix-security-tracker/issues/935",
  "author": "fricklerhandwerk",
  "state": "closed",
  "created_at": "2026-03-25",
  "closed_at": "2026-03-30",
  "repo": "NixOS/nix-security-tracker",
  "strategy": "LABEL"
}
```

Field meanings:
- `strategy` — which discovery strategy of **phase 2** matched this
  issue (independent from the strategy that found the repo in phase 1).
- `created_at` / `closed_at` — formatted as `YYYY-MM-DD`. Used in phase
  5 to compute `delta_days_issue_closed_to_pr_merged`.

**Typical volume:** ~2,500 entries on a 100-repo run; ~4.4 MB.

**Consumed by:** phase 3 (PR linking).

### [S] `2_issue_discovery/run_stats.json`

Top-level keys:

```json
{
  "_schema_version": 1, "_phase": 2,
  "total_time_seconds": 2377.7,
  "avg_time_per_repo_seconds": 24.02,
  "configuration": { "max_issues_per_repo": 40, ... },
  "totals": {
    "processed_repos": 99,
    "issues_found": 2464,
    "discarded_regex": 1970,
    "empty_repos": 3
  },
  "per_strategy": { "LABEL": {...}, "TAG": {...}, ... },
  "issues_per_repo_distribution": { "min": 0, "max": 40, "mean": 24.89, ... },
  "top_10_repo_per_issue": [ ... ]
}
```

---

## Phase 3 — PR Linking

### [I] `3_pr_linking/issues_with_pr.json`

**Format:** JSON array. Each issue is enriched with one or more PR
records. Pure "issue had at least one closing PR" cases.

**Schema:**

```json
{
  "repo": "NixOS/nix-security-tracker",
  "number": 935,
  "title": "Specify start and end date for bulk CVE ingestion",
  "body": "As a contributor to the security tracker, ...",
  "labels": ["good first issue", "user story", "contributor experience"],
  "url": "https://github.com/NixOS/nix-security-tracker/issues/935",
  "author": "fricklerhandwerk",
  "state": "closed",
  "created_at": "2026-03-25",
  "closed_at": "2026-03-30",
  "strategy": "LABEL",
  "pr": [
    {
      "number": 955,
      "title": "feat: date-based CVE ingestion filtering",
      "url": "https://github.com/NixOS/nix-security-tracker/pull/955",
      "merged": true,
      "merged_at": "2026-03-30T10:38:01Z",
      "state": "MERGED",
      "source": "cross_reference"
    }
  ],
  "link_source": "graphql"
}
```

Field meanings:
- `pr` — list of PRs that closed/referenced this issue. The first one is
  the "primary" (most-merged-first ordering). An issue can have multiple
  PRs if several contributors attempted the fix.
- `pr[].source` — provenance: `cross_reference` for GraphQL's
  `closingIssuesReferences`, `timeline_event` for `REFERENCED_EVENT`
  fallback (Round 2 of phase 3).
- `link_source` — at the issue level: `graphql` (Round 1) or
  `graphql_r2` (Round 2). Always GraphQL.

**Typical volume:** ~1,200 entries on a 100-repo run; ~3.0 MB.

**Consumed by:** phase 4 (AI validation).

### [I] `3_pr_linking/issues_without_pr.json`

Same schema as above **minus** the `pr` and `link_source` fields. These
issues are dropped from the downstream pipeline but kept for ablation
analysis on the dashboard.

**Typical volume:** ~1,300 entries on a 100-repo run; ~300 KB.

### [S] `3_pr_linking/run_stats.json`

```json
{
  "_schema_version": 1, "_phase": 3, "version": "graphql-p1",
  "totals": {
    "input_issues": 2464,
    "issues_with_pr": 1177,
    "issues_without_pr": 1287,
    "success_rate_pct": 47.8,
    "total_prs_found": 1630
  },
  "per_pass": {
    "P1_graphql": { "resolved_issues": 1177, "issue_round2_extra": 78, ... }
  },
  "prs_per_issue_distribution": { "1": 903, "2": 170, "3": 57, "4": 19, "5+": 28 },
  "time_issue_to_merge": { "mean_days": 62.8, "median_days": 20, ... }
}
```

---

## Phase 4 — AI Validation

### [I] `4_ai_validation/validated_issues.json`

**Format:** JSON array. Each element is an issue from phase 3 enriched
with the AI validation result.

**Schema:**

```json
{
  "repo": "NixOS/nix-security-tracker",
  "number": 922,
  "title": "Management command for deleting matches older than 1 year",
  "body": "As a security team member, I don't care about CVEs older than a year. ...",
  "labels": ["good first issue", "user story", "data"],
  "url": "...",
  "author": "fricklerhandwerk",
  "state": "closed",
  "created_at": "2026-03-24",
  "closed_at": "2026-03-25",
  "strategy": "LABEL",
  "pr": [ ... same as phase 3 ... ],
  "link_source": "graphql",
  "ai_validation": {
    "is_user_story": true,
    "connextra": "complete",
    "role": "security team member",
    "feature": "delete matches older than 1 year using a management command",
    "benefit": "arrive at an empty queue and ensure no old data remains",
    "location": "body",
    "metadata": {
      "gherkin": true,
      "acceptance_criteria": true,
      "other_format": false
    },
    "confidence": "high"
  },
  "_validation_path": "cot"
}
```

Field meanings:
- `ai_validation.connextra` — `complete` (all three parts present),
  `partial` (only two), `none` (Ollama did not identify a US, but the
  issue still made it past validation because of metadata).
- `ai_validation.location` — where Ollama found the user story:
  `title`, `body`, `both`, or `unspecified`.
- `_validation_path` — `cot` (Chain-of-Thought succeeded on first try)
  or `fallback` (CoT failed parsing, the simpler fallback prompt was
  used).

**Typical volume:** ~780 entries on a 100-repo run; ~2.1 MB.

**Consumed by:** phase 5 (dataset building).

### [I] `4_ai_validation/discarded_issues_ai.json`

Same schema as `validated_issues.json` but for issues Ollama rejected.
Kept for ablation analysis.

**Typical volume:** ~400 entries on a 100-repo run; ~1.4 MB.

### [S] `4_ai_validation/run_stats.json`

```json
{
  "_schema_version": 3, "_phase": 4,
  "model": "qwen-gpu",
  "endpoint": "http://localhost:11434 (Ollama local)",
  "prompt_version": "chain-of-thought+few-shot-v6-replace+smart-truncate+enum-strict+fallback-cap",
  "wall_clock_seconds": 12545.6,
  "totals": {
    "input_issues": 1177,
    "validated_issues": 779,
    "discarded_issues": 397,
    "ollama_errors": 1,
    "errors_by_category": { "json_decode_error": 226, "schema_invalid": 1, ... },
    "validation_rate_pct": 66.2
  },
  "validation_path": {
    "cot_total": 701, "fallback_total": 475, "failed_total": 1,
    "validated_per_path": { "cot": 503, "fallback": 276 },
    "cot_success_rate_pct": 59.6
  },
  "connextra_distribution": { "complete": 405, "partial": 315 },
  "confidence_distribution": { "high": 400, "medium": 377, "low": 2 },
  "validation_rates_per_strategy": {
    "LABEL":     { "rate_pct": 60.5 },
    "CONNEXTRA": { "rate_pct": 82.0 },
    ...
  },
  "top_10_roles": [ { "role": "user", "count": 144 }, ... ],
  "top_10_action_verbs": [ { "verb": "see", "count": 30 }, ... ]
}
```

---

## Phase 5 — Build Dataset (final output)

This is the **deliverable** of the pipeline. One folder per issue, with
the full code context.

### [F] `5_dataset/<language>/<owner>_<repo>/issue_<number>/data.json`

The "fat record": everything we know about the issue, the PR, the merge
and the diff.

**Schema (top-level keys):**

```json
{
  "repository": "AxonIQ/AxonFramework",
  "issue_number": 3879,
  "issue_title": "Provide kotlin extension for 5.0",
  "issue_body": "As a kotlin developer using the axon-framework, ...",
  "issue_url": "https://github.com/AxonIQ/AxonFramework/issues/3879",
  "issue_author": "jangalinski",
  "issue_state": "closed",
  "issue_created_at": "2025-11-07",
  "issue_closed_at": "2026-04-28",
  "issue_labels": ["Priority 2: Should"],

  "discovery_strategy": "CONNEXTRA",
  "link_source": "graphql",

  "ai_validation": { ... same as phase 4 ... },

  "primary_pr": {
    "number": 4358,
    "title": "chore: make Property#get method return nullable value",
    "url": "...",
    "merged_at": "2026-03-24T16:32:47Z",
    "source": "cross_reference",
    "source_branch": "chore/property-should-be-nullable",
    "base_branch": "main",
    "additions": 56,
    "deletions": 20,
    "changed_files": 3
  },

  "base_sha": "661df35a3e830b299ec1e85effb0cd64e4dde7b7",
  "last_commit_sha": "04b0f0b14c31f4a932c9f2758de948a5ed451f62",
  "merge_commit_sha": "ff1a6c4a69b78f4e72cb4ab394e4c8045b244ba4",

  "merge_info": {
    "merge_commit_sha": "...",
    "merged": true,
    "mergeable": null,
    "mergeable_state": "unknown",
    "had_conflicts": null
  },

  "timeline": {
    "issue_created_at": "2025-11-07",
    "issue_closed_at": "2026-04-28",
    "pr_merged_at": "2026-03-24T16:32:47Z",
    "delta_days_issue_closed_to_pr_merged": -35
  },

  "diff_between_base_and_last": [
    {
      "sha": "...",
      "filename": "common/src/main/java/.../Property.java",
      "status": "modified",
      "additions": 3,
      "deletions": 1,
      "changes": 4,
      "blob_url": "...",
      "raw_url": "..."
    }
  ],

  "commits": [ ... ],
  "all_linked_prs": [ ... ],
  "pull_request_url": "...",
  "commits_url": "...",
  "files_url": "...",
  "language": "java",
  "dataset_build_date": "2026-05-19T15:44:..."
}
```

Field meanings (selected):
- `primary_pr` — the chosen "best" PR among all linked ones (typically
  the merged one with the most changes).
- `merge_info.had_conflicts` — `null` when GitHub does not expose it
  (typical after merge has already happened); otherwise `true`/`false`.
- `timeline.delta_days_issue_closed_to_pr_merged` — can be **negative**
  when the PR was merged before the issue was officially closed (common
  case: issue stays open as tracker, PR ships separately).
- `diff_between_base_and_last` — file-by-file summary of the changes
  between `base_sha` and `last_commit_sha`. The full source code lives
  next to this file under `original_files/` and `modified_files/`.

### [F] `5_dataset/<language>/<owner>_<repo>/issue_<number>/original_files/`

Source files at `base_sha`. Filenames flatten the original repo path
using `__` as separator (e.g. `common__src__main__java__org__axonframework__common__property__Property.java`
corresponds to `common/src/main/java/org/axonframework/common/property/Property.java`).
This avoids name collisions between files with the same basename living
in different directories.

### [F] `5_dataset/<language>/<owner>_<repo>/issue_<number>/modified_files/`

Same files at `last_commit_sha`. Pair-wise diff possible with any
text-comparison tool.

### [C] `5_dataset/_checkpoint.json`

Resume marker. Used by phase 5 to skip issues that have already been
fully processed in a previous (possibly interrupted) run.

```json
{
  "processed": [
    "bcgov/STRR#1142",
    "Genesis-Embodied-AI/genesis-world#1365",
    "HHS/simpler-grants-gov#8596",
    ...
  ],
  "stats": { ... mirrored from _run_stats.json ... }
}
```

### [S] `5_dataset/_run_stats.json`

```json
{
  "_schema_version": 1, "_phase": 5,
  "total_input": 779,
  "completed": 753,
  "permanent_skip": 26,
  "failed": 0,
  "total_time_seconds": 8030.5,
  "count_by_language": { "java": 23, "python": 42 },
  "issues_per_discovery_strategy": {
    "LABEL": 366, "CONNEXTRA": 187, "MENTION": 80, "TAG": 61, "TEMPLATE": 59
  },
  "distribution_files_per_pr":  { "min": 1, "max": 1620, "mean": 34.0,  "median": 11.0 },
  "distribution_additions_per_pr": { "median": 342.0,  "max": 424370 },
  "distribution_deletions_per_pr": { "median": 45.0, "max": 272033 },
  "distribution_diff_size": {
    "<10": 23, "10-100": 132, "100-500": 254, "500-1000": 130, "1000+": 213
  },
  "top_20_file_extensions": [ { "extension": ".py", "count": 3344 }, ... ],
  "total_skipped_files": 8815
}
```

Note: `count_by_language.java + count_by_language.python = 65`, not
`completed = 753`. This is because the stats here count **repos** with
Java/Python diffs, not issues. The split-per-language breakdown lives
inside the file system layout itself.

---

## State files (cross-phase)

### [C] `config.json`

Frozen at experiment creation by the dashboard's Launch page or by
`new_experiment.py`. Read by every phase via `paths.load_config()`.

```json
{
  "experiment_id": "run_statistica_prova_100Repo",
  "created_at": "2026-05-19T11:26:37.200535",
  "description": "",
  "run_mode": "testing",
  "discovery_config": {
    "max_total_repos": 100,
    "max_repos_per_strategy": 20,
    "min_stars": 2,
    "languages": ["Java", "Python"],
    "language_mode": "any_above_threshold",
    "min_lang_pct": 25.0,
    "active_strategies": ["LABEL", "TEMPLATE", "TAG", "MENTION", "CONNEXTRA"],
    "inactive_strategies": [],
    "time_windows_granularity": "semester",
    "max_pages_override": 5
  },
  "dataset_config": {
    "min_language_files": 0,
    "min_language_fraction": 0.0,
    "cutoff_date_closed": null,
    "max_issues_per_repo": 40
  },
  "exclude_from_experiments": [],
  "exclude_issues_from_experiments": [],
  "exclude_rejected_issues_from": [],
  "exclude_validated_issues_from": []
}
```

### [C] `pipeline_state.json`

Written by the orchestrator after every phase finishes. The dashboard
polls this file to show live progress.

```json
{
  "phase_1_done": true,
  "phase_2_done": true,
  "phase_3_done": true,
  "phase_4_done": true,
  "phase_5_done": true,
  "started_at": "2026-05-19T11:26:37",
  "errors": [],
  "phase_1_done_at": "2026-05-19T11:38:05",
  "phase_2_done_at": "2026-05-19T12:17:43",
  "phase_3_done_at": "2026-05-19T12:23:52",
  "phase_4_done_at": "2026-05-19T15:53:01",
  "phase_5_done_at": "2026-05-19T18:07:03"
}
```

### [C] `_runtime.json`

Written by the orchestrator at start-up, used by the dashboard to detect
externally-launched runs (e.g. one launched from the terminal while the
dashboard is open).

```json
{
  "experiment_id": "run_statistica_prova_100Repo",
  "pid": 6508,
  "started_at": "2026-05-19T11:26:37.214211",
  "cwd": "C:\\Users\\...\\stage-dataset\\pipeline",
  "cmd": ["python.exe", "-u", "orchestration/run_pipeline.py"],
  "finished_at": null,
  "exit_code": null
}
```

`finished_at` and `exit_code` stay `null` until the orchestrator exits.

### [C] `_done.txt`

Plain-text marker written only after all 5 phases complete successfully.
The dashboard reads it to mark an experiment as "fully done".

```
completed_at: 2026-05-19T18:07:03
elapsed_sec: 24026
phases_completed: [1, 2, 3, 4, 5]
```

---

## Reusing the outputs

### Load any JSON array with pandas

```python
import pandas as pd

# Each *.json file in phases 1..4 is a JSON array, not JSON-lines.
df = pd.read_json("experiments/<id>/4_ai_validation/validated_issues.json")
```

### Join phase 2 (issues) with phase 3 (PRs)

```python
import pandas as pd

issues = pd.read_json(".../2_issue_discovery/issues_user_story.json")
issues["key"] = issues["repo"] + "#" + issues["number"].astype(str)

linked = pd.read_json(".../3_pr_linking/issues_with_pr.json")
linked["key"] = linked["repo"] + "#" + linked["number"].astype(str)

# All issues that survived phase 3
df = issues.merge(linked[["key", "pr", "link_source"]], on="key", how="inner")
```

### Filter the validated user stories

```python
validated = pd.read_json(".../4_ai_validation/validated_issues.json")

# Only "complete" Connextra with high confidence
high_quality = validated[
    (validated["ai_validation"].apply(lambda v: v["connextra"]) == "complete") &
    (validated["ai_validation"].apply(lambda v: v["confidence"]) == "high")
]
```

### Walk the per-issue dataset

```python
from pathlib import Path
import json

DATASET = Path("experiments/<id>/5_dataset")

for data_file in DATASET.rglob("data.json"):
    issue_dir = data_file.parent
    record = json.loads(data_file.read_text())

    repo = record["repository"]
    n = record["issue_number"]
    base_sha = record["base_sha"]

    # Read original/modified file pairs
    for orig in (issue_dir / "original_files").glob("*"):
        modified = issue_dir / "modified_files" / orig.name
        if not modified.exists():
            continue
        # diff orig.read_text() vs modified.read_text() with any tool
```

---

## What to ignore

If you only care about the **final dataset** (not the analysis):

- Skip everything under `_logs/`, `_pipeline.log`, `_bootstrap.log`.
- Skip every `run_stats.json` (those are for the dashboard).
- Skip `DEBUG_*.json` files in `1_discovery/`.
- Skip `_checkpoint.json` (only useful for resumption).
- Skip phase 3 / phase 4 intermediates (`issues_with_pr.json`,
  `validated_issues.json`, ...) — everything you need is already in
  `5_dataset/.../data.json`.

The minimal "dataset to publish" is:

```
experiments/<id>/5_dataset/
└── <language>/<owner>_<repo>/issue_<number>/
    ├── data.json
    ├── original_files/
    └── modified_files/
```

plus, optionally, `config.json` for reproducibility.