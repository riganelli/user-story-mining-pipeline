"""
Loader for user story discovery pipeline output.

Supports TWO modes:
1) load_output(root)         — backwards compatible, accepts a direct folder
2) load_experiment(exp_id)   — new API based on `experiments/{exp_id}/`
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Expected folder structure for an experiment (or a legacy output/).
EXPECTED_STRUCTURE = {
    "1_discovery": ["repo_user_story.json", "run_stats.json"],
    "2_issue_discovery": ["issues_user_story.json", "run_stats.json"],
    "3_pr_linking": ["issues_with_pr.json", "issues_without_pr.json", "run_stats.json"],
    "4_ai_validation": ["validated_issues.json", "discarded_issues_ai.json", "run_stats.json"],
    "5_dataset": ["_checkpoint.json", "_run_stats.json"],
}


@dataclass
class PipelineOutput:
    """Container for all data loaded from a pipeline run."""
    root: Path

    # Experiment configuration, if present.
    experiment_id: str = ""
    config: dict = field(default_factory=dict)

    repo: list[dict] = field(default_factory=list)
    discovery_stats: dict = field(default_factory=dict)

    issues: list[dict] = field(default_factory=list)
    issue_extraction_stats: dict = field(default_factory=dict)

    issues_with_pr: list[dict] = field(default_factory=list)
    issues_without_pr: list[dict] = field(default_factory=list)
    pr_linking_stats: dict = field(default_factory=dict)

    validated_issues: list[dict] = field(default_factory=list)
    discarded_issues: list[dict] = field(default_factory=list)
    validation_stats: dict = field(default_factory=dict)

    dataset_checkpoint: dict = field(default_factory=dict)
    dataset_stats: dict = field(default_factory=dict)


def _read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def validate_output_dir(root: Path) -> list[str]:
    """Check that the expected structure is present. Return a list of errors (empty = ok)."""
    errors: list[str] = []
    if not root.exists():
        return [f"Folder '{root}' does not exist."]
    if not root.is_dir():
        return [f"'{root}' is not a folder."]

    for subdir, files in EXPECTED_STRUCTURE.items():
        sub_path = root / subdir
        if not sub_path.exists():
            errors.append(f"Missing subfolder: {subdir}/")
            continue
        for f in files:
            if not (sub_path / f).exists():
                errors.append(f"Missing file: {subdir}/{f}")
    return errors


def load_output(root: str | Path) -> PipelineOutput:
    """Load all JSON files from a folder (compatible with both legacy `output/`
    and `experiments/{id}/`)."""
    root = Path(root)
    errors = validate_output_dir(root)
    if errors:
        raise FileNotFoundError("Invalid output:\n  - " + "\n  - ".join(errors))

    data = PipelineOutput(root=root)

    # For an experiment, also populate experiment_id and config.
    cfg_file = root / "config.json"
    if cfg_file.exists():
        try:
            data.config = _read_json(cfg_file)
            data.experiment_id = data.config.get("experiment_id", root.name)
        except Exception:
            data.experiment_id = root.name
    else:
        # Legacy run without config: use the folder name.
        data.experiment_id = root.name

    data.repo = _read_json(root / "1_discovery" / "repo_user_story.json")
    data.discovery_stats = _read_json(root / "1_discovery" / "run_stats.json")

    data.issues = _read_json(root / "2_issue_discovery" / "issues_user_story.json")
    data.issue_extraction_stats = _read_json(root / "2_issue_discovery" / "run_stats.json")

    data.issues_with_pr = _read_json(root / "3_pr_linking" / "issues_with_pr.json")
    data.issues_without_pr = _read_json(root / "3_pr_linking" / "issues_without_pr.json")
    data.pr_linking_stats = _read_json(root / "3_pr_linking" / "run_stats.json")

    data.validated_issues = _read_json(root / "4_ai_validation" / "validated_issues.json")
    data.discarded_issues = _read_json(root / "4_ai_validation" / "discarded_issues_ai.json")
    data.validation_stats = _read_json(root / "4_ai_validation" / "run_stats.json")

    data.dataset_checkpoint = _read_json(root / "5_dataset" / "_checkpoint.json")
    data.dataset_stats = _read_json(root / "5_dataset" / "_run_stats.json")

    return data


def list_experiments(experiments_root: str | Path = "experiments") -> list[dict]:
    """Return the experiments found. Each is a dict with id, path, config,
    has_step5 (useful for filtering incomplete runs)."""
    root = Path(experiments_root)
    if not root.exists():
        return []

    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        cfg_file = d / "config.json"
        cfg: dict = {}
        if cfg_file.exists():
            try:
                with cfg_file.open("r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {"_error": "config.json not parsable"}
        out.append({
            "id": d.name,
            "path": str(d),
            "config": cfg,
            "has_step5": (d / "5_dataset" / "_checkpoint.json").exists(),
        })
    return out


def load_experiment(experiment_id: str, experiments_root: str | Path = "experiments") -> PipelineOutput:
    """Shortcut to load an experiment by id."""
    root = Path(experiments_root) / experiment_id
    return load_output(root)