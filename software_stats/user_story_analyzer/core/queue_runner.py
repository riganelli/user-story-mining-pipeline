"""Experiment queue management.

Persists a queue of experiments to run under experiments/_queue.json and
provides the public functions used by the UI (enqueue, reorder, cancel)
plus the worker subprocess that runs the queued experiments in series.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path

# Files kept inside the experiments/ folder.
QUEUE_FILE = "_queue.json"
WORKER_RUNTIME_FILE = "_queue_worker.json"
WORKER_LOG_FILE = "_queue_worker.log"


# Queue model.

@dataclass
class QueueItem:
    """A queued experiment. Holds only the ID — the config is already in
    experiments/{ID}/config.json, written by the UI."""
    experiment_id: str
    added_at: str
    status: str = "pending"   # pending | running | completed | failed | skipped
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


@dataclass
class QueueState:
    """Full queue state."""
    items: list[QueueItem] = field(default_factory=list)
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "items": [asdict(i) for i in self.items],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QueueState":
        items = [QueueItem(**i) for i in d.get("items", [])]
        return cls(items=items, updated_at=d.get("updated_at", ""))


@dataclass
class WorkerInfo:
    """Info about the worker subprocess that loops the queue."""
    pid: int
    started_at: str
    cwd: str


# Atomic I/O, to avoid races between the UI and the worker.

def _atomic_write_json(path: Path, data: dict) -> None:
    """Write a JSON file atomically: write to a temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# Public queue functions.

def load_queue(experiments_root: Path) -> QueueState:
    """Read the queue. If it does not exist, return an empty queue."""
    data = _read_json(experiments_root / QUEUE_FILE)
    if not data:
        return QueueState()
    return QueueState.from_dict(data)


def save_queue(experiments_root: Path, state: QueueState) -> None:
    state.updated_at = datetime.now().isoformat()
    _atomic_write_json(experiments_root / QUEUE_FILE, state.to_dict())


def add_to_queue(experiments_root: Path, experiment_id: str) -> tuple[bool, str]:
    """Add an experiment to the end of the queue.
    Returns (success, message). Fails if already queued with status pending/running.
    """
    state = load_queue(experiments_root)
    for item in state.items:
        if item.experiment_id == experiment_id and item.status in ("pending", "running"):
            return False, f"'{experiment_id}' is already queued (status: {item.status})"
    state.items.append(QueueItem(
        experiment_id=experiment_id,
        added_at=datetime.now().isoformat(),
    ))
    save_queue(experiments_root, state)
    return True, f"'{experiment_id}' added to the queue"


def remove_from_queue(experiments_root: Path, index: int) -> tuple[bool, str]:
    """Remove the item at the given index. Only for pending items."""
    state = load_queue(experiments_root)
    if index < 0 or index >= len(state.items):
        return False, "Index out of range"
    item = state.items[index]
    if item.status != "pending":
        return False, f"Cannot remove an item with status '{item.status}'"
    removed = state.items.pop(index)
    save_queue(experiments_root, state)
    return True, f"'{removed.experiment_id}' removed from the queue"


def move_in_queue(experiments_root: Path, index: int, direction: int) -> tuple[bool, str]:
    """Move the item at index by +1 (down) or -1 (up). Only between pending items."""
    state = load_queue(experiments_root)
    if direction not in (-1, 1):
        return False, "direction must be -1 or +1"
    new_idx = index + direction
    if index < 0 or index >= len(state.items) or new_idx < 0 or new_idx >= len(state.items):
        return False, "Move out of range"
    if state.items[index].status != "pending" or state.items[new_idx].status != "pending":
        return False, "Only pending items can be reordered"
    state.items[index], state.items[new_idx] = state.items[new_idx], state.items[index]
    save_queue(experiments_root, state)
    return True, "Reordered"


def update_item_status(
    experiments_root: Path,
    experiment_id: str,
    status: str,
    error: str | None = None,
) -> None:
    """Update the status of an item in the queue. Called by the worker."""
    state = load_queue(experiments_root)
    now = datetime.now().isoformat()
    for item in state.items:
        if item.experiment_id == experiment_id and item.status in ("pending", "running"):
            item.status = status
            if status == "running":
                item.started_at = now
            if status in ("completed", "failed", "skipped"):
                item.finished_at = now
                if error:
                    item.error = error
            break
    save_queue(experiments_root, state)


def clear_completed(experiments_root: Path) -> int:
    """Remove all completed/failed/skipped items from the queue.
    Returns the number of items removed."""
    state = load_queue(experiments_root)
    before = len(state.items)
    state.items = [i for i in state.items if i.status in ("pending", "running")]
    save_queue(experiments_root, state)
    return before - len(state.items)


def stop_running_item(experiments_root: Path, experiment_id: str) -> tuple[bool, str]:
    """Terminate the in-progress run of a queue item with status 'running'.

    Kills the pipeline process (reads the PID from _runtime.json) and marks
    the item as 'failed' with error="Killed by user". The worker (which is in
    proc.wait()) unblocks, records the failure, and moves on to the next
    item in the queue.

    Does NOT terminate the worker: the queue keeps flowing normally.
    """
    exp_dir = experiments_root / experiment_id
    runtime_file = exp_dir / "_runtime.json"
    if not runtime_file.exists():
        return False, f"_runtime.json not found for {experiment_id}"

    try:
        with runtime_file.open("r", encoding="utf-8") as f:
            runtime_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return False, f"Error reading _runtime.json: {e}"

    pid = runtime_data.get("pid", 0)
    if not pid:
        return False, "PID not found in _runtime.json"

    if not is_pid_alive(pid):
        return False, f"Process (PID {pid}) is no longer active"

    # On Windows, taskkill /T also kills the child phase processes.
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        else:
            os.kill(pid, 15)  # SIGTERM
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Kill error: {e}"

    # The status is not updated here; the worker sets it to 'failed' when
    # run_experiment returns, which avoids a race with the UI.
    return True, f"Process {pid} terminated. The worker will move on to the next item."


# Worker subprocess that loops over the queue.

def is_pid_alive(pid: int) -> bool:
    """Check whether a process is still alive. Identical to the main launcher."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            out = subprocess.check_output(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                stderr=subprocess.DEVNULL, timeout=5,
            )
            return str(pid) in out.decode("utf-8", errors="ignore")
        else:
            os.kill(pid, 0)
            return True
    except (OSError, subprocess.SubprocessError):
        return False


def load_worker_info(experiments_root: Path) -> WorkerInfo | None:
    data = _read_json(experiments_root / WORKER_RUNTIME_FILE)
    if not data:
        return None
    try:
        return WorkerInfo(**data)
    except TypeError:
        return None


def save_worker_info(experiments_root: Path, info: WorkerInfo) -> None:
    _atomic_write_json(experiments_root / WORKER_RUNTIME_FILE, asdict(info))


def is_worker_running(experiments_root: Path) -> bool:
    info = load_worker_info(experiments_root)
    if not info:
        return False
    return is_pid_alive(info.pid)


def detect_active_external_run(experiments_root: Path) -> dict | None:
    """Detect whether there is an ACTIVE pipeline run that was NOT started by the worker.

    A run is "active" if:
      - experiments/{ID}/_runtime.json exists with a PID
      - that PID is alive
      - _done.txt does NOT exist in the folder

    The worker uses this function to avoid starting in parallel with a run
    already launched manually by the user. It waits for it to finish before
    launching the next item in the queue.

    Returns a dict with info about the active run, or None if no run is active.
    """
    if not experiments_root.exists():
        return None
    for d in sorted(experiments_root.iterdir()):
        if not d.is_dir():
            continue
        runtime_path = d / "_runtime.json"
        if not runtime_path.exists():
            continue
        try:
            with runtime_path.open("r", encoding="utf-8") as f:
                runtime_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        pid = runtime_data.get("pid", 0)
        if not pid or not is_pid_alive(pid):
            continue

        # PID alive but _done.txt present: the run finished, so it is not
        # considered active.
        if (d / "_done.txt").exists():
            continue

        return {
            "experiment_id": d.name,
            "pid": pid,
            "started_at": runtime_data.get("started_at", ""),
            "path": str(d),
        }
    return None


def start_worker(
    experiments_root: Path,
    pipeline_cwd: Path,
) -> tuple[bool, str, WorkerInfo | None]:
    """Start the worker that loops the queue. Only ONE can run at a time."""
    if is_worker_running(experiments_root):
        info = load_worker_info(experiments_root)
        return False, f"A worker is already active (PID {info.pid})", info

    # queue_worker.py lives in pipeline/orchestration/.
    queue_worker_script = pipeline_cwd / "orchestration" / "queue_worker.py"
    if not queue_worker_script.exists():
        return False, f"queue_worker.py not found in {pipeline_cwd}/orchestration/", None

    # Open the log in append mode so subsequent runs are appended.
    log_path = experiments_root / WORKER_LOG_FILE
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("a", encoding="utf-8", buffering=1)
    log_file.write(f"\n{'='*60}\n")
    log_file.write(f"Worker started at {datetime.now().isoformat()}\n")
    log_file.write(f"CWD: {pipeline_cwd}\n")
    log_file.write(f"{'='*60}\n")
    log_file.flush()

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["EXPERIMENTS_ROOT"] = str(experiments_root)

    cmd = [sys.executable, "-u", str(queue_worker_script)]

    if sys.platform == "win32":
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        kwargs = {"start_new_session": True}

    proc = subprocess.Popen(
        cmd,
        cwd=str(pipeline_cwd),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        **kwargs,
    )

    info = WorkerInfo(
        pid=proc.pid,
        started_at=datetime.now().isoformat(),
        cwd=str(pipeline_cwd),
    )
    save_worker_info(experiments_root, info)
    return True, f"Worker started (PID {proc.pid})", info


def stop_worker(experiments_root: Path) -> tuple[bool, str]:
    """Terminate the worker. Does NOT terminate the in-progress experiment (to
    avoid losing progress). After finishing the current experiment, the worker
    simply does not start another one."""
    info = load_worker_info(experiments_root)
    if not info:
        return False, "No active worker"
    if not is_pid_alive(info.pid):
        return False, "The worker is not running"
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(info.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
            )
        else:
            os.kill(info.pid, 15)  # SIGTERM
        return True, f"Worker terminated (PID {info.pid})"
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"Worker termination error: {e}"