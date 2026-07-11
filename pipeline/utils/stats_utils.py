"""Statistical helpers shared by the phases that produce run statistics."""
from __future__ import annotations


def median(lst):
    """Compute the median of a numeric list.

    Returns 0 for an empty list. The input is sorted internally; for an
    even number of elements the average of the two central values is
    returned (possibly a float).
    """
    if not lst:
        return 0
    lst_sorted = sorted(lst)
    n = len(lst_sorted)
    return lst_sorted[n // 2] if n % 2 == 1 else (lst_sorted[n // 2 - 1] + lst_sorted[n // 2]) / 2
