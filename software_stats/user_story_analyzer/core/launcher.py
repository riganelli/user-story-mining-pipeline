"""Pipeline launch logic from Streamlit.

Builds an experiment's ``config.json``, launches ``run_pipeline.py`` as a
detached subprocess and exposes helpers to read the live run state, tail
the logs and list the active runs shown in the dashboard.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

VALID_STRATEGIES = {"LABEL", "TEMPLATE", "TAG", "MENTION", "CONNEXTRA"}
VALID_RUN_MODES = {"testing", "full_coverage"}
VALID_GRANULARITY = {"yearly", "semester", "quarter", "monthly", "custom"}


# config.json generation.

def build_config(
    experiment_id: str,
    description: str = "",
    *,
    max_repo: int | None = 100,
    max_per_strategy: int | None = 20,
    min_stars: int = 2,
    languages: list[str] | None = None,
    language_mode: str = "primary_only",
    min_lang_pct: float = 15.0,
    strategies: list[str] | None = None,
    min_language_files: int = 0,
    min_language_fraction: float = 0.0,
    cutoff_date_closed: str | None = None,
    cutoff_date_closed_end: str | None = None,
    max_issue_per_repo: int | None = 15,
    exclude_from: list[str] | None = None,
    exclude_issues_from: list[str] | None = None,
    exclude_rejected_from: list[str] | None = None,
    exclude_validated_from: list[str] | None = None,
    # Run mode and full-coverage parameters.
    run_mode: str = "testing",
    time_windows_granularity: str = "yearly",
    time_windows_custom_days: int = 30,
    max_pages_override: int | None = None,
) -> dict:
    """Build the config.json dict.

    Mirrors the new_experiment.py logic in-process so the UI does not need
    to shell out.

    Parameter `cutoff_date_closed`:
      Time filter applied in Phase 2 (issue extraction). If set
      (format 'YYYY-MM-DD'), only issues closed on or after that
      date are included: closed_at >= cutoff_date_closed. Issues closed
      earlier are excluded from the dataset. If None/empty → filter disabled.

    Parameter `run_mode`:
      "testing" (default) → legacy behavior, all caps configurable.
      "full_coverage"     → exhaustive run, no caps on repos, narrow time
                            windows, max_pages at the GitHub ceiling.
      It is only a metadata flag: the real values that change the pipeline
      are in the specific fields (max_repo, max_per_strategy, etc.). The UI
      is responsible for forcing them to None / appropriate values when
      "full_coverage" is selected.

    Parameter `max_repo` / `max_per_strategy`:
      int >= 1 → classic cap.
      None     → "no cap": phase 1 stops only when GitHub queries
                 exhaust their results. Used in run_mode="full_coverage".

    Parameter `time_windows_granularity`:
      "yearly"   (default) → 10 yearly windows (historical behavior)
      "semester" → 19 semester windows (more coverage, more API calls)
      "quarter"  → 37 quarterly windows (max coverage, slower)

    Parameter `max_pages_override`:
      None (default) → phase 2 uses the hardcoded max_pages per strategy.
      int >= 1       → overrides max_pages of ALL phase-2 strategies.
                       Typically 10 in full_coverage (GitHub ceiling).
    """
    languages = languages or ["Java", "Python"]
    strategies = [s.upper() for s in (strategies or list(VALID_STRATEGIES))]

    invalid = [s for s in strategies if s not in VALID_STRATEGIES]
    if invalid:
        raise ValueError(f"Invalid strategies: {invalid}")
    if language_mode not in ("primary_only", "any_above_threshold"):
        raise ValueError(f"Invalid language_mode: {language_mode}")
    if language_mode == "any_above_threshold" and not (0 < min_lang_pct <= 100):
        raise ValueError(f"min_lang_pct out of range: {min_lang_pct}")
    if min_language_files < 0:
        raise ValueError(f"Negative min_language_files: {min_language_files}")
    if not (0.0 <= min_language_fraction <= 1.0):
        raise ValueError(f"min_language_fraction out of [0,1]: {min_language_fraction}")

    if run_mode not in VALID_RUN_MODES:
        raise ValueError(
            f"Invalid run_mode='{run_mode}'. Expected: {sorted(VALID_RUN_MODES)}"
        )

    if time_windows_granularity not in VALID_GRANULARITY:
        raise ValueError(
            f"Invalid time_windows_granularity='{time_windows_granularity}'. "
            f"Expected: {sorted(VALID_GRANULARITY)}"
        )
    # time_windows_custom_days is only meaningful for "custom".
    if time_windows_granularity == "custom":
        if not isinstance(time_windows_custom_days, int) or time_windows_custom_days < 1:
            raise ValueError(
                f"Invalid time_windows_custom_days='{time_windows_custom_days}'. "
                "Expected: int >= 1 (number of days per window)."
            )

    if max_repo is not None:
        if not isinstance(max_repo, int) or max_repo < 1:
            raise ValueError(
                f"Invalid max_repo='{max_repo}'. Expected: int >= 1, or None."
            )
    if max_per_strategy is not None:
        if not isinstance(max_per_strategy, int) or max_per_strategy < 1:
            raise ValueError(
                f"Invalid max_per_strategy='{max_per_strategy}'. "
                "Expected: int >= 1, or None."
            )

    if max_pages_override is not None:
        if not isinstance(max_pages_override, int) or max_pages_override < 1:
            raise ValueError(
                f"Invalid max_pages_override='{max_pages_override}'. "
                "Expected: int >= 1, or None."
            )

    # cutoff_date_closed and _end are each None, "" or YYYY-MM-DD and define
    # the interval on closed_at; either bound may be open, and "" becomes None.
    def _norm_cutoff(val, label):
        if val is None:
            return None
        val = str(val).strip()
        if val == "":
            return None
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", val):
            raise ValueError(
                f"Invalid {label}='{val}'. Expected format: YYYY-MM-DD (e.g. '2025-02-01')"
            )
        return val

    cutoff_date_closed     = _norm_cutoff(cutoff_date_closed, "cutoff_date_closed")
    cutoff_date_closed_end = _norm_cutoff(cutoff_date_closed_end, "cutoff_date_closed_end")
    if (cutoff_date_closed and cutoff_date_closed_end
            and cutoff_date_closed_end < cutoff_date_closed):
        raise ValueError(
            f"cutoff_date_closed_end ('{cutoff_date_closed_end}') is before "
            f"cutoff_date_closed ('{cutoff_date_closed}')."
        )

    # max_issue_per_repo is None (no cap) or an int >= 1.
    if max_issue_per_repo is not None:
        if not isinstance(max_issue_per_repo, int) or max_issue_per_repo < 1:
            raise ValueError(
                f"Invalid max_issue_per_repo='{max_issue_per_repo}'. "
                "Expected: int >= 1, or None for no cap."
            )

    inactive_strategies = sorted(VALID_STRATEGIES - set(strategies))

    # discovery_config always includes time_windows_granularity, and
    # max_pages_override only when set, to keep legacy configs clean.
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


def write_config(config: dict, experiments_root: Path, *, force: bool = False) -> Path:
    """Write config.json into the experiment folder.

    Returns the path of the created folder.
    Raises FileExistsError if the folder already exists and force=False.
    """
    exp_id = config["experiment_id"]
    target = experiments_root / exp_id
    if target.exists() and not force:
        raise FileExistsError(f"Experiment '{exp_id}' already exists at {target}")
    target.mkdir(parents=True, exist_ok=True)
    cfg_file = target / "config.json"
    with cfg_file.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return target


# Pipeline launch.

@dataclass
class RuntimeInfo:
    """Info about a running or finished run, persisted to _runtime.json."""
    experiment_id: str
    pid: int
    started_at: str
    cwd: str
    cmd: list[str]
    finished_at: str | None = None
    exit_code: int | None = None


def _runtime_file(experiment_dir: Path) -> Path:
    return experiment_dir / "_runtime.json"


def save_runtime(experiment_dir: Path, info: RuntimeInfo) -> None:
    with _runtime_file(experiment_dir).open("w", encoding="utf-8") as f:
        json.dump(info.__dict__, f, indent=2)


def load_runtime(experiment_dir: Path) -> RuntimeInfo | None:
    f = _runtime_file(experiment_dir)
    if not f.exists():
        return None
    try:
        with f.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Tolerate extra fields in the JSON (the queue worker may add
        # worker_pid, queue_position, etc.) so RuntimeInfo(**data) does not
        # raise and the run is not shown as "not started".
        from dataclasses import fields
        valid_keys = {f.name for f in fields(RuntimeInfo)}
        clean_data = {k: v for k, v in data.items() if k in valid_keys}
        return RuntimeInfo(**clean_data)
    except Exception:
        return None


def is_pid_alive(pid: int) -> bool:
    """Check whether a process is still alive (cross-platform best-effort)."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            # On Windows, use tasklist.
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL, timeout=5,
            )
            return str(pid) in out.decode("utf-8", errors="ignore")
        else:
            os.kill(pid, 0)  # Signal 0 tests existence.
            return True
    except (OSError, subprocess.SubprocessError):
        return False


def reset_experiment_runtime(experiment_dir: Path) -> None:
    """Clean up artifacts of a previous run BEFORE launching a new one.

    Deletes all runtime state files, phase metrics, and logs,
    WITHOUT touching config.json. This way the live dashboard does not show
    KPIs / progress / logs from the previous run as if they belonged to the new one.

    Idempotent: if the folder does not exist or is already clean, does nothing.
    Does NOT delete raw data (e.g. issues_user_story.json) to avoid losing
    intermediate results if the run is intentionally restarted with force.
    Only deletes files the dashboard uses to display live state.
    """
    if not experiment_dir.exists():
        return

    # State indicator files reset on every new run.
    indicators = [
        experiment_dir / "_done.txt",
        experiment_dir / "pipeline_state.json",
        experiment_dir / "_runtime.json",
        experiment_dir / "_pipeline.log",
        experiment_dir / "_bootstrap.log",
    ]
    for p in indicators:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass

    # Remove each phase's run_stats.json, otherwise _phase_metrics would
    # read the previous run's final KPIs as if they were the new run's.
    for subdir, filename in PHASE_FILES.values():
        stats_file = experiment_dir / subdir / filename
        try:
            if stats_file.exists():
                stats_file.unlink()
        except OSError:
            pass

    logs_dir = experiment_dir / "_logs"
    if logs_dir.exists():
        for log_file in logs_dir.glob("phase_*.log"):
            try:
                log_file.unlink()
            except OSError:
                pass


def launch_pipeline(
    experiment_id: str,
    pipeline_cwd: Path,
    *,
    no_parallel: bool = False,
    skip_phases: list[int] | None = None,
    experiments_root: Path | None = None,
) -> RuntimeInfo:
    """Launch run_pipeline.py as a detached subprocess.

    pipeline_cwd: folder where the scripts live (1_discovery.py etc.).
    experiments_root: where experiments live (for writing the bootstrap log).
    Returns RuntimeInfo with PID for later monitoring/killing.

    NOTE: uses the EXPERIMENT_ID env var, does NOT write current_experiment.txt
    (so parallel Streamlit sessions don't step on each other).

    Subprocess stdout/stderr are captured into a `_bootstrap.log` file
    inside the experiment folder, so that if the script crashes before
    opening its own logger (e.g. ImportError, missing env), a trace of
    the error is still available.
    """
    skip_phases = skip_phases or []

    # Clean up the previous run's artifacts in the same folder, so the live
    # dashboard does not show stale KPIs, progress or logs (and a stale
    # _done.txt does not keep the run marked as completed).
    if experiments_root is not None:
        reset_experiment_runtime(experiments_root / experiment_id)

    # run_pipeline.py lives in pipeline/orchestration/.
    cmd = [sys.executable, "-u", "orchestration/run_pipeline.py"]
    for n in skip_phases:
        cmd.append(f"--skip-{n}")
    if no_parallel:
        cmd.append("--no-parallel")

    env = os.environ.copy()
    env["EXPERIMENT_ID"] = experiment_id
    # Force UTF-8 output so non-ASCII characters print on Windows.
    env["PYTHONIOENCODING"] = "utf-8"

    # Bootstrap log for the subprocess stdout/stderr, opened before launch.
    bootstrap_log = None
    if experiments_root is not None:
        exp_dir = experiments_root / experiment_id
        exp_dir.mkdir(parents=True, exist_ok=True)
        bootstrap_path = exp_dir / "_bootstrap.log"
        # Append so subprocess re-launches do not overwrite the log.
        bootstrap_log = bootstrap_path.open("a", encoding="utf-8", buffering=1)
        bootstrap_log.write(f"\n{'='*60}\n")
        bootstrap_log.write(f"Subprocess started at {datetime.now().isoformat()}\n")
        bootstrap_log.write(f"CMD: {' '.join(cmd)}\n")
        bootstrap_log.write(f"CWD: {pipeline_cwd}\n")
        bootstrap_log.write(f"EXPERIMENT_ID env: {experiment_id}\n")
        bootstrap_log.write(f"{'='*60}\n")
        bootstrap_log.flush()

    # Subprocess detach. On Windows only CREATE_NO_WINDOW is used: it hides
    # the console and lets the subprocess outlive the Streamlit UI, without
    # the crashes caused by CREATE_NEW_PROCESS_GROUP or the empty console
    # opened by DETACHED_PROCESS on Python 3.13.
    if sys.platform == "win32":
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        kwargs = {"start_new_session": True}

    proc = subprocess.Popen(
        cmd,
        cwd=str(pipeline_cwd),
        env=env,
        stdout=bootstrap_log if bootstrap_log else subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )

    info = RuntimeInfo(
        experiment_id=experiment_id,
        pid=proc.pid,
        started_at=datetime.now().isoformat(),
        cwd=str(pipeline_cwd),
        cmd=cmd,
    )
    return info


def stop_pipeline(info: RuntimeInfo) -> bool:
    """Terminate a launched pipeline. Cross-platform best-effort.

    WINDOWS: uses `taskkill /F /T /PID` to terminate the process and its entire
    tree of children (phases 3/4/5 running in parallel as sub-processes).
    CTRL_BREAK_EVENT is not used because it requires CREATE_NEW_PROCESS_GROUP
    on the Popen side, which is incompatible with this launch setup.
    UNIX: SIGTERM to the process group, propagates correctly to children.
    """
    if not is_pid_alive(info.pid):
        return False
    try:
        if sys.platform == "win32":
            # /T kills the process tree, /F forces it.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(info.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # SIGTERM to the process group so children are terminated too.
            os.killpg(os.getpgid(info.pid), signal.SIGTERM)
        return True
    except (OSError, subprocess.SubprocessError):
        # Fallback: kill only the parent.
        try:
            os.kill(info.pid, signal.SIGTERM)
            return True
        except OSError:
            return False


# Live state read during execution.

@dataclass
class LiveState:
    """Snapshot of a pipeline's state at a given moment.

    Read from pipeline_state.json + run_stats.json of each phase.
    """
    experiment_id: str
    is_running: bool
    pid: int | None = None
    pipeline_done: bool = False
    phases_done: dict[int, bool] = field(default_factory=dict)
    phase_metrics: dict[int, dict] = field(default_factory=dict)
    log_tail: list[str] = field(default_factory=list)
    elapsed_sec: float = 0.0


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        # Race with the pipeline write; the next poll will be fine.
        return None


def _phase_metrics(stats: dict, phase: int) -> dict:
    """Extract compact KPIs from a phase's run_stats.json for the live dashboard."""
    if not stats:
        return {}
    if phase == 1:
        tot = stats.get("global_totals", {})
        return {
            "Accepted repos": tot.get("accepted_repos", 0),
            "Seen repos": tot.get("unique_repos_seen", 0),
            "Language drops": tot.get("discarded_language", 0),
            "Time (s)": stats.get("total_time_seconds", 0),
        }
    if phase == 2:
        # Read from totals.*, with a fallback to the older global_totals
        # schema for backward compatibility.
        tot = stats.get("totals", {})
        return {
            "Issues found": tot.get("issues_found",
                             stats.get("global_totals", {}).get("issue_accettate", 0)),
            "Repos processed": tot.get("processed_repos", 0),
            "Time (s)": stats.get("total_time_seconds", 0),
        }
    if phase == 3:
        # Keys are nested under totals.*, not at the root.
        tot = stats.get("totals", {})
        return {
            "With PR": tot.get("issues_with_pr", stats.get("issues_with_pr", 0)),
            "Without PR": tot.get("issues_without_pr", stats.get("issues_without_pr", 0)),
            "Time (s)": stats.get("total_time_seconds", 0),
        }
    if phase == 4:
        # totals.validated_issues / totals.discarded_issues.
        tot = stats.get("totals", {})
        return {
            "Validated": tot.get("validated_issues", tot.get("validate", 0)),
            "Rejected": tot.get("discarded_issues", tot.get("discarded", 0)),
            "Rate %": tot.get("validation_rate_pct", 0),
            "Time (s)": stats.get("total_time_seconds", 0),
        }
    if phase == 5:
        return {
            "Completed": stats.get("completed", 0),
            "Permanent skips": stats.get("permanent_skip", 0),
            "Failed": stats.get("failed", 0),
            "Time (s)": stats.get("total_time_seconds", 0),
        }
    return {}


def _phase_metrics_live(experiment_dir: Path, phase: int) -> dict:
    """KPIs extracted from DATA files while the phase is still running.

    Phases 2/3/5 write their files incrementally, so counting elements
    gives a sense of real-time progress.
    Phases 1 and 4 write in a single block at the end of the phase, so
    here we return {} (the UI will show 'waiting...').
    """
    if phase == 2:
        data = _read_json(experiment_dir / "2_issue_discovery" / "issues_user_story.json")
        if isinstance(data, list):
            return {"Issues found (live)": len(data)}
    if phase == 3:
        with_pr = _read_json(experiment_dir / "3_pr_linking" / "issues_with_pr.json")
        without_pr = _read_json(experiment_dir / "3_pr_linking" / "issues_without_pr.json")
        out = {}
        if isinstance(with_pr, list):
            out["With PR (live)"] = len(with_pr)
        if isinstance(without_pr, list):
            out["Without PR (live)"] = len(without_pr)
        return out
    if phase == 4:
        validate = _read_json(experiment_dir / "4_ai_validation" / "validated_issues.json")
        discarded = _read_json(experiment_dir / "4_ai_validation" / "discarded_issues_ai.json")
        out = {}
        if isinstance(validate, list):
            out["Validated (live)"] = len(validate)
        if isinstance(discarded, list):
            out["Rejected (live)"] = len(discarded)
        return out
    if phase == 5:
        ck = _read_json(experiment_dir / "5_dataset" / "_checkpoint.json")
        if isinstance(ck, dict):
            processed = ck.get("processed", [])
            return {"Processed (live)": len(processed) if isinstance(processed, list) else 0}
    return {}


PHASE_FILES = {
    1: ("1_discovery", "run_stats.json"),
    2: ("2_issue_discovery", "run_stats.json"),
    3: ("3_pr_linking", "run_stats.json"),
    4: ("4_ai_validation", "run_stats.json"),
    5: ("5_dataset", "_run_stats.json"),
}


def read_live_state(
    experiment_dir: Path,
    *,
    log_lines: int = 60,
) -> LiveState:
    """Build a LiveState by reading the pipeline's state files.

    Designed to be polled from the Streamlit page.
    """
    exp_id = experiment_dir.name
    state = LiveState(experiment_id=exp_id, is_running=False)

    runtime = load_runtime(experiment_dir)
    if runtime:
        state.pid = runtime.pid
        state.is_running = is_pid_alive(runtime.pid)
        try:
            t0 = datetime.fromisoformat(runtime.started_at)
            state.elapsed_sec = (datetime.now() - t0).total_seconds()
        except ValueError:
            pass

    state.pipeline_done = (experiment_dir / "_done.txt").exists()

    ps = _read_json(experiment_dir / "pipeline_state.json")
    if ps:
        for n in range(1, 6):
            state.phases_done[n] = bool(ps.get(f"phase_{n}_done"))

    # Per-phase KPIs: use run_stats.json when the phase has finished,
    # otherwise read live counts from the incremental data files.
    for n, (subdir, filename) in PHASE_FILES.items():
        stats = _read_json(experiment_dir / subdir / filename)
        if stats:
            state.phase_metrics[n] = _phase_metrics(stats, n)
        else:
            state.phase_metrics[n] = _phase_metrics_live(experiment_dir, n)

    # Log tail: pick the most recent of _pipeline.log, the current
    # phase_N.log and _bootstrap.log, so a stale orchestrator log does not
    # hide the output of the running phase.
    candidates = [
        experiment_dir / "_pipeline.log",
        *[experiment_dir / "_logs" / f"phase_{n}.log" for n in range(1, 6)],
        experiment_dir / "_bootstrap.log",
    ]
    existing = [p for p in candidates if p.exists()]
    log_source = max(existing, key=lambda p: p.stat().st_mtime) if existing else None
    bootstrap_path = experiment_dir / "_bootstrap.log"

    if log_source is not None:
        try:
            with log_source.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            state.log_tail = lines[-log_lines:]
            # Header naming which log is shown.
            header = f"📄 {log_source.relative_to(experiment_dir)}\n" + "─" * 60 + "\n"
            state.log_tail = [header] + state.log_tail
            if log_source == bootstrap_path:
                state.log_tail = ["⚠️  Pipeline has not opened _pipeline.log yet — "
                                  "showing _bootstrap.log (direct subprocess output):\n",
                                  "─" * 60 + "\n"] + state.log_tail
        except OSError:
            pass

    return state


# Active runs, for the "Active runs" tab.

def list_active_runs(experiments_root: Path) -> list[dict]:
    """Scan experiments_root and return experiments that have a
    _runtime.json + a still-alive process, or have no _done.txt."""
    out = []
    if not experiments_root.exists():
        return out
    for d in sorted(experiments_root.iterdir()):
        if not d.is_dir():
            continue
        runtime = load_runtime(d)
        if not runtime:
            continue
        alive = is_pid_alive(runtime.pid)
        done = (d / "_done.txt").exists()
        # Active means running, or started but never completed.
        if alive or not done:
            out.append({
                "experiment_id": d.name,
                "path": str(d),
                "pid": runtime.pid,
                "started_at": runtime.started_at,
                "is_alive": alive,
                "is_done": done,
            })
    return out