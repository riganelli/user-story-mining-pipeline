"""The five executable phase scripts of the pipeline.

These scripts are meant to be run as standalone processes (via
``run_pipeline.py`` or ``python steps/N_xxx.py``), not imported: their
filenames start with a digit and are therefore not valid module names.
Each script adds the ``pipeline/`` folder to ``sys.path`` so it can
import ``config/``, ``utils/`` and the other packages.
"""
