# 00 — Documentation Index

> Navigable index of all the documentation files. Each entry has a
> one-line description and a suggested reading order.

## Files in this folder

| # | File | What's inside |
|---|------|---------------|
| 01 | [`01_architecture.md`](01_architecture.md) | System-wide overview, the 5-phase pipeline, the dashboard, glossary |
| 02 | [`02_setup_installation.md`](02_setup_installation.md) | How to install, configure tokens, set up Ollama, run a first experiment |
| 03 | [`03_phase1_discovery.md`](03_phase1_discovery.md) | Phase 1 — find candidate repositories on GitHub (5 strategies) |
| 04 | [`04_phase2_issue_extraction.md`](04_phase2_issue_extraction.md) | Phase 2 — fetch closed user-story-like issues from each repo |
| 05 | [`05_phase3_pr_linking.md`](05_phase3_pr_linking.md) | Phase 3 — link each issue to the Pull Request that closed it |
| 06 | [`06_phase4_llm_validation.md`](06_phase4_llm_validation.md) | Phase 4 — local LLM validates if each issue is really a user story |
| 07 | [`07_phase5_build_dataset.md`](07_phase5_build_dataset.md) | Phase 5 — fetch source code before/after the PR, build the final dataset |
| 08 | [`08_rate_limiting.md`](08_rate_limiting.md) | Adaptive GitHub API rate limiter (`github_pacing.py`) |
| 09 | [`09_pipeline_state.md`](09_pipeline_state.md) | Orchestration, experiment paths, marker files, checkpointing |
| 10 | [`10_streamlit_app.md`](10_streamlit_app.md) | The Streamlit dashboard (reading, comparing, launching) |
| 11 | [`11_output_catalog.md`](11_output_catalog.md) | Catalog of every file the pipeline writes, with schemas and examples |
| 12 | [`12_design_decisions.md`](12_design_decisions.md) | The 10 key design decisions, with alternatives considered |
| 13 | [`13_limitations_future_work.md`](13_limitations_future_work.md) | Known limitations and prioritized future improvements |

## Suggested reading order

### If you just want to run the pipeline

1. [`01_architecture.md`](01_architecture.md) — what the system does (10 min)
2. [`02_setup_installation.md`](02_setup_installation.md) — install and verify (30 min)

That's enough to launch your first experiment.

### If you want to understand a specific phase

Read the file for that phase. Each phase doc is self-contained:

- Phase 1 → [`03_phase1_discovery.md`](03_phase1_discovery.md)
- Phase 2 → [`04_phase2_issue_extraction.md`](04_phase2_issue_extraction.md)
- Phase 3 → [`05_phase3_pr_linking.md`](05_phase3_pr_linking.md)
- Phase 4 → [`06_phase4_llm_validation.md`](06_phase4_llm_validation.md)
- Phase 5 → [`07_phase5_build_dataset.md`](07_phase5_build_dataset.md)

### If you want to use the dataset (downstream analysis)

1. [`01_architecture.md`](01_architecture.md) — high-level picture
2. [`11_output_catalog.md`](11_output_catalog.md) — full file & field reference
3. [`06_phase4_llm_validation.md`](06_phase4_llm_validation.md) — what "validated" means
4. [`07_phase5_build_dataset.md`](07_phase5_build_dataset.md) — how the dataset is laid out

### If you want to extend or modify the code

1. [`01_architecture.md`](01_architecture.md) — the big picture
2. [`12_design_decisions.md`](12_design_decisions.md) — why things are the way they are
3. [`09_pipeline_state.md`](09_pipeline_state.md) — orchestration, paths, checkpoints
4. [`08_rate_limiting.md`](08_rate_limiting.md) — the API client every phase uses
5. The specific phase doc you're touching
6. [`13_limitations_future_work.md`](13_limitations_future_work.md) — what's already on the
   to-do list (don't reinvent the wheel)

### If you're evaluating the project (academic context)

Read in order:

1. [`01_architecture.md`](01_architecture.md)
2. [`12_design_decisions.md`](12_design_decisions.md)
3. [`06_phase4_llm_validation.md`](06_phase4_llm_validation.md)
4. [`13_limitations_future_work.md`](13_limitations_future_work.md)

These four cover the "what, why, how, what's next" arc.

## Other resources

- The repository's `README.md` at the root has a quick-start and
  pointers back here.
- The `pipeline/` folder contains the code with inline docstrings on
  every module. The docstrings document the **how**; this folder
  documents the **why** and the **big picture**.
- The `experiments/` folder contains the data the pipeline produces.
  Open any `config.json` to see the parameters that produced a given
  dataset.
