"""Centralized management of the pipeline paths.

All pipeline scripts import from here to know where to read and write
their files. The base path is determined by the experiment ID, resolved
in priority order from:

  1. the ``EXPERIMENT_ID`` environment variable;
  2. the ``current_experiment.txt`` file next to this module;
  3. the ``"exp_default"`` fallback.

Usage:
    from experiment_paths import paths

    INPUT_FILE  = paths.step1_repo_json
    OUTPUT_FILE = paths.step2_issues_json
    paths.ensure_step_dir(2)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# The experiments/ folder lives outside the pipeline/ package (at the
# repository root), so run data is kept separate from the code.
_HERE = Path(__file__).resolve().parent
_PIPELINE_ROOT = _HERE.parent
_DEFAULT_EXPERIMENTS_ROOT = _PIPELINE_ROOT.parent / "experiments"
_CURRENT_FILE = _HERE / "current_experiment.txt"


def _resolve_experiments_root() -> Path:
    """Determine the experiments root folder.

    The ``EXPERIMENTS_ROOT`` environment variable takes precedence; the
    default is ``<repo_root>/experiments/``.
    """
    env_root = os.getenv("EXPERIMENTS_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return _DEFAULT_EXPERIMENTS_ROOT


def _resolve_experiment_id() -> str:
    """Determine the active experiment ID (env, then file, then default)."""
    env_id = os.getenv("EXPERIMENT_ID")
    if env_id:
        return env_id.strip()

    if _CURRENT_FILE.exists():
        content = _CURRENT_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content

    return "exp_default"


def set_current_experiment(experiment_id: str) -> None:
    """Persist the chosen experiment ID to ``current_experiment.txt``."""
    _CURRENT_FILE.write_text(experiment_id, encoding="utf-8")


@dataclass(frozen=True)
class ExperimentPaths:
    """Canonical paths of an experiment.

    All paths are relative to the script's working directory.
    """
    experiment_id: str
    base: Path

    # Step 1 — discovery
    step1_dir: Path
    step1_repo_json: Path
    step1_stats_json: Path

    # Step 2 — issue extraction
    step2_dir: Path
    step2_issues_json: Path
    step2_stats_json: Path

    # Step 3 — PR linking
    step3_dir: Path
    step3_with_pr_json: Path
    step3_without_pr_json: Path
    step3_stats_json: Path

    # Step 4 — AI validation
    step4_dir: Path
    step4_validate_json: Path
    step4_discarded_json: Path
    step4_stats_json: Path

    # Step 5 — dataset build
    step5_dir: Path
    step5_checkpoint_json: Path
    step5_stats_json: Path

    config_json: Path

    def ensure_step_dir(self, step: int) -> None:
        """Create the folder of the given step if it does not exist."""
        d = {1: self.step1_dir, 2: self.step2_dir, 3: self.step3_dir,
             4: self.step4_dir, 5: self.step5_dir}[step]
        d.mkdir(parents=True, exist_ok=True)

    def load_config(self) -> dict:
        """Load the experiment's ``config.json``; returns ``{}`` if missing."""
        if self.config_json.exists():
            with self.config_json.open("r", encoding="utf-8") as f:
                return json.load(f)
        return {}

def _build_paths(experiment_id: str) -> ExperimentPaths:
    base = _resolve_experiments_root() / experiment_id

    s1 = base / "1_discovery"
    s2 = base / "2_issue_discovery"
    s3 = base / "3_pr_linking"
    s4 = base / "4_ai_validation"
    s5 = base / "5_dataset"

    return ExperimentPaths(
        experiment_id=experiment_id,
        base=base,
        step1_dir=s1,
        step1_repo_json=s1 / "repo_user_story.json",
        step1_stats_json=s1 / "run_stats.json",
        step2_dir=s2,
        step2_issues_json=s2 / "issues_user_story.json",
        step2_stats_json=s2 / "run_stats.json",
        step3_dir=s3,
        step3_with_pr_json=s3 / "issues_with_pr.json",
        step3_without_pr_json=s3 / "issues_without_pr.json",
        step3_stats_json=s3 / "run_stats.json",
        step4_dir=s4,
        step4_validate_json=s4 / "validated_issues.json",
        step4_discarded_json=s4 / "discarded_issues_ai.json",
        step4_stats_json=s4 / "run_stats.json",
        step5_dir=s5,
        step5_checkpoint_json=s5 / "_checkpoint.json",
        step5_stats_json=s5 / "_run_stats.json",
        config_json=base / "config.json",
    )


# Global instance imported by the pipeline scripts.
paths = _build_paths(_resolve_experiment_id())


def list_experiments(experiments_root: Optional[Path] = None) -> list[dict]:
    """Return the list of experiments found under the experiments root.

    When ``experiments_root`` is not given, the same folder used by the
    pipeline scripts is scanned. Each element has the shape
    ``{id, path, config, has_step5}``; a corrupt ``config.json`` is
    reported through an ``_error`` key instead of raising.
    """
    root = Path(experiments_root) if experiments_root else _resolve_experiments_root()

    if not root.exists():
        return []

    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue

        cfg_file = d / "config.json"
        cfg = {}
        if cfg_file.exists():
            try:
                with cfg_file.open("r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {"_error": "config.json could not be parsed"}
        out.append({
            "id": d.name,
            "path": str(d),
            "config": cfg,
            "has_step5": (d / "5_dataset" / "_checkpoint.json").exists(),
        })
    return out


if __name__ == "__main__":
    # Smoke test: print the current experiment and its paths.
    print(f"Current EXPERIMENT_ID:  {paths.experiment_id}")
    print(f"Base path:              {paths.base}")
    print(f"Step 1 repo:            {paths.step1_repo_json}")
    print(f"Step 5 dir:             {paths.step5_dir}")
    print()
    print(f"Available experiments: {[e['id'] for e in list_experiments()]}")
