"""Worker subprocess that runs the queued experiments in series.

Launched by ``queue_runner.start_worker()`` as a detached subprocess.
Reads ``experiments/_queue.json``, takes the next pending item, launches
``run_pipeline.py`` with the corresponding ``EXPERIMENT_ID``, waits for
completion and moves on. A failed experiment is marked as such and does
not stop the queue; items without a ``config.json`` are skipped.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# queue_runner lives in the dashboard package, outside the pipeline
# folder the worker is launched from, so it is located dynamically.
def _find_queue_runner() -> Path | None:
    """Locate queue_runner.py walking up from the cwd and into subfolders."""
    here = Path.cwd().resolve()
    for parent in [here, *here.parents]:
        for candidate in parent.rglob("queue_runner.py"):
            return candidate.parent
    return None


_qr_dir = _find_queue_runner()
if _qr_dir:
    sys.path.insert(0, str(_qr_dir))

try:
    from queue_runner import (  # type: ignore
        load_queue, update_item_status, detect_active_external_run,
    )
except ImportError as e:
    print(f"❌ Could not import queue_runner: {e}", flush=True)
    print(f"   sys.path: {sys.path}", flush=True)
    sys.exit(1)


# The worker is launched from the pipeline/ folder. The experiments
# root defaults to <pipeline_parent>/experiments/ and can be overridden
# with the EXPERIMENTS_ROOT environment variable.
PIPELINE_CWD = Path.cwd()
RUN_PIPELINE_SCRIPT = PIPELINE_CWD / "orchestration" / "run_pipeline.py"

_default_exp_root = (PIPELINE_CWD.parent / "experiments").resolve()
EXPERIMENTS_ROOT = Path(os.environ.get("EXPERIMENTS_ROOT", str(_default_exp_root))).resolve()

POLL_INTERVAL_SEC = 3        # queue re-read interval when empty
IDLE_EXIT_AFTER_SEC = 60     # exit after the queue stays empty this long
EXTERNAL_RUN_POLL_SEC = 10   # re-check interval for external runs


def log(msg: str) -> None:
    """Print with a timestamp (stdout is redirected to the log file)."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_next_pending() -> str | None:
    """Return the experiment_id of the next pending item in the queue, or None if empty."""
    state = load_queue(EXPERIMENTS_ROOT)
    for item in state.items:
        if item.status == "pending":
            return item.experiment_id
    return None


def run_experiment(exp_id: str) -> tuple[bool, str | None]:
    """Launch ``run_pipeline.py`` for the experiment and wait for it.

    Writes ``_runtime.json`` with the subprocess PID before launching and
    deletes it afterwards, so the UI shows the run exactly like a run
    launched manually. Returns ``(success, error_message)``.
    """

    exp_dir = EXPERIMENTS_ROOT / exp_id
    if not exp_dir.exists():
        return False, f"Experiment folder not found: {exp_dir}"
    if not (exp_dir / "config.json").exists():
        return False, f"config.json not found in {exp_dir}"
    if not RUN_PIPELINE_SCRIPT.exists():
        return False, f"run_pipeline.py not found in {PIPELINE_CWD}"

    env = os.environ.copy()
    env["EXPERIMENT_ID"] = exp_id
    env["PYTHONIOENCODING"] = "utf-8"

    cmd = [sys.executable, "-u", str(RUN_PIPELINE_SCRIPT)]

    log(f"  ▶️  Launching: {' '.join(cmd)} (EXPERIMENT_ID={exp_id})")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PIPELINE_CWD),
            env=env,
            # stdout/stderr are inherited from the worker log.
        )
    except Exception as e:
        return False, f"Subprocess startup error: {type(e).__name__}: {e}"

    # Same _runtime.json format as the manual launcher; experiment_id is
    # a required field of RuntimeInfo.
    runtime_data = {
        "experiment_id": exp_id,
        "pid": proc.pid,
        "started_at": datetime.now().isoformat(),
        "cwd": str(PIPELINE_CWD),
        "cmd": cmd,
    }
    runtime_file = exp_dir / "_runtime.json"
    try:
        with runtime_file.open("w", encoding="utf-8") as f:
            json.dump(runtime_data, f, indent=2)
    except OSError as e:
        log(f"  ⚠️  Could not write _runtime.json: {e} (continuing anyway)")

    try:
        returncode = proc.wait()
    except KeyboardInterrupt:
        # The worker is being terminated: stop the subprocess as well.
        log(f"  ⚠️  Worker interrupted, terminating subprocess PID {proc.pid}")
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        try:
            runtime_file.unlink(missing_ok=True)
        except Exception:
            pass
        return False, "Interrupted"

    # The PID is no longer valid: remove _runtime.json so the UI does not
    # report a stale run.
    try:
        runtime_file.unlink(missing_ok=True)
    except Exception:
        pass

    if returncode == 0:
        # The pipeline writes _done.txt on successful completion.
        if (exp_dir / "_done.txt").exists():
            return True, None
        else:
            return False, "Pipeline finished with exit 0 but _done.txt does not exist"
    else:
        return False, f"Pipeline finished with exit code {returncode}"


def main() -> int:
    log("=" * 60)
    log(f"QUEUE WORKER started")
    log(f"  EXPERIMENTS_ROOT: {EXPERIMENTS_ROOT}")
    log(f"  PIPELINE_CWD:     {PIPELINE_CWD}")
    log("=" * 60)

    if not EXPERIMENTS_ROOT.exists():
        log(f"❌ EXPERIMENTS_ROOT does not exist: {EXPERIMENTS_ROOT}")
        return 1

    if not RUN_PIPELINE_SCRIPT.exists():
        log(f"❌ run_pipeline.py not found: {RUN_PIPELINE_SCRIPT}")
        return 1

    idle_since: float | None = None
    n_processed = 0
    n_failed = 0
    waiting_for_external = False  # logs the waiting message only once

    while True:
        # Wait while an external pipeline run (launched manually from
        # the UI) is in progress.
        external = detect_active_external_run(EXPERIMENTS_ROOT)
        if external:
            if not waiting_for_external:
                log("")
                log(f"⏸️  External run active: {external['experiment_id']} "
                    f"(PID {external['pid']}). Worker waiting...")
                waiting_for_external = True
            time.sleep(EXTERNAL_RUN_POLL_SEC)
            continue
        elif waiting_for_external:
            log(f"▶️  No external run active, resuming queue")
            waiting_for_external = False

        exp_id = get_next_pending()

        if exp_id is None:
            if idle_since is None:
                idle_since = time.time()
                log(f"💤 Empty queue, waiting for new items (timeout {IDLE_EXIT_AFTER_SEC}s)")
            elif time.time() - idle_since > IDLE_EXIT_AFTER_SEC:
                log(f"🏁 Queue empty for >{IDLE_EXIT_AFTER_SEC}s, worker terminating")
                break
            time.sleep(POLL_INTERVAL_SEC)
            continue

        idle_since = None

        log("")
        log(f"📦 Next: {exp_id}")
        update_item_status(EXPERIMENTS_ROOT, exp_id, "running")

        t0 = time.time()
        success, err = run_experiment(exp_id)
        elapsed = time.time() - t0

        if success:
            log(f"  ✅ {exp_id} completed in {elapsed/60:.1f} min")
            update_item_status(EXPERIMENTS_ROOT, exp_id, "completed")
            n_processed += 1
        else:
            log(f"  ❌ {exp_id} failed after {elapsed/60:.1f} min: {err}")
            update_item_status(EXPERIMENTS_ROOT, exp_id, "failed", error=err)
            n_failed += 1

    log("")
    log("=" * 60)
    log(f"WORKER terminated — {n_processed} completed, {n_failed} failed")
    log("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
