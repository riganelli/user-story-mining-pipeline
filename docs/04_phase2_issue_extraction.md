# 04 — Phase 2: Issue Extraction

> For each repo found in phase 1, fetch the closed issues that look
> like user stories. Parallel per-repo workers, cutoff filter at two
> levels, regex filtering for noisy strategies.

## 1. Goal

Phase 2 takes the list of repositories produced by phase 1 and, for
each one, fetches the **closed issues** that match user-story-like
patterns. The output is a flat list of issues, one record per issue,
ready to be enriched with PR data in phase 3.

The output file is `2_issue_discovery/issues_user_story.json`.

## 2. Input and output at a glance

**Input:** `1_discovery/repo_user_story.json` — produced by phase 1.

**Output:** `2_issue_discovery/issues_user_story.json` — see schema in
`11_output_catalog.md`.

Roughly: 100 input repos → 2500 output issues on a typical run.

## 3. The same five strategies, applied differently

Phase 2 reuses the **same five strategy names** as phase 1 (LABEL, TAG,
TEMPLATE, CONNEXTRA, MENTION), but the queries and execution are
different:

- Phase 1 searched **GitHub globally** to find repos.
- Phase 2 searches **inside known repos** to find issues.

The strategies are defined inline in `2_issue_discovery.py`. Example:

```python
STRATEGY_LABEL = {
    "name": "LABEL",
    "queries": [
        # Labels WITH SPACES go in quotes (one query, comma-separated OR)
        'label:"user story","user stories" is:issue is:closed',
        # Labels WITHOUT SPACES go WITHOUT quotes, one per query
        'label:user-story is:issue is:closed',
        'label:user-stories is:issue is:closed',
        'label:user_story is:issue is:closed',
        'label:userstory is:issue is:closed',
        # Special case: enhancement label + Connextra in body
        'label:enhancement "as a" in:body is:issue is:closed',
    ],
    "regex_filter": None,
    "regex_target": None,
    "max_pages": 3,
}
```

The execution order is the same: LABEL → TAG → TEMPLATE → CONNEXTRA →
MENTION. Within each repo, an issue can be matched by multiple
strategies, but only the **first** match attaches the strategy name to
the record (per-issue deduplication, similar in spirit to phase 1's
per-repo deduplication).

### TEMPLATE is special

`STRATEGY_TEMPLATE` has an empty `queries` list and an extra
`"template_based": True` flag. Instead of searching, this strategy:

1. Looks for the repo's `.github/ISSUE_TEMPLATE/` folder.
2. Reads the templates that mention "user story", "As a / I want", etc.
3. Issues created from those templates are matched against the template
   structure.

This is more expensive than the search-based strategies but catches
issues that don't have any explicit user-story marker themselves —
they inherit the structure from the template.

## 4. Parallel per-repo workers

Phase 2 processes multiple repos in parallel using a
`ThreadPoolExecutor`:

```python
MAX_WORKERS = 4

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(process_repo, repo): repo for repo in repos}
    for future in as_completed(futures):
        ...
```

Four workers is the sweet spot found empirically: more workers don't
help because the bottleneck is GitHub's search rate limit (30 req/min
per token for search endpoints), which `github_pacing.py` enforces
across all threads.

Logging is serialized via a shared lock from `utils/logging_utils.py`,
so output lines from different workers don't get interleaved mid-print.

## 5. Cutoff date — applied at TWO levels

Phase 2 honors the `dataset_config.cutoff_date_closed` field from the
experiment config:

```python
def _load_cutoff_date() -> "str | None":
    """Read dataset_config.cutoff_date_closed from config.json.
    Returns the date string or None if disabled/invalid."""
```

When the cutoff is set, it is applied in two places:

**Level 1 — server side.** The qualifier `closed:>=YYYY-MM-DD` is added
to the GitHub query. This is the "polite" enforcement: GitHub never
sends back issues older than the cutoff, saving the rate limit budget.

**Level 2 — client side.** Each issue's `closed_at` field is also
checked locally before applying the regex filter. This is the
"paranoid" enforcement: if GitHub for any reason returned an
out-of-range issue, the client-side check drops it before any expensive
processing.

The double-check is cheap (one comparison per issue) and removes a
whole class of subtle bugs where a stale GitHub index could leak
out-of-range data into the dataset.

The filter is applied on `closed_at`, **not** `created_at`. An issue
created in 2018 but closed in 2024 is "in scope" for a 2024 cutoff,
because the user story was developed in the period we care about.

## 6. The `max_issues_per_repo` cap

A second cap, separate from the cutoff, limits how many issues any
single repo can contribute:

```python
def _load_max_issues_per_repo() -> "int | None":
    """Read dataset_config.max_issues_per_repo. Returns int >= 1 or None
    (= no cap)."""

MAX_ISSUE_PER_REPO = _load_max_issues_per_repo()
```

Without this cap, a single hyper-active repo (one of the SOEN490 or
ICTU/quality-time class — university final projects with hundreds of
user-story issues) would dominate the dataset. With a cap of 40, the
distribution stays balanced.

Inside the processing loop, the worker stops fetching new issues as
soon as it hits the cap for that repo:

```python
if len(found_issues) >= MAX_ISSUE_PER_REPO:
    break
```

## 7. Regex filtering for noisy strategies

TAG and CONNEXTRA produce noisy results in phase 2 as well as in phase 1.
The same regexes from `utils/discovery_strategies.py` are reused:

```python
STRATEGY_TAG = {
    ...
    "regex_filter": REGEX_TAG_COMBINED,
    "regex_target": "title",   # apply the regex to the title
    ...
}

STRATEGY_CONNEXTRA = {
    ...
    "regex_filter": REGEX_CONNEXTRA_PARTIAL,
    "regex_target": "body",
    ...
}
```

Issues that fail the regex are counted under `discarded_regex` in
`run_stats.json`. In a typical run, the TAG strategy discards ~40% of
the issues it fetches, and CONNEXTRA discards ~79% — most of the false
positives are issues where the phrase "as a" appears in a non-Connextra
context.

## 8. Early-exit on high discard ratios

To save time on broken queries:

```python
MAX_DISCARD_RATIO = 0.85  # Early-exit if a strategy discards >85% of issues
```

If a strategy is discarding more than 85% of what it fetches, it stops
early. This prevents wasting rate limit on a query that has clearly
gone wrong (e.g. a regex that started matching everything because of a
typo).

## 9. Output schema

Each record in `issues_user_story.json` looks like this:

```json
{
  "number": 935,
  "title": "Specify start and end date for bulk CVE ingestion",
  "body": "As a contributor to the security tracker, ...",
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

`strategy` here is the **phase 2 strategy** that matched this issue,
not the phase 1 strategy that discovered the repo. The two can be (and
often are) different: a repo found by CONNEXTRA in phase 1 may have its
issues matched by LABEL in phase 2.

## 10. Configuration parameters

Read from `config.json` under both `discovery_config` (for strategy
selection) and `dataset_config` (for filters):

```json
"discovery_config": {
  "active_strategies": ["LABEL", "TEMPLATE", "TAG", "MENTION", "CONNEXTRA"]
},
"dataset_config": {
  "cutoff_date_closed": null,
  "max_issues_per_repo": 40
}
```

| Field | Section | Purpose |
|-------|---------|---------|
| `active_strategies` | `discovery_config` | Whitelist. Same list as phase 1, can differ |
| `cutoff_date_closed` | `dataset_config` | ISO date `YYYY-MM-DD`. `null` = no filter |
| `max_issues_per_repo` | `dataset_config` | Cap per repo. `null` = no cap |

The `inactive_strategies` field is also honored as a blacklist, in case
the launcher writes both lists.

## 11. Statistics

`run_stats.json` reports:

- Total processed repos and issues found.
- Per-strategy breakdown: issues found, regex discard rate, time spent.
- `issues_per_repo_distribution` (min, max, mean, median).
- Top-10 repos by issue count.
- Number of empty repos (repos that contributed zero issues).

A snippet from a real run:

```json
"totals": {
  "processed_repos": 99,
  "issues_found": 2464,
  "discarded_regex": 1970,
  "empty_repos": 3
},
"issues_per_repo_distribution": {
  "min": 0, "max": 40, "mean": 24.89, "median": 31,
  "empty_repos": 3, "repo_con_almeno_1_issue": 96
}
```

The `empty_repos` count is the symptom of the cutoff-misalignment issue
described in `03_phase1_discovery.md` § 10: when phase 1 doesn't
pre-filter for the cutoff and phase 2 does, some repos that "looked
right" in phase 1 turn out to have zero qualifying issues here.
