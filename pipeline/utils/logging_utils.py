"""Thread-safe logging helpers.

A single module-level lock serializes access to stdout so that lines
printed by concurrent threads never interleave. Every module importing
from here shares the same lock.
"""
from __future__ import annotations

from threading import Lock

_PRINT_LOCK = Lock()


def tprint(*args, **kwargs) -> None:
    """Thread-safe print with the same signature as ``print()``."""
    with _PRINT_LOCK:
        print(*args, **kwargs)


def tlog(msg: str) -> None:
    """Thread-safe print of a single string (alias of ``tprint``)."""
    with _PRINT_LOCK:
        print(msg)
