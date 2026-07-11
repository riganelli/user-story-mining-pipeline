"""Orchestrator of the user-story extraction pipeline.

Runs the five phases in sequence: each one starts only if the previous
finished with exit code 0. The two marker files read by the dashboard
(``pipeline_state.json`` and ``_done.txt``) are written atomically so an
interrupted write cannot corrupt them.

Usage:
    python run_pipeline.py            # all phases (1 → 5)
    python run_pipeline.py --from 3   # resume from phase 3 onward
    python run_pipeline.py --only 4   # run only phase 4

The output of each phase is written to stdout and to
``experiments/{EXPERIMENT_ID}/_logs/phase_N.log`` (per-phase log) and
``experiments/{EXPERIMENT_ID}/_pipeline.log`` (unified log).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Make the pipeline package importable both when this script is run
# directly and when it is launched from the dashboard.
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.experiment_paths import paths

from utils.atomic_io import dump_json_atomic


# The subprocesses run with PIPELINE_ROOT as their working directory, so
# the phase scripts can import config/, utils/ and the other packages.
ORCHESTRATION_DIR = Path(__file__).resolve().parent
PIPELINE_ROOT = ORCHESTRATION_DIR.parent
STEPS_DIR = PIPELINE_ROOT / "steps"

FASI = [
    (1, "1_discovery.py"),
    (2, "2_issue_discovery.py"),
    (3, "3_pr_linking.py"),
    (4, "4_us_validation.py"),
    (5, "5_build_dataset.py"),
]

# Per-experiment log locations.
LOG_DIR      = paths.base / "_logs"
PIPELINE_LOG = paths.base / "_pipeline.log"

# Marker files read by the dashboard: pipeline_state.json holds the
# per-phase completion flags, _done.txt marks a fully completed pipeline.
# Only the orchestrator writes them.
PIPELINE_STATE_FILE = paths.base / "pipeline_state.json"
DONE_MARKER         = paths.base / "_done.txt"


# A single lock protects both stdout and _pipeline.log, so lines from
# the output-draining threads never interleave.
_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    """Log one orchestrator line to stdout and to the unified log."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    with _LOG_LOCK:
        print(line, flush=True)
        try:
            PIPELINE_LOG.parent.mkdir(parents=True, exist_ok=True)
            with PIPELINE_LOG.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass  # Logging must never block the pipeline.


def _stream_output(stream, phase_log_path: Path, prefix: str) -> None:
    """Drain a subprocess's stdout line by line.

    Each line is written to the orchestrator's stdout (with a phase
    prefix), to the per-phase log (without prefix) and to the unified
    log (with timestamp and prefix). The draining runs in a thread so
    the OS pipe cannot fill up and block the phase.
    """
    try:
        phase_log_path.parent.mkdir(parents=True, exist_ok=True)
        with phase_log_path.open("a", encoding="utf-8") as phase_log, \
             PIPELINE_LOG.open("a", encoding="utf-8") as pipe_log:
            for line in stream:
                with _LOG_LOCK:
                    sys.stdout.write(f"{prefix} {line}")
                    sys.stdout.flush()
                    phase_log.write(line)
                    phase_log.flush()
                    pipe_log.write(f"[{datetime.now().strftime('%H:%M:%S')}] {prefix} {line}")
                    pipe_log.flush()
    except Exception as e:
        log(f"[stream_output] error: {e}")


def _read_pipeline_state() -> dict:
    """Read ``pipeline_state.json``; a missing or corrupt file yields an
    empty state with all phases marked as not completed."""
    base = {
        "phase_1_done": False,
        "phase_2_done": False,
        "phase_3_done": False,
        "phase_4_done": False,
        "phase_5_done": False,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "errors": [],
    }
    if not PIPELINE_STATE_FILE.exists():
        return base
    try:
        with PIPELINE_STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in base.items():
            data.setdefault(k, v)
        return data
    except (json.JSONDecodeError, OSError):
        return base


def _write_pipeline_state(state: dict) -> None:
    """Write ``pipeline_state.json`` atomically.

    An interrupted plain write would truncate the file and the next read
    would fall back to "all phases not done", losing the completion
    flags of the phases already finished.
    """
    try:
        dump_json_atomic(state, PIPELINE_STATE_FILE)
    except OSError as e:
        log(f"⚠️  Could not write pipeline_state.json: {e}")


def _mark_phase_done(number: int) -> None:
    """Mark phase ``number`` as completed in ``pipeline_state.json``."""
    state = _read_pipeline_state()
    state[f"phase_{number}_done"] = True
    state[f"phase_{number}_done_at"] = datetime.now().isoformat(timespec="seconds")
    _write_pipeline_state(state)


def _log_phase_error(number: int, exit_code: int) -> None:
    """Append a phase error to the list in ``pipeline_state.json``."""
    state = _read_pipeline_state()
    state["errors"].append({
        "phase": number,
        "message": f"exit code {exit_code}",
        "at": datetime.now().isoformat(timespec="seconds"),
    })
    _write_pipeline_state(state)


def _write_done_marker(total_duration: float, completed: list[int],
                      failed: list[int]) -> None:
    """Write ``_done.txt``, the pipeline-completed marker.

    Read by the dashboard and by ``queue_runner`` to detect a finished
    queued run.
    """
    DONE_MARKER.parent.mkdir(parents=True, exist_ok=True)
    # Written atomically so a kill mid-write cannot leave a half-written
    # marker that readers would take as "pipeline complete".
    lines = [
        f"completed_at: {datetime.now().isoformat(timespec='seconds')}",
        f"elapsed_sec: {total_duration:.0f}",
        f"phases_completed: {completed}",
    ]
    if failed:
        lines.append(f"phases_failed: {failed}")
    content = "\n".join(lines) + "\n"
    tmp = DONE_MARKER.parent / (DONE_MARKER.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, DONE_MARKER)
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        log(f"⚠️  Could not write _done.txt: {e}")


# Map phase number → its run_stats.json. Each phase records its own
# cumulative total_time_seconds across resumed sessions, which is more
# accurate than the orchestrator's per-session subprocess clock.
_PHASE_STATS_FILE = {
    1: paths.step1_stats_json,
    2: paths.step2_stats_json,
    3: paths.step3_stats_json,
    4: paths.step4_stats_json,
    5: paths.step5_stats_json,
}


def _phase_cumulative_seconds(number: int, fallback: float) -> float:
    """Return the phase's cumulative ``total_time_seconds`` from its
    run-stats file, or ``fallback`` if the value is unavailable."""
    stats_path = _PHASE_STATS_FILE.get(number)
    if not stats_path or not Path(stats_path).exists():
        return fallback
    try:
        with open(stats_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return fallback
    val = data.get("total_time_seconds")
    return float(val) if isinstance(val, (int, float)) else fallback


def run_phase(number: int, script: str) -> tuple[bool, float]:
    """Launch a phase script as a subprocess.

    Returns ``(success, duration_seconds)``; the output is streamed live
    to stdout and to the log files.
    """
    script_path = STEPS_DIR / script
    if not script_path.exists():
        log(f"❌ Script not found: {script_path}")
        return False, 0.0

    log("=" * 70)
    log(f"▶️  PHASE {number} — starting {script}")
    log("=" * 70)

    phase_log_path = LOG_DIR / f"phase_{number}.log"
    prefix = f"[F{number}]"

    # On Windows, CREATE_NO_WINDOW prevents a visible console per phase
    # and the STATUS_CONTROL_C_EXIT crash that can hit subprocesses doing
    # HTTP when the parent has an ambiguous console handler.
    popen_kwargs: dict = {}
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

    start = time.time()

    # -u keeps the phase output unbuffered for real-time streaming.
    cmd = [sys.executable, "-u", str(script_path)]

    proc = subprocess.Popen(
        cmd,
        cwd=PIPELINE_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
        **popen_kwargs,
    )

    t = threading.Thread(
        target=_stream_output,
        args=(proc.stdout, phase_log_path, prefix),
        daemon=True,
    )
    t.start()

    proc.wait()
    t.join(timeout=5)

    duration = time.time() - start
    rc = proc.returncode

    if rc == 0:
        # The phase's own cumulative time also covers previous resumed
        # sessions; the subprocess clock only measures this one.
        cumulative = _phase_cumulative_seconds(number, duration)
        log(f"✅ Phase {number} completed in {cumulative:.1f}s "
            f"(this session: {duration:.1f}s)")
        _mark_phase_done(number)
        return True, cumulative
    else:
        log(f"❌ Phase {number} FAILED (exit code {rc}) after {duration:.1f}s")
        log(f"   See detailed log: {phase_log_path}")
        _log_phase_error(number, rc)
        return False, duration


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Runs the user-story extraction pipeline in sequence."
    )
    parser.add_argument(
        "--from", dest="da_fase", type=int, default=1, choices=[1, 2, 3, 4, 5],
        help="Resumes from phase N onward (default: 1).",
    )
    parser.add_argument(
        "--only", dest="solo_fase", type=int, default=None, choices=[1, 2, 3, 4, 5],
        help="Runs only phase N (overrides --from).",
    )
    # Legacy flags kept for compatibility with the dashboard launcher;
    # --no-parallel is a no-op in a sequential pipeline.
    for n in range(1, 6):
        parser.add_argument(
            f"--skip-{n}", action="store_true",
            help=f"[legacy] Skip phase {n}.",
        )
    parser.add_argument(
        "--no-parallel", action="store_true",
        help="[legacy, no-op] The pipeline is always sequential.",
    )
    args = parser.parse_args()

    if args.solo_fase is not None:
        phases_to_run = [(n, s) for n, s in FASI if n == args.solo_fase]
    else:
        skip_set = {n for n in range(1, 6) if getattr(args, f"skip_{n}", False)}
        phases_to_run = [(n, s) for n, s in FASI
                            if n >= args.da_fase and n not in skip_set]

    log("=" * 70)
    log("DISCOVERY PIPELINE — sequential orchestrator")
    log(f"📁 Experiment: {paths.experiment_id}")
    log(f"📋 Phases to run: {[n for n, _ in phases_to_run]}")
    log(f"📂 Log dir: {LOG_DIR}")
    log("=" * 70)

    # Update started_at without resetting the completion flags of phases
    # finished by previous runs (relevant with --from N or --only N).
    state = _read_pipeline_state()
    state["started_at"] = datetime.now().isoformat(timespec="seconds")
    # Remove a stale _done.txt from a previous run: this run might not
    # complete everything.
    if DONE_MARKER.exists():
        try:
            DONE_MARKER.unlink()
        except OSError:
            pass
    _write_pipeline_state(state)

    results: list[tuple[int, str, bool, float]] = []
    total_start = time.time()

    for number, script in phases_to_run:
        ok, duration = run_phase(number, script)
        results.append((number, script, ok, duration))
        if not ok:
            break  # Stop on error: the following phases are not launched.

    session_wall = time.time() - total_start

    # pipeline_state.json reflects all phases ever completed, not just
    # the ones run in this session.
    final_state = _read_pipeline_state()
    globally_completed = [n for n in range(1, 6)
                          if final_state.get(f"phase_{n}_done")]
    failed_in_run = [n for n, _, ok, _ in results if not ok]

    # End-to-end compute time: the sum of each phase's cumulative time,
    # which also covers sessions before a resume.
    pipeline_total_seconds = sum(
        _phase_cumulative_seconds(n, 0.0) for n in globally_completed
    )

    # _done.txt is written only when all five phases are globally done
    # and nothing failed in this run.
    pipeline_completa_globalmente = (len(globally_completed) == 5
                                     and not failed_in_run)
    if pipeline_completa_globalmente:
        _write_done_marker(pipeline_total_seconds, globally_completed, failed_in_run)

    log("")
    log("=" * 70)
    log("PIPELINE SUMMARY")
    log("=" * 70)
    for number, script, ok, duration in results:
        status = "✅" if ok else "❌"
        log(f"  [{number}/5] {script:30s} {status} {duration:7.1f}s")
    log(f"  Pipeline total (all sessions): {pipeline_total_seconds:.1f}s "
        f"({pipeline_total_seconds/60:.1f} min)")
    log(f"  This session (wall):           {session_wall:.1f}s "
        f"({session_wall/60:.1f} min)")

    if results and all(ok for _, _, ok, _ in results):
        log("🎉 Pipeline completed successfully.")
        return 0
    else:
        last_failed = next(
            (n for n, _, ok, _ in results if not ok), None
        )
        log(f"💥 Pipeline interrupted at phase {last_failed}.")
        log(f"   To resume after the fix: "
            f"python run_pipeline.py --from {last_failed}")
        return 1


if __name__ == "__main__":
    sys.exit(main())