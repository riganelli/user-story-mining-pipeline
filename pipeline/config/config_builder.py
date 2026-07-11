"""Centralized construction of an experiment's ``config.json``.

Builds and validates the canonical configuration shape used by both the
CLI (``new_experiment.py``) and the Streamlit launcher, so the two entry
points always produce the same structure.

Usage:
    from config_builder import build_config, write_config, VALID_STRATEGIES

    cfg = build_config(
        "exp_001_baseline",
        description="Baseline run",
        max_repo=100,
        strategies=["LABEL", "CONNEXTRA"],
    )
    target = write_config(cfg, Path("experiments"), force=False)
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


# Valid discovery strategies and run modes.
VALID_STRATEGIES = {"LABEL", "TEMPLATE", "TAG", "MENTION", "CONNEXTRA"}
VALID_RUN_MODES = {"testing", "full_coverage"}


def build_config(
    experiment_id: str,
    description: str = "",
    *,
    # Discovery (Phase 1)
    max_repo: int | None = 100,
    max_per_strategy: int | None = 20,
    min_stars: int = 2,
    languages: list[str] | None = None,
    language_mode: str = "primary_only",
    min_lang_pct: float = 15.0,
    strategies: list[str] | None = None,
    time_windows_granularity: str = "yearly",
    time_windows_custom_days: int = 30,
    max_pages_override: int | None = None,
    # Dataset (Phase 2 and 5)
    min_language_files: int = 0,
    min_language_fraction: float = 0.0,
    cutoff_date_closed: str | None = None,
    cutoff_date_closed_end: str | None = None,
    max_issue_per_repo: int | None = 15,
    # Exclusions (Phase 1, 2 and 4)
    exclude_from: list[str] | None = None,
    exclude_issues_from: list[str] | None = None,
    exclude_rejected_from: list[str] | None = None,
    exclude_validated_from: list[str] | None = None,
    # Run mode (metadata; the caller forces the caps for full_coverage)
    run_mode: str = "testing",
) -> dict:
    """Build the ``config.json`` dict with full validation.

    Args:
        experiment_id: experiment identifier (used as the folder name).
        description: free-text description.
        max_repo: total repository cap (phase 1), or None for no cap.
        max_per_strategy: per-strategy repository cap (phase 1).
        min_stars: minimum stars required to accept a repository.
        languages: allowed languages (default: ["Java", "Python"]).
        language_mode: "primary_only" or "any_above_threshold".
        min_lang_pct: byte-percentage threshold for "any_above_threshold".
        strategies: subset of VALID_STRATEGIES (default: all).
        min_language_files: minimum language-file count per repo (phase 2).
        min_language_fraction: minimum language-file fraction in [0, 1].
        cutoff_date_closed: lower bound (YYYY-MM-DD) on closed_at (phase 2).
        cutoff_date_closed_end: upper bound (YYYY-MM-DD) on closed_at.
        max_issue_per_repo: issue cap per repository, or None for no cap.
        exclude_*: lists of experiment IDs whose results are excluded.

    Raises:
        ValueError: if any parameter is invalid.
    """
    languages = languages or ["Java", "Python"]
    strategies = [s.upper() for s in (strategies or list(VALID_STRATEGIES))]

    invalid = [s for s in strategies if s not in VALID_STRATEGIES]
    if invalid:
        raise ValueError(
            f"Invalid strategies: {invalid}. "
            f"Valid strategies: {sorted(VALID_STRATEGIES)}"
        )

    if language_mode not in ("primary_only", "any_above_threshold"):
        raise ValueError(
            f"Invalid language_mode: {language_mode}. "
            "Expected: 'primary_only' or 'any_above_threshold'"
        )

    # min_lang_pct is only meaningful for the threshold-based mode.
    if language_mode == "any_above_threshold":
        if not (0.0 < min_lang_pct <= 100.0):
            raise ValueError(
                f"min_lang_pct out of range (0, 100]: {min_lang_pct}"
            )

    if min_language_files < 0:
        raise ValueError(f"min_language_files is negative: {min_language_files}")
    if not (0.0 <= min_language_fraction <= 1.0):
        raise ValueError(
            f"min_language_fraction out of [0,1]: {min_language_fraction}"
        )

    if time_windows_granularity not in ("yearly", "semester", "quarter", "monthly", "custom"):
        raise ValueError(
            f"Invalid time_windows_granularity: {time_windows_granularity}. "
            "Expected: yearly, semester, quarter, monthly, or custom."
        )
    if time_windows_granularity == "custom":
        if not isinstance(time_windows_custom_days, int) or time_windows_custom_days < 1:
            raise ValueError(
                f"time_windows_custom_days='{time_windows_custom_days}' is invalid. "
                "Expected: integer >= 1 (number of days per window)."
            )

    if run_mode not in VALID_RUN_MODES:
        raise ValueError(
            f"Invalid run_mode='{run_mode}'. Expected: {sorted(VALID_RUN_MODES)}"
        )

    # Nullable caps: each must be None (= no cap) or int >= 1.
    if max_repo is not None and (not isinstance(max_repo, int) or max_repo < 1):
        raise ValueError(f"Invalid max_repo='{max_repo}'. Expected: int >= 1, or None.")
    if max_per_strategy is not None and (not isinstance(max_per_strategy, int) or max_per_strategy < 1):
        raise ValueError(f"Invalid max_per_strategy='{max_per_strategy}'. Expected: int >= 1, or None.")

    # max_pages_override: None means per-strategy defaults.
    if max_pages_override is not None:
        if not isinstance(max_pages_override, int) or max_pages_override < 1:
            raise ValueError(
                f"Invalid max_pages_override='{max_pages_override}'. Expected: int >= 1, or None."
            )

    # cutoff_date_closed / _end validation: each None, "", or YYYY-MM-DD.
    # Together they define the interval [start, end] on closed_at (Phase 2);
    # either bound may be None (→ open on that side). "" is normalized to None.
    def _norm_date(val, label):
        if val is None:
            return None
        val = str(val).strip()
        if val == "":
            return None
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", val):
            raise ValueError(
                f"{label}='{val}' is invalid. "
                "Expected format: YYYY-MM-DD (e.g. '2025-02-01')"
            )
        return val

    cutoff_date_closed     = _norm_date(cutoff_date_closed, "cutoff_date_closed")
    cutoff_date_closed_end = _norm_date(cutoff_date_closed_end, "cutoff_date_closed_end")
    if (cutoff_date_closed and cutoff_date_closed_end
            and cutoff_date_closed_end < cutoff_date_closed):
        raise ValueError(
            f"cutoff_date_closed_end ('{cutoff_date_closed_end}') is before "
            f"cutoff_date_closed ('{cutoff_date_closed}')."
        )

    if max_issue_per_repo is not None:
        if not isinstance(max_issue_per_repo, int) or max_issue_per_repo < 1:
            raise ValueError(
                f"max_issue_per_repo='{max_issue_per_repo}' is invalid. "
                "Expected: integer >= 1, or None for no cap."
            )

    inactive_strategies = sorted(VALID_STRATEGIES - set(strategies))

    discovery_cfg: dict = {
        "max_total_repos": max_repo,
        "max_repos_per_strategy": max_per_strategy,
        "min_stars": min_stars,
        "languages": languages,
        "language_mode": language_mode,
        "min_lang_pct": min_lang_pct,
        "active_strategies": strategies,
        "inactive_strategies": inactive_strategies,
        "time_windows_granularity": time_windows_granularity,
        "time_windows_custom_days": time_windows_custom_days,
    }
    # Written only when explicitly set, so configs relying on the
    # per-strategy defaults stay minimal.
    if max_pages_override is not None:
        discovery_cfg["max_pages_override"] = max_pages_override

    return {
        "experiment_id": experiment_id,
        "created_at": datetime.now().isoformat(),
        "description": description,
        "run_mode": run_mode,
        "discovery_config": discovery_cfg,
        "dataset_config": {
            "min_language_files": min_language_files,
            "min_language_fraction": min_language_fraction,
            "cutoff_date_closed": cutoff_date_closed,
            "cutoff_date_closed_end": cutoff_date_closed_end,
            "max_issues_per_repo": max_issue_per_repo,
        },
        "exclude_from_experiments": exclude_from or [],
        "exclude_issues_from_experiments": exclude_issues_from or [],
        "exclude_rejected_issues_from": exclude_rejected_from or [],
        "exclude_validated_issues_from": exclude_validated_from or [],
    }


def write_config(
    config: dict,
    experiments_root: Path,
    *,
    force: bool = False,
) -> Path:
    """Write the config.json into the experiment folder.

    Creates the `experiments_root/{experiment_id}/` folder if it does not
    exist and writes `config.json` inside it.

    Parameters:
      config           — dict produced by build_config()
      experiments_root — experiments root folder (e.g. Path("experiments"))
      force            — if True, overwrites an existing folder

    Returns the path of the created folder.
    Raises FileExistsError if the folder already exists and force=False.
    """
    exp_id = config["experiment_id"]
    target = experiments_root / exp_id
    if target.exists() and not force:
        raise FileExistsError(
            f"Experiment '{exp_id}' already exists in {target}"
        )
    target.mkdir(parents=True, exist_ok=True)
    cfg_file = target / "config.json"
    with cfg_file.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return target