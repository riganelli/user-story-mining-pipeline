"""Pipeline for extracting user stories from GitHub issues.

Packages:
  steps: the five pipeline phases (discovery to dataset build).
  orchestration: entry points to create and run experiments.
  llm_validation: LLM-based validation helpers (phase 4).
  dataset_builder: GitHub data retrieval helpers (phase 5).
  config: configuration and canonical experiment paths.
  utils: shared helpers (pacing, logging, checkpoints, regexes).
"""
