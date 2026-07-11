# 03 — Phase 1: Repository Discovery

> Find GitHub repositories likely to contain user stories.
> Five strategies, global deduplication, time-windowed search to bypass
> the 1000-result GitHub limit.

## 1. Goal

Phase 1 builds a list of **candidate repositories**: repos that are
likely to contain user stories somewhere in their issues, code, or
templates. It does **not** download the user stories themselves — that
is phase 2's job.

The output is a single file, `1_discovery/repo_user_story.json`,
containing one record per accepted repository.

## 2. The five strategies

Each strategy is a different way of "guessing" that a repository uses
user stories. They run **sequentially**, in this order:

1. **LABEL** — repos that have issues tagged with a label like
   `user-story`, `user story`, `user_story`
2. **TEMPLATE** — repos with an issue template under
   `.github/ISSUE_TEMPLATE` mentioning "user story", "As a / I want", etc.
3. **TAG** — repos whose issue titles match patterns like `US-42`,
   `[US]`, `[User Story]`
4. **MENTION** — repos where issues mention "user story" in title or body
5. **CONNEXTRA** — repos with the Connextra pattern
   (`as a ... I want ...`) in issue bodies

Why this specific order? **Precision first, recall last.** LABEL has
extremely low noise (a label is an explicit commitment from the repo
owner); CONNEXTRA is the broadest and noisiest. The order matters
because of the deduplication rule (see below).

Each strategy is a dict in `1_discovery.py`. Here is LABEL as an
example:

```python
STRATEGY_LABEL = {
    "name": "LABEL",
    "queries": [
        'label:"user story" is:issue is:closed',      # with space → quotes OK
        'label:"user stories" is:issue is:closed',
        'label:user-story is:issue is:closed',        # hyphen → NO quotes
        'label:user-stories is:issue is:closed',
        'label:user_story is:issue is:closed',        # underscore → NO quotes
        'label:userstory is:issue is:closed',
    ],
    "search_type": "issues",
    "sort": "created",
    "regex_filter": None,
    "regex_target": None,
    "time_windows": True,
}
```

### A quoting lesson learned

GitHub's Search API has a non-obvious quoting rule for labels:

- Labels with **spaces** → must be quoted (`label:"user story"`)
- Labels with hyphens, underscores, or no separator → must **not** be
  quoted (`label:user-story`)

Mixing the two in the same query causes the search to silently return
empty results. This is why LABEL has six separate queries instead of one
big OR'd list.

## 3. Global deduplication: every repo belongs to exactly one strategy

The phase maintains a single global set `seen_repos`. As each strategy
runs, every repository it finds is checked against the set:

```python
seen_repos = set(REPO_BLACKLIST)  # also pre-populated from prior experiments

for strategy in STRATEGIES:
    stats = run_strategy(strategy, seen_repos, valid_repos)

# Inside run_strategy:
if repo_name in seen_repos:
    continue  # already attributed to a previous (more precise) strategy
seen_repos.add(repo_name)
```

This means **overlap between strategies is always 0 by design**. A repo
that would match both LABEL and CONNEXTRA gets attributed to LABEL,
because LABEL ran first. The benefit: when you analyze the dataset,
"how many repos did strategy X bring in" has a clean, non-ambiguous
answer.

The cost: per-strategy acceptance rates are not directly comparable
between experiments unless the strategy order is also identical.

## 4. Time-windowed search (working around GitHub's 1000-result cap)

GitHub's Search API has a hard limit of **1000 results per query**, no
matter what. Even with pagination, you cannot get past 1000.

For broad strategies (TAG, MENTION, CONNEXTRA) this is a real
problem: a query like `"user story" in:title,body` can match millions of
issues globally. To work around the cap, the phase splits each query
across multiple **time windows**:

```python
TIME_WINDOWS_GRANULARITY = _dc.get("time_windows_granularity", "yearly")
# "yearly", "semester", or "quarter"

def _generate_time_windows(granularity: str) -> list[str]:
    """Generate qualifiers like 'created:2024-01-01..2024-12-31', '...semester', etc."""
```

For each query in a strategy with `"time_windows": True`, the search is
executed once per window. Each window has its own 1000-result budget.

Granularity options:

| Granularity | Windows generated | API cost | Coverage |
|-------------|------------------|----------|----------|
| `yearly`    | ~15 (one per year since 2010) | low | low–medium |
| `semester`  | ~30 | medium | medium–high |
| `quarter`   | ~60 | high | maximum |

Higher granularity = more queries = more API calls = more rate-limit
pressure, but also more coverage. The default for production runs is
`semester`.

For phase 2, time windows are handled differently (per-repo issue
listing, not search), so the global API budget is mostly spent here in
phase 1.

## 5. Strategy execution flow

Inside `run_strategy(strategy, seen_repos, valid_repos)`:

1. **Resolve queries.** If `time_windows=True`, expand each query into
   one-per-window. Otherwise, run the query as-is.
2. **For each (query, window) pair, paginate the GitHub search.** Up to
   `max_pages` × 100 results per page (default `max_pages=10`).
3. **For each search hit, extract the repo identity** (`owner/name`).
   Skip if already in `seen_repos`.
4. **Apply optional regex filter** (`regex_filter` against
   `regex_target` field). Used by TAG to reject false-positive titles
   like "JavaScript framework" matching `JS` and by CONNEXTRA to filter
   out blacklisted "as a result/matter of fact" idioms.
5. **Apply the repo filters** (see next section).
6. **If the repo passes everything**, add to `valid_repos` with the
   strategy name attached.

The throttle between API calls is handled by `github_pacing.py`
transparently — the strategy code never calls `time.sleep` directly.

## 6. The repo filter chain

A repo discovered by any strategy must pass:

- **Star floor** — `stars >= min_stars` (default 2; production runs use
  500).
- **Date filter** — repo created before the experiment's "future cutoff"
  (`year_cutoff_max`, typically the current year).
- **Language filter** — two modes:
  - `primary_only` — only accept if `primaryLanguage ∈ ALLOWED_LANGUAGES`.
  - `any_above_threshold` — accept if ANY allowed language has
    `>= min_lang_pct` bytes percentage (default 25%). Captures
    polyglot repos where the language of interest isn't the primary.
- **Not null** — skip repos GitHub returned with a missing primary
  language (sometimes happens for forks or empty repos).

All filter decisions are tracked in `run_stats.json`:

```json
"global_totals": {
  "unique_repos_seen": 1568,
  "accepted_repos": 99,
  "discarded_stars": 917,
  "discarded_date": 177,
  "discarded_language": 360,
  "discarded_regex": 11658
}
```

The `discarded_regex` count is by far the biggest contributor on most
runs — that is the cost of TAG's broad initial query, where most title
matches are not real user-story tags.

## 7. Caps on the harvest

Two caps prevent the discovery from running forever:

- `max_repos_per_strategy` — once a strategy has accepted N repos, it
  stops (other strategies still run). Default 20 in test mode, `null`
  (uncapped) in "Full Coverage" mode.
- `max_total_repos` — global cap across all strategies. Default 100 in
  test mode.

When either cap is `null`, the code uses an internal sentinel (`+Inf`
for comparisons, `10^9` for budget computations) to keep the math the
same.

## 8. Configuration parameters

All read from `config.json` under `discovery_config`:

```json
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
}
```

| Field | Purpose |
|-------|---------|
| `max_total_repos` | Global cap. `null` = no cap |
| `max_repos_per_strategy` | Per-strategy cap. `null` = no cap |
| `min_stars` | Star floor on the GitHub query (`stars:>=N`) |
| `languages` | Accepted languages (used by the language filter) |
| `language_mode` | `primary_only` or `any_above_threshold` |
| `min_lang_pct` | Threshold for `any_above_threshold` mode (%) |
| `active_strategies` | Whitelist. Omit a name to skip that strategy |
| `inactive_strategies` | Blacklist (alternative to `active_strategies`) |
| `time_windows_granularity` | `yearly`, `semester`, `quarter` |
| `max_pages_override` | Override the per-strategy `max_pages` (default 10) |

## 9. Output

`1_discovery/repo_user_story.json` — list of accepted repos with all
metadata (see `11_output_catalog.md` for the full schema).

`1_discovery/run_stats.json` — full breakdown for the dashboard.

`1_discovery/DEBUG_tag_discarded_regex.json` — diagnostic dump of titles
the TAG regex discarded (debug only, can be deleted).

## 10. A known issue: cutoff misalignment with phase 2

Phase 1 currently does **not** apply the
`dataset_config.cutoff_date_closed` filter inside its search queries.
The queries are simply `is:closed` (any closing date).

Phase 2 **does** apply the cutoff (it adds `closed:>=YYYY-MM-DD` to its
per-repo issue queries).

The consequence: when running an experiment with a recent cutoff date,
phase 1 happily accepts a repo whose last user-story-ish issue was
closed years before the cutoff. Phase 2 then finds zero qualifying
issues in that repo, leading to ~half the discovered repos contributing
nothing to the dataset.

The fix is straightforward (add the cutoff to phase 1's queries when
the config has one) but has not been applied yet. See
`13_limitations_future_work.md` for the planned remediation.
