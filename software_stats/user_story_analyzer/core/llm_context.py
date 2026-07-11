"""
Builds a structured, compact context from the pipeline statistics,
to pass to the LLM as the basis for answering questions.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any


def build_context(data) -> dict[str, Any]:
    """
    Build a dictionary with all aggregated statistics of the run.
    Designed to be serialized to JSON and passed to the LLM.
    """
    ctx: dict[str, Any] = {}

    disc_cfg = data.discovery_stats.get("configuration", {})
    ctx["configuration"] = {
        "min_stars": disc_cfg.get("min_stars"),
        "max_repos_per_strategy": disc_cfg.get("max_repos_per_strategy"),
        "max_total_repos": disc_cfg.get("max_total_repos"),
        "allowed_languages": disc_cfg.get("allowed_languages"),
        "ai_model": data.validation_stats.get("model"),
        "timestamp_discovery": data.discovery_stats.get("timestamp"),
        "timestamp_validation": data.validation_stats.get("timestamp"),
    }

    # Step 1 with derived per-repo metrics.
    per_strat_raw = data.discovery_stats.get("per_strategy", {})
    per_strat_enriched = {}
    for name, s in per_strat_raw.items():
        accepted = s.get("accepted", 0) or 0
        elapsed = s.get("time_seconds", 0) or 0
        issue_scorse = s.get("issues_scanned", 0) or 0
        per_strat_enriched[name] = {
            **s,
            "seconds_per_accepted_repo": round(elapsed / accepted, 2) if accepted else None,
            "pct_issues_accepted_from_github_search": (
                round(accepted / issue_scorse * 100, 2) if issue_scorse else None
            ),
        }

    ctx["step_1_discovery"] = {
        "time_seconds": data.discovery_stats.get("total_time_seconds"),
        "unique_repos_seen": data.discovery_stats.get("global_totals", {}).get("unique_repos_seen"),
        "accepted_repos": len(data.repo),
        "discarded_stars": data.discovery_stats.get("global_totals", {}).get("discarded_stars"),
        "discarded_language": data.discovery_stats.get("global_totals", {}).get("discarded_language"),
        "discarded_regex": data.discovery_stats.get("global_totals", {}).get("discarded_regex"),
        "per_strategy": per_strat_enriched,
    }

    ctx["step_2_issue_extraction"] = {
        "total_candidate_issues": len(data.issues),
        "per_strategy": dict(Counter(i.get("strategy") for i in data.issues)),
    }

    ctx["step_3_pr_linking"] = {
        "issues_with_pr": len(data.issues_with_pr),
        "issues_without_pr": len(data.issues_without_pr),
        "with_pr_rate_pct": round(
            len(data.issues_with_pr) / max(len(data.issues), 1) * 100, 1
        ),
    }

    vt = data.validation_stats.get("totals", {})
    val_per_strat = data.validation_stats.get("validated_per_discovery_strategy", {})
    discard_per_strat = data.validation_stats.get("discarded_per_discovery_strategy", {})

    ctx["step_4_ai_validation"] = {
        "time_seconds": data.validation_stats.get("total_time_seconds"),
        "input_issues": vt.get("input_issues"),
        "validated_issues": vt.get("validated_issues"),
        "discarded_issues": vt.get("discarded_issues"),
        "global_ai_validation_rate_pct": vt.get("validation_rate_pct"),
        "ollama_errors": vt.get("ollama_errors"),
        "connextra_distribution": data.validation_stats.get("connextra_distribution", {}),
        "confidence_distribution": data.validation_stats.get("confidence_distribution", {}),
        "metadata_in_validated": data.validation_stats.get("metadata_in_validated", {}),
        "validated_per_strategy": val_per_strat,
        "discarded_per_strategy": discard_per_strat,
    }

    ck_stats = data.dataset_checkpoint.get("stats", {})
    ctx["step_5_dataset"] = {
        "total_input": ck_stats.get("total_input"),
        "completed_last_run": ck_stats.get("completed"),
        "skipped_from_cache": ck_stats.get("skipped_checkpoint"),
        "failed": ck_stats.get("failed"),
        "total_processed": len(data.dataset_checkpoint.get("processed", [])),
    }

    # Per-strategy summary table gathering the key metrics in one structure,
    # so the LLM does not have to cross-reference the steps.
    riepilogo = {}
    for strat in per_strat_raw.keys():
        s1 = per_strat_enriched.get(strat, {})
        validate = val_per_strat.get(strat, 0)
        discarded = discard_per_strat.get(strat, 0)
        total_ai = validate + discarded
        riepilogo[strat] = {
            "accepted_repos_step1": s1.get("accepted", 0),
            "seconds_per_repo_step1": s1.get("seconds_per_accepted_repo"),
            "pct_accepted_from_github_step1": s1.get("pct_issues_accepted_from_github_search"),
            "issues_validated_by_ai_step4": validate,
            "issues_discarded_by_ai_step4": discarded,
            "ai_validation_rate_pct": (
                round(validate / total_ai * 100, 1) if total_ai else None
            ),
        }
    ctx["summary_per_strategy"] = riepilogo

    lang_dist = Counter(r.get("language") for r in data.repo)
    stars = [r.get("stars", 0) for r in data.repo]
    ctx["repo_characteristics"] = {
        "languages": dict(lang_dist),
        "mean_stars": round(sum(stars) / len(stars), 1) if stars else 0,
        "median_stars": sorted(stars)[len(stars) // 2] if stars else 0,
        "max_stars": max(stars) if stars else 0,
        "min_stars": min(stars) if stars else 0,
    }

    repo_counter = Counter()
    for k in data.dataset_checkpoint.get("processed", []):
        if "#" in k:
            repo = k.rsplit("#", 1)[0]
            repo_counter[repo] += 1
    ctx["top_repo_dataset"] = [
        {"repo": r, "n_issues": n}
        for r, n in repo_counter.most_common(10)
    ]

    return ctx


def context_to_prompt_string(ctx: dict[str, Any]) -> str:
    """Serialize the context as indented JSON for the prompt."""
    return json.dumps(ctx, indent=2, ensure_ascii=False)