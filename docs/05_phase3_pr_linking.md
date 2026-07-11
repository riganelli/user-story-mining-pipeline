# 05 — Phase 3: PR Linking

> For every issue, find the Pull Requests that closed it.
> Single-pass GraphQL strategy: one batched query per group of issues,
> in two rounds (closing/cross references first, referenced events on
> the leftovers).

## 1. Goal

Phase 3 takes the issues from phase 2 and, for each one, tries to find
the **Pull Request(s) that closed it**. The output splits into two
files:

- `3_pr_linking/issues_with_pr.json` — issues that resolved to at least
  one PR.
- `3_pr_linking/issues_without_pr.json` — issues with no closing PR
  found.

Both files keep the full issue record from phase 2 and the issues_with_pr
file enriches each record with a `pr` list and a `link_source` tag.

A typical 100-repo run links ~48% of the input issues. The unlinked ones
are kept (not discarded) for ablation analysis.

## 2. Why GraphQL and not REST

GitHub's REST API does not expose the field we need:
`closingIssuesReferences`. This is the link from a PR back to the
issue(s) it explicitly closes via `Closes #N` / `Fixes #N` keywords in
the PR description.

GraphQL exposes it directly via the `timelineItems` connection:

```graphql
query {
  repository(owner: "X", name: "Y") {
    issue(number: 42) {
      closingReferencesConnection: timelineItems(
        itemTypes: CONNECTED_EVENT, first: 50
      ) {
        nodes {
          ... on ConnectedEvent {
            subject { ... on PullRequest { number title url merged mergedAt state } }
          }
        }
      }
      crossReferences: timelineItems(
        itemTypes: CROSS_REFERENCED_EVENT, first: 50
      ) {
        nodes {
          ... on CrossReferencedEvent {
            source { ... on PullRequest { number title url merged mergedAt state } }
          }
        }
      }
    }
  }
}
```

Two timeline item types are queried in parallel for each issue:

- `CONNECTED_EVENT` — issues "officially closed by" a PR (the strongest
  link).
- `CROSS_REFERENCED_EVENT` — PRs that mention the issue (weaker but
  often the only signal when the closing keyword was not used).

The two together give the best recall.

## 3. The single-pass decision

Phase 3 uses GraphQL only. Two additional fallbacks were evaluated
during development and deliberately left out:

- querying GitHub's Search API for PRs whose body contained `#N`
  (issue number);
- searching commit messages for `#N` references.

Both were expensive (the Search API has its own 30 req/min rate limit,
on top of core) and, in a benchmark over 1086 issues, contributed less
than 1.5% extra recall at the cost of ~43 minutes of runtime — with
the commit search finding zero PRs. Phase 3 therefore runs strictly
the GraphQL single-pass.

For the full reasoning, see `12_design_decisions.md`.

## 4. The Round 1 / Round 2 split

Within the single GraphQL pass, there are two rounds:

### Round 1 — `closing` + `cross_reference`

The query above runs first. The result is parsed by `extract_pr_round1`,
which collects every PR found via either event type. Issues with at
least one PR are immediately resolved.

### Round 2 — `REFERENCED_EVENT` on the leftovers

Issues not resolved by Round 1 get a second query:

```graphql
referencedEvents: timelineItems(itemTypes: REFERENCED_EVENT, first: 50) {
  nodes {
    ... on ReferencedEvent {
      commit {
        oid url
        associatedPullRequests(first: 5) {
          nodes { number title url merged mergedAt state }
        }
      }
    }
  }
}
```

`REFERENCED_EVENT` fires when a commit message mentions the issue. The
commit may or may not be inside a PR — `associatedPullRequests` gives
the PR if there is one.

This catches issues where the developer fixed the bug in a direct
commit (no `Fixes #N` in the PR description) but the commit message
itself mentioned the issue.

`MENTIONED_EVENT` is intentionally excluded: its `subject` is just
"@user mentioned", with no PR or commit attached. Querying it would
only add noise.

In a typical 100-repo run, Round 2 recovers ~6-8% extra issues
(`issue_round2_extra: 78` out of `issues_with_pr: 1177` in the
benchmark).

The `link_source` field records which round produced the link:
`"graphql"` for Round 1, `"graphql_r2"` for Round 2.

## 5. Batching: 10 issues per GraphQL query

GraphQL queries are built dynamically with N issues batched into a
single query, each as an aliased sub-field (`r0`, `r1`, …, `r9`):

```python
GRAPHQL_BATCH_SIZE = 10

def build_query_round1(issues_batch):
    parts = []
    for idx, issue in enumerate(issues_batch):
        owner, name = issue["repo"].split("/")
        alias = f"r{idx}"
        parts.append(
            f'{alias}: repository(owner:"{owner}", name:"{name}") {{ ... }}'
        )
    return "query {\n" + "\n".join(parts) + "\n}"
```

Why 10 and not 100? Two reasons:

1. **TLS reliability.** Big queries (50+ aliases) occasionally hang the
   TLS connection on Windows when running through corporate proxies.
   10 is the empirical sweet spot for stability.
2. **Per-query cost.** GraphQL costs are computed per node; a 10-issue
   query stays under the 500-node-per-request soft limit, even with
   `first: 50` on two timeline connections.

## 6. PR record schema

Each PR found is normalized to:

```json
{
  "number": 955,
  "title": "feat: date-based CVE ingestion filtering",
  "url": "https://github.com/NixOS/nix-security-tracker/pull/955",
  "merged": true,
  "merged_at": "2026-03-30T10:38:01Z",
  "state": "MERGED",
  "source": "cross_reference"
}
```

`source` is one of:

- `"closing"` — found via `CONNECTED_EVENT` in Round 1.
- `"cross_reference"` — found via `CROSS_REFERENCED_EVENT` in Round 1.
- `"timeline_event"` — found via `REFERENCED_EVENT` in Round 2.

`state` is one of `MERGED`, `CLOSED` (closed without merging), `OPEN`
(still open, but already linked to a closed issue).

## 7. Per-issue cap

To avoid noise, each issue saves at most 5 PRs:

```python
MAX_PR_PER_ISSUE = 5
```

Issues with more than 5 candidate PRs (~2% of cases in a typical run)
have the rest truncated. The selection prefers merged-and-most-recent
first.

In the stats:

```json
"prs_per_issue_distribution": {
  "1": 903,
  "2": 170,
  "3": 57,
  "4": 19,
  "5+": 28
}
```

The vast majority of linked issues have exactly one PR.

## 8. Output statistics

`3_pr_linking/run_stats.json` reports:

```json
{
  "_schema_version": 1, "_phase": 3, "version": "graphql-p1",
  "total_time_seconds": 368.4,
  "avg_time_per_issue_seconds": 0.15,
  "totals": {
    "input_issues": 2464,
    "issues_with_pr": 1177,
    "issues_without_pr": 1287,
    "success_rate_pct": 47.8,
    "total_prs_found": 1630
  },
  "per_pass": {
    "P1_graphql": {
      "resolved_issues": 1177,
      "issue_round2_extra": 78,
      "wall_clock_time_seconds": 368.4,
      "contribution_pct": 100.0
    }
  },
  "link_source_distribution": {
    "graphql": 1099,
    "graphql_r2": 78
  },
  "time_issue_to_merge": {
    "min_days": 0,
    "max_days": 2467,
    "mean_days": 62.8,
    "median_days": 20,
    "sample_size": 1171
  }
}
```

The `time_issue_to_merge` distribution is one of the more interesting
data points the phase emits: median 20 days from issue creation to PR
merge, with a long tail (max 2467 days, ~6.7 years) for issues that
were declared "fixed" only after a long dormancy.

## 9. What this phase does NOT do

It does **not** fetch the PR file diffs, the commits, the source code
or the merge commit details. All of that is phase 5's job. Phase 3's
output is purely **metadata about which PR closed which issue**.

This separation is intentional: phase 3 is fast (~6 minutes on a
100-repo run) and rate-limit-friendly. Phase 5 is slow (hours) and
hits the REST API hard. Keeping them separate means you can run phase
3 as a "first cut" of the dataset, then decide whether to invest the
time in phase 5.

## 10. Configuration

Phase 3 has no per-experiment configuration. Its behavior is fixed by
constants at the top of the script:

```python
GRAPHQL_BATCH_SIZE     = 10        # issues per GraphQL query
GRAPHQL_TIMELINE_FIRST = 50        # timeline items fetched per issue
MAX_PR_PER_ISSUE       = 5         # cap on PRs saved per issue
```

The phase respects the global `EXPERIMENT_ID` selection and the
checkpoint mechanism (`utils/checkpoint.py`) but otherwise runs the
same way on every experiment.
