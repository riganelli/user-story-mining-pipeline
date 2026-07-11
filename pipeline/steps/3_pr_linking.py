"""Phase 3 — issue-to-PR linking.

Links each user-story issue to the pull requests that reference it, using
a single GraphQL pass (round 1: closing and cross references; round 2:
referenced events on the issues still unresolved). Issues with no linked
PR are written to ``issues_without_pr`` with the same schema, so phases 4
and 5 run unchanged.
"""

import json
import os
import sys
import time

# Make the pipeline package importable when run directly or via the
# orchestrator.
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from config.experiment_paths import paths

# GitHub client with adaptive pacing; the "graphql" bucket is paced
# independently of core/search.
from utils import github_pacing as gh

from utils.stats_utils import median
from utils.checkpoint import load_resume, load_progress
from utils.atomic_io import dump_json_atomic

sys.stdout.reconfigure(encoding='utf-8')

GRAPHQL_URL = "https://api.github.com/graphql"


# Configuration

INPUT_FILE  = str(paths.step2_issues_json)
OUTPUT_FILE = str(paths.step3_with_pr_json)
DISCARDED_FILE = str(paths.step3_without_pr_json)

# GraphQL pass.
GRAPHQL_BATCH_SIZE     = 10
GRAPHQL_TIMELINE_FIRST = 50

MAX_PR_PER_ISSUE       = 5         # Cap on PRs saved per issue, to limit noise.


print("=" * 70)
print("PHASE 3 — ISSUE ↔ PR LINKING (GraphQL)")
print("=" * 70)
print(f"  Input:                       {INPUT_FILE}")
print(f"  Output:                      {OUTPUT_FILE}")
print(f"  Discarded:                   {DISCARDED_FILE}")
print()
print(f"  P1 timeline first:           {GRAPHQL_TIMELINE_FIRST}")
print(f"  MAX_PR_PER_ISSUE:            {MAX_PR_PER_ISSUE}")
print()
print("  Strategy:")
print("    P1: aggressive GraphQL (closing + cross + referenced)")
print("    Issues not resolved by P1 → issues_without_pr (no Search/Commit fallback)")
print()


# tprint is used for consistency with the other phases even though this
# script is sequential.


# Load input and resume from checkpoint.

paths.ensure_step_dir(3)
print(f"📁 Current experiment: {paths.experiment_id}")

if not os.path.exists(INPUT_FILE):
    raise FileNotFoundError(f"File {INPUT_FILE} not found. Run phase 2 first.")

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    issues_list = json.load(f)

print(f"Loaded {len(issues_list)} issues from {INPUT_FILE}")

issues_with_pr, issues_without_pr, processed_issues = load_resume(
    output_file=OUTPUT_FILE,
    discarded_file=DISCARDED_FILE,
    output_label="issues with PRs already found",
    discarded_label="issues without PRs already recorded",
)

# Progress file with cumulative timings across resumed jobs. The
# session-local counters are recomputed from issues_with_pr at the end.
_PROGRESS_FILE = paths.step3_dir / "_progress.json"
_prog3 = load_progress(str(_PROGRESS_FILE))
cumulative_elapsed_total_seconds = float(_prog3.get("cumulative_elapsed_total_seconds", 0.0))
cumulative_elapsed_p1_seconds    = float(_prog3.get("cumulative_elapsed_p1_seconds", 0.0))
if cumulative_elapsed_total_seconds > 0:
    print(f"📂 RESUME: restored cumulative elapsed "
          f"({cumulative_elapsed_total_seconds:.1f}s total, "
          f"{cumulative_elapsed_p1_seconds:.1f}s in P1)")
print()


# Output and atomic saving, so phase 4 never reads a truncated file.

def build_issue_output(issue_info, pr_merged, source=""):
    """Build the output record for an issue with its linked PRs.

    Includes ``closed_at`` so phase 5 can compute the issue-to-PR delta.
    """
    return {
        "repo": issue_info["repo"],
        "number": issue_info["number"],
        "title": issue_info.get("title", ""),
        "body": issue_info.get("body", ""),
        "labels": issue_info.get("labels", []),
        "url": issue_info.get("url", ""),
        "author": issue_info.get("author", ""),
        "state": issue_info.get("state", ""),
        "created_at": issue_info.get("created_at", ""),
        "closed_at": issue_info.get("closed_at", ""),
        "strategy": issue_info.get("strategy", ""),
        "pr": pr_merged,
        "link_source": source,
    }


def save_progress(with_pr_snapshot=None, without_pr_snapshot=None):
    """Incremental saving of results (checkpoint).

    Uses atomic writes (write-to-tmp + os.replace) so a SIGKILL by SLURM
    during a json.dump cannot leave a truncated file on disk. On Windows
    the helper falls back to a direct overwrite if os.replace hits a
    PermissionError held by a reader (antivirus, editor).

    Also persists the cumulative wall-clock timers to _PROGRESS_FILE: the
    in-memory variables `start_p1` and `_session_start` are session-local
    and would zero out on restart, so we accumulate them on disk.
    """
    cp = with_pr_snapshot if with_pr_snapshot is not None else issues_with_pr
    sp = without_pr_snapshot if without_pr_snapshot is not None else issues_without_pr
    dump_json_atomic(cp, OUTPUT_FILE)
    dump_json_atomic(sp, DISCARDED_FILE)

    # Persist cumulative timers, reading the session counters from globals
    # instead of threading them through every call.
    try:
        now = time.time()
        session_total = now - _session_start
        session_p1    = (now - start_p1) if start_p1 is not None else 0.0
        payload = {
            "cumulative_elapsed_total_seconds":
                cumulative_elapsed_total_seconds + session_total,
            "cumulative_elapsed_p1_seconds":
                cumulative_elapsed_p1_seconds + session_p1,
        }
        dump_json_atomic(payload, _PROGRESS_FILE)
    except (OSError, NameError) as e:
        # Skip on early-call NameError or filesystem error; the data file
        # is already safe.
        pass


# Session clock anchors: _session_start is set at module load; start_p1
# is set just before pass 1 and is None until then.
_session_start = time.time()
start_p1 = None


# GraphQL pass. Round 1: closing and cross references. Round 2:
# referenced events on the issues still unresolved.

def build_query_round1(issues_batch):
    """GraphQL Round 1 query: CONNECTED_EVENT + CROSS_REFERENCED_EVENT."""
    parts = []
    alias_map = {}
    for idx, issue in enumerate(issues_batch):
        owner, name = issue["repo"].split("/")
        number = issue["number"]
        alias = f"r{idx}"
        parts.append(
            f'{alias}: repository(owner:"{owner}", name:"{name}") {{\n'
            f'  issue(number:{number}) {{\n'
            '    closingReferencesConnection: timelineItems('
            f'itemTypes: CONNECTED_EVENT, first: {GRAPHQL_TIMELINE_FIRST}) {{\n'
            f'      nodes {{ ... on ConnectedEvent {{ subject {{\n'
            f'        ... on PullRequest {{ number title url merged mergedAt state }}\n'
            f'      }} }} }}\n'
            f'    }}\n'
            '    crossReferences: timelineItems('
            f'itemTypes: CROSS_REFERENCED_EVENT, first: {GRAPHQL_TIMELINE_FIRST}) {{\n'
            f'      nodes {{ ... on CrossReferencedEvent {{ source {{\n'
            f'        ... on PullRequest {{ number title url merged mergedAt state }}\n'
            f'      }} }} }}\n'
            f'    }}\n'
            f'  }}\n'
            f'}}'
        )
        alias_map[alias] = issue
    query = "query {\n" + "\n".join(parts) + "\n}"
    return query, alias_map


def build_query_round2(issues_batch):
    """GraphQL Round 2 query: REFERENCED_EVENT (broader).

    Note: REFERENCED_EVENT.subject is a Commit, not a PR — but the commit
    belongs to a PR and the commit_url carries info. MENTIONED_EVENT has no
    useful subject (it is only "@user mentioned"), so I exclude it to avoid
    polluting the query. I keep REFERENCED_EVENT, which is the one that
    carries signal.
    """
    parts = []
    alias_map = {}
    for idx, issue in enumerate(issues_batch):
        owner, name = issue["repo"].split("/")
        number = issue["number"]
        alias = f"r{idx}"
        parts.append(
            f'{alias}: repository(owner:"{owner}", name:"{name}") {{\n'
            f'  issue(number:{number}) {{\n'
            '    referencedEvents: timelineItems('
            f'itemTypes: REFERENCED_EVENT, first: {GRAPHQL_TIMELINE_FIRST}) {{\n'
            f'      nodes {{ ... on ReferencedEvent {{\n'
            f'        commit {{ oid url\n'
            f'          associatedPullRequests(first: 5) {{\n'
            f'            nodes {{ number title url merged mergedAt state }}\n'
            f'          }}\n'
            f'        }}\n'
            f'      }} }}\n'
            f'    }}\n'
            f'  }}\n'
            f'}}'
        )
        alias_map[alias] = issue
    query = "query {\n" + "\n".join(parts) + "\n}"
    return query, alias_map


def run_graphql(query):
    """Runs a GraphQL query via github_pacing.
    Adaptive pacing, retry on 5xx and 403 handling (primary/secondary/permanent)
    are now centralized in github_pacing. I keep this function as a thin
    wrapper so as not to break the call sites."""
    return gh.graphql(query)


# Bounded retry on a single GraphQL batch. github_pacing already retries
# 5xx/403, but a transient GraphQL hiccup (empty or malformed response) can
# slip through. The whole batch is retried a few times with a short
# increasing backoff; the cap is strict, so on failure a falsy result is
# returned and the caller leaves those issues out of the checkpoint to be
# retried on the next run.
GRAPHQL_BATCH_RETRIES       = 2
GRAPHQL_BATCH_RETRY_BACKOFF = 3.0


def run_graphql_retry(query):
    """Run a GraphQL query, retrying up to GRAPHQL_BATCH_RETRIES times on a
    falsy (failed or empty) result with a short increasing backoff."""
    data = run_graphql(query)
    attempt = 0
    while not data and attempt < GRAPHQL_BATCH_RETRIES:
        attempt += 1
        wait_s = GRAPHQL_BATCH_RETRY_BACKOFF * attempt
        print(f"    retrying empty/failed GraphQL batch "
              f"{attempt}/{GRAPHQL_BATCH_RETRIES} in {wait_s:.0f}s...")
        time.sleep(wait_s)
        data = run_graphql(query)
    return data


def extract_pr_round1(data, alias):
    """Extracts PRs from a Round 1 response (closing + cross)."""
    repo_data = data.get(alias)
    if not repo_data or not repo_data.get("issue"):
        return []
    issue_data = repo_data["issue"]
    seen_prs = {}

    # ConnectedEvent: explicit closures.
    for node in (issue_data.get("closingReferencesConnection") or {}).get("nodes", []):
        if not node:
            continue
        subj = node.get("subject") or {}
        if subj.get("number") and subj.get("url"):
            n = subj["number"]
            if n not in seen_prs:
                seen_prs[n] = {
                    "number": n,
                    "title": subj.get("title", ""),
                    "url": subj.get("url", ""),
                    "merged": subj.get("merged", False),
                    "merged_at": subj.get("mergedAt"),
                    "state": subj.get("state", ""),
                    "source": "closing_reference",
                }

    # CrossReferencedEvent: mentions.
    for node in (issue_data.get("crossReferences") or {}).get("nodes", []):
        if not node:
            continue
        src = node.get("source") or {}
        if src.get("number") and src.get("url"):
            n = src["number"]
            if n not in seen_prs:                  seen_prs[n] = {
                    "number": n,
                    "title": src.get("title", ""),
                    "url": src.get("url", ""),
                    "merged": src.get("merged", False),
                    "merged_at": src.get("mergedAt"),
                    "state": src.get("state", ""),
                    "source": "cross_reference",
                }
    return list(seen_prs.values())


def extract_pr_round2(data, alias):
    """Extracts PRs from a Round 2 response (REFERENCED_EVENT → commit → PR)."""
    repo_data = data.get(alias)
    if not repo_data or not repo_data.get("issue"):
        return []
    issue_data = repo_data["issue"]
    seen_prs = {}

    for node in (issue_data.get("referencedEvents") or {}).get("nodes", []):
        if not node:
            continue
        commit = node.get("commit") or {}
        for pr in (commit.get("associatedPullRequests") or {}).get("nodes", []):
            if not pr or not pr.get("number"):
                continue
            n = pr["number"]
            if n not in seen_prs:
                seen_prs[n] = {
                    "number": n,
                    "title": pr.get("title", ""),
                    "url": pr.get("url", ""),
                    "merged": pr.get("merged", False),
                    "merged_at": pr.get("mergedAt"),
                    "state": pr.get("state", ""),
                    "source": "referenced_event",
                }
    return list(seen_prs.values())


# GraphQL pass execution.

print("=" * 70)
print("PASS 1 — GraphQL (round 1: closing+cross, round 2: referenced)")
print("=" * 70)
print()

issues_to_process = [
    i for i in issues_list
    if f"{i['repo']}#{i['number']}" not in processed_issues
]
print(f"Issues to process: {len(issues_to_process)}")
print()

start_p1 = time.time()
p1_found = 0
p1_round2_extra = 0
issues_unresolved = []
# A "no PR" verdict is only valid if the issue was checked by both rounds.
# Issues are recorded here only when their GraphQL batch succeeded; those
# from a failed batch are retried on the next run.
_r1_seen: set = set()
_r2_seen: set = set()

# Round 1: closing and cross references.
issues_missed_round1 = []

try:
    for batch_start in range(0, len(issues_to_process), GRAPHQL_BATCH_SIZE):
        batch = issues_to_process[batch_start:batch_start + GRAPHQL_BATCH_SIZE]
        batch_num = (batch_start // GRAPHQL_BATCH_SIZE) + 1
        total_batches = (len(issues_to_process) + GRAPHQL_BATCH_SIZE - 1) // GRAPHQL_BATCH_SIZE

        print(f"  [R1 {batch_num}/{total_batches}] {len(batch)} issues...")

        query, alias_map = build_query_round1(batch)
        data = run_graphql_retry(query)

        if not data:
            print(f"    ⚠️ R1 query failed, {len(batch)} issues → R2.")
            issues_missed_round1.extend(batch)
            continue

        for alias, issue_info in alias_map.items():
            key = f"{issue_info['repo']}#{issue_info['number']}"
            _r1_seen.add(key)
            pr_list = extract_pr_round1(data, alias)
            pr_merged = [p for p in pr_list if p.get("merged")][:MAX_PR_PER_ISSUE]

            if pr_merged:
                p1_found += 1
                issue_output = build_issue_output(issue_info, pr_merged, "graphql")
                issues_with_pr.append(issue_output)
                processed_issues.add(key)
            else:
                issues_missed_round1.append(issue_info)

        save_progress()
        if batch_num % 10 == 0:
            elapsed = time.time() - start_p1
            print(f"\n  📊 R1 {batch_num}/{total_batches} | "
                  f"Found: {p1_found} | For R2: {len(issues_missed_round1)} | "
                  f"{elapsed:.1f}s\n")

except KeyboardInterrupt:
    print("\n  ⚠️ Interrupted — saving progress and exiting.")
    save_progress()
    raise

print()
print(f"  Round 1 complete: {p1_found} resolved, {len(issues_missed_round1)} → R2")
print()

# Round 2: referenced events on the issues still unresolved.
print(f"  Round 2 (REFERENCED_EVENT) on {len(issues_missed_round1)} issues...")

try:
    for batch_start in range(0, len(issues_missed_round1), GRAPHQL_BATCH_SIZE):
        batch = issues_missed_round1[batch_start:batch_start + GRAPHQL_BATCH_SIZE]
        batch_num = (batch_start // GRAPHQL_BATCH_SIZE) + 1
        total_batches = (len(issues_missed_round1) + GRAPHQL_BATCH_SIZE - 1) // GRAPHQL_BATCH_SIZE

        if batch_num % 5 == 0 or batch_num == 1:
            print(f"  [R2 {batch_num}/{total_batches}] {len(batch)} issues...")

        query, alias_map = build_query_round2(batch)
        data = run_graphql_retry(query)

        if not data:
            issues_unresolved.extend(batch)
            continue

        for alias, issue_info in alias_map.items():
            key = f"{issue_info['repo']}#{issue_info['number']}"
            _r2_seen.add(key)
            pr_list = extract_pr_round2(data, alias)
            pr_merged = [p for p in pr_list if p.get("merged")][:MAX_PR_PER_ISSUE]

            if pr_merged:
                p1_round2_extra += 1
                p1_found += 1
                issue_output = build_issue_output(issue_info, pr_merged, "graphql_r2")
                issues_with_pr.append(issue_output)
                processed_issues.add(key)
            else:
                issues_unresolved.append(issue_info)

        if batch_num % 5 == 0:
            save_progress()

except KeyboardInterrupt:
    print("\n  ⚠️ Interrupted — saving and exiting.")
    save_progress()
    raise

# Issues not resolved by the GraphQL pass are recorded as without_pr,
# keeping the same output schema.
p1_not_found = 0
for issue_info in issues_unresolved:
    key = f"{issue_info['repo']}#{issue_info['number']}"
    if key in processed_issues:
        continue
    # Only record "no PR" if the issue was seen by both rounds; otherwise it
    # is skipped so it stays out of processed_issues and is retried next run.
    if key not in _r1_seen or key not in _r2_seen:
        continue
    issues_without_pr.append({
        "repo": issue_info["repo"],
        "number": issue_info["number"],
        "title": issue_info.get("title", ""),
        "url": issue_info.get("url", ""),
        "strategy": issue_info.get("strategy", ""),
    })
    processed_issues.add(key)
    p1_not_found += 1

save_progress()

# An issue is done if it has a PR or was checked by both rounds; the rest
# failed transiently and are dropped with a log line.
_with_pr_keys = {f"{i['repo']}#{i['number']}" for i in issues_with_pr}
_complete_keys = _with_pr_keys | (_r1_seen & _r2_seen)
incomplete = [
    i for i in issues_to_process
    if f"{i['repo']}#{i['number']}" not in _complete_keys
]
if incomplete:
    examples = [f"{i['repo']}#{i['number']}" for i in incomplete[:10]]
    print()
    print("=" * 70)
    print(f"⚠️  PHASE 3: {len(incomplete)} issues were NOT fully checked "
          f"(a GraphQL batch failed on a transient error) and were dropped.")
    print(f"   Examples: {examples}")
    print("   Policy: continue anyway (lost data logged in run_stats._lost_items).")
    print("=" * 70)

elapsed_p1 = (time.time() - start_p1) + cumulative_elapsed_p1_seconds

print()
print(f"  {'─' * 50}")
print("  P1 SUMMARY")
print(f"  {'─' * 50}")
print(f"  Time:                    {elapsed_p1:.1f}s")
print(f"  Found (R1+R2):           {p1_found}")
print(f"    of which from R2:      {p1_round2_extra}")
print(f"  Unresolved (-> without_pr): {p1_not_found}")
print()


# Final summary.

# Cumulative wall-clock across this session and all previous jobs of the phase.
elapsed_total = (time.time() - _session_start) + cumulative_elapsed_total_seconds

print()
print("=" * 70)
print("FINAL SUMMARY — PHASE 3")
print("=" * 70)
print(f"  Total time:                  {elapsed_total:.1f}s")
print(f"  Input issues:                {len(issues_list)}")
print()
print(f"  P1 (GraphQL R1+R2):          {p1_found}")
print(f"    of which extra from R2:    {p1_round2_extra}")
print()
print("  TOTAL:")
print(f"    Issues with PR:            {len(issues_with_pr)}")
print(f"    Issues without PR:         {len(issues_without_pr)}")
print("    Success rate:              "
      f"{len(issues_with_pr)*100/max(len(issues_list),1):.1f}%")
print()
print(f"  Output:                      {OUTPUT_FILE}")
print(f"  Discarded:                   {DISCARDED_FILE}")
print("=" * 70)


# Run statistics.

from datetime import datetime as _dt

RUN_STATS_FILE = str(paths.step3_stats_json)

n_input = len(issues_list)
n_with_pr = len(issues_with_pr)
n_without_pr = len(issues_without_pr)


pr_per_issue_distribution = {"1": 0, "2": 0, "3": 0, "4": 0, "5+": 0}
pr_counts_per_issue = []
for issue in issues_with_pr:
    n_pr = len(issue.get("pr", []))
    pr_counts_per_issue.append(n_pr)
    if n_pr == 1:    pr_per_issue_distribution["1"] += 1
    elif n_pr == 2:  pr_per_issue_distribution["2"] += 1
    elif n_pr == 3:  pr_per_issue_distribution["3"] += 1
    elif n_pr == 4:  pr_per_issue_distribution["4"] += 1
    else:            pr_per_issue_distribution["5+"] += 1

days_issue_to_merge = []
for issue in issues_with_pr:
    creata = issue.get("created_at", "")
    pr_list = issue.get("pr", [])
    if not pr_list or not creata:
        continue
    for pr in pr_list:
        merged_at = pr.get("merged_at") or ""
        if not merged_at:
            continue
        try:
            data_creazione = _dt.fromisoformat(creata[:10])
            data_merge = _dt.fromisoformat(merged_at[:10])
            delta = (data_merge - data_creazione).days
            if delta >= 0:
                days_issue_to_merge.append(delta)
            break
        except Exception:
            continue

time_issue_to_merge = {
    "min_days": min(days_issue_to_merge) if days_issue_to_merge else 0,
    "max_days": max(days_issue_to_merge) if days_issue_to_merge else 0,
    "mean_days": round(sum(days_issue_to_merge) / max(len(days_issue_to_merge), 1), 1),
    "median_days": median(days_issue_to_merge),
    "sample_size": len(days_issue_to_merge),
}

pr_per_repo = {}
for issue in issues_with_pr:
    repo = issue.get("repo", "")
    pr_per_repo[repo] = pr_per_repo.get(repo, 0) + len(issue.get("pr", []))
top_10_repo = [
    {"repo": r, "prs_found": n,
     "issues_with_pr": sum(1 for i in issues_with_pr if i.get("repo") == r)}
    for r, n in sorted(pr_per_repo.items(), key=lambda kv: -kv[1])[:10]
]

issues_sorted_by_pr = sorted(issues_with_pr, key=lambda i: -len(i.get("pr", [])))
top_5_issue_multi_pr = [
    {"repo": i.get("repo", ""), "number": i.get("number", ""),
     "title": i.get("title", "")[:80], "n_pr": len(i.get("pr", [])),
     "url": i.get("url", "")}
    for i in issues_sorted_by_pr[:5] if len(i.get("pr", [])) >= 2
]

source_distribution = {}
for issue in issues_with_pr:
    f = issue.get("link_source", "unknown")
    source_distribution[f] = source_distribution.get(f, 0) + 1


run_stats = {
    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    # Uniform fields for external consumers.
    "_schema_version": 1,
    "_phase": 3,
        "_lost_items": [f"{i['repo']}#{i['number']}" for i in incomplete],
    "version": "graphql-p1",
    "total_time_seconds": round(elapsed_total, 1),
    "avg_time_per_issue_seconds": round(elapsed_total / max(n_input, 1), 2),
    "note": (
        "GraphQL-only linking (first:50, round 1 closing+cross, "
        "round 2 REFERENCED_EVENT)."
    ),
    "totals": {
        "input_issues": n_input,
        "issues_with_pr": n_with_pr,
        "issues_without_pr": n_without_pr,
        "success_rate_pct": round(n_with_pr * 100 / max(n_input, 1), 1),
        "total_prs_found": sum(pr_counts_per_issue),
    },
    "per_pass": {
        "P1_graphql": {
            # Counters derived from issues_with_pr by link_source, so they
            # include results from earlier jobs.
            "resolved_issues":
                source_distribution.get("graphql", 0)
                + source_distribution.get("graphql_r2", 0),
            "issue_round2_extra":
                source_distribution.get("graphql_r2", 0),
            "wall_clock_time_seconds": round(elapsed_p1, 1),
            "avg_time_per_issue_seconds": round(elapsed_p1 / max(n_input, 1), 3),
            "workers": 1,
            "contribution_pct": round(
                (source_distribution.get("graphql", 0)
                 + source_distribution.get("graphql_r2", 0)) * 100
                / max(n_with_pr, 1), 1),
        },
    },
    "prs_per_issue_distribution": pr_per_issue_distribution,
    "link_source_distribution": source_distribution,
    "time_issue_to_merge": time_issue_to_merge,
    "top_10_repo_per_pr": top_10_repo,
    "top_5_issue_multi_pr": top_5_issue_multi_pr,
}

dump_json_atomic(run_stats, RUN_STATS_FILE)
print(f"\n📊 Statistics saved to {RUN_STATS_FILE}")


print("🏁 Phase 3 complete.")