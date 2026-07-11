"""Atomic JSON writes for crash-resilient checkpoints.

The pipeline runs under SLURM, where a job can be killed at any moment
(e.g. walltime expiry). A kill during a plain ``json.dump`` leaves a
truncated file that breaks the next run. Writing to a temporary file in
the same directory and then ``os.replace()``-ing it onto the final path
guarantees the destination is always either the previous complete
version or the new complete version.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def dump_json_atomic(data: Any, path: str | os.PathLike) -> None:
    """Write ``data`` to ``path`` as JSON, atomically.

    The temporary file is created next to the target because
    cross-filesystem renames are not guaranteed to be atomic.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / (p.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        # Flush to physical disk before the rename, so the data survives
        # a node crash or power loss, not just a process kill.
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(tmp, p)
    except PermissionError:
        # Windows: the destination may be locked by another reader.
        # Fall back to a direct (non-atomic) overwrite.
        try:
            tmp.unlink()
        except OSError:
            pass
        with p.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        return
    # Sync the parent directory so the rename itself is durable.
    # Best effort: not supported on all platforms/filesystems.
    try:
        dir_fd = os.open(str(p.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, AttributeError):
        pass
