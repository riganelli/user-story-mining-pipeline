"""Configuration management and canonical experiment paths.

Modules:
  experiment_paths: canonical paths of an experiment.
  config_builder: validated construction of ``config.json`` (CLI and UI).

The non-Python file ``current_experiment.txt`` holds the ID of the
current experiment and is read by ``experiment_paths`` as a fallback
when the ``EXPERIMENT_ID`` environment variable is not set.
"""
