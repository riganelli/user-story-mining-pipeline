"""Helper modules shared across pipeline phases.

Modules:
  discovery_strategies: Connextra regex patterns (phases 1 and 2).
  github_pacing: GitHub client with adaptive rate limiting.
  logging_utils: thread-safe printing (``tprint``, ``tlog``).
  stats_utils: statistical functions used in run statistics.
  checkpoint: resume helpers for checkpoint files (phases 3 and 4).
"""
