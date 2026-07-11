# 01 — Architecture

> System-wide overview. Read this first: it provides the backbone
> for everything that follows.

## 1. What the system does

This project is a **mining pipeline** that extracts from GitHub a dataset of
**user stories in Connextra format** ("As a / I want / so that"), linked to
the **Pull Requests** that implemented them and to all the surrounding
context: issues, commits, modified files, diffs, merge commits.

The output is a structured dataset designed for research analysis: for each
user story found, the pipeline collects the issue metadata, the linked PR
data, the source code before and after the change, and a series of computed
fields (time between issue creation and merge, file distribution by
language, AI classification of the user story itself).

Alongside the pipeline, a **Streamlit dashboard** allows users to explore
experiments, compare them, launch new runs, and chat with the data through
a local LLM.

## 2. The two halves of the project

The software has two distinct but tightly coupled parts:

- **Pipeline** (`pipeline/`) — the data extraction component. A 5-phase
  terminal script that produces JSON files inside an `experiments/`
  directory.
- **Dashboard** (`software_stats/user_story_analyzer/`) — the Streamlit
  interface that reads the `experiments/` folders, displays charts and
  statistics, and lets users launch new pipelines.

The two halves **do not talk in memory**: they communicate exclusively
through files on disk. The dashboard reads the pipeline outputs; when it
needs to start a pipeline, it spawns the process as a separate subprocess.
This isolation lets users launch the pipeline from the terminal, from the
dashboard, or from both at the same time, without conflicts.

## 3. The 5-phase pipeline

```
┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐
│ 1. DISCOVERY    │──▶│ 2. ISSUE        │──▶│ 3. PR LINKING   │
│ find candidate  │   │    EXTRACTION   │   │ link issues     │
│ repositories    │   │ fetch issues    │   │ to PRs          │
└─────────────────┘   └─────────────────┘   └─────────────────┘
                                                      │
                                                      ▼
                      ┌─────────────────┐   ┌─────────────────┐
                      │ 5. BUILD        │◀──│ 4. AI           │
                      │    DATASET      │   │    VALIDATION   │
                      │ fetch source    │   │ Ollama decides  │
                      │ code and diffs  │   │ if it's a US    │
                      └─────────────────┘   └─────────────────┘
```

Each phase is a standalone Python script under `pipeline/steps/`. The
5 phases run in **pure sequence**, orchestrated by `run_pipeline.py`: each
phase starts only if the previous one terminated with exit code 0.

Inputs and outputs, phase by phase:

| Phase | Input | Main output |
|------|-------|------------|
| 1. Discovery | experiment configuration | `repo_user_story.json` — list of candidate repositories |
| 2. Issue Extraction | output of phase 1 | `issues_user_story.json` — closed "user-story-like" issues |
| 3. PR Linking | output of phase 2 | `issues_with_pr.json` + `issues_without_pr.json` |
| 4. AI Validation | output of phase 3 | `validated_issues.json` + `discarded_issues_ai.json` |
| 5. Build Dataset | output of phase 4 | `5_dataset/` folder with `data.json` and source files per issue |

> For the full schema of each file, see `11_output_catalog.md`.

## 4. The "experiment" concept

The whole pipeline is organized around the **experiment** concept: a
single isolated run inside its own folder. The goal is to allow repeating
the extraction with different parameters (more repositories, a different
AI model, different discovery strategies) without contaminating previous
results.

Each experiment has:

- a **unique ID** (e.g. `run_statistica_prova_100Repo`)
- a **dedicated folder** under `experiments/<id>/`
- a **configuration file** `config.json` that pins down the parameters
- a set of **subfolders**, one per phase

```
experiments/
└── run_statistica_prova_100Repo/
    ├── config.json              ← experiment parameters
    ├── pipeline_state.json      ← phase state (1_done? 2_done? ...)
    ├── _runtime.json            ← PID and timestamp of the running run
    ├── _done.txt                ← completion marker
    ├── _bootstrap.log
    ├── _pipeline.log            ← unified log of all phases
    ├── _logs/
    │   ├── phase_1.log
    │   ├── phase_2.log
    │   └── ...
    ├── 1_discovery/
    │   ├── repo_user_story.json
    │   ├── run_stats.json
    │   └── DEBUG_*.json
    ├── 2_issue_discovery/
    │   ├── issues_user_story.json
    │   └── run_stats.json
    ├── 3_pr_linking/
    │   ├── issues_with_pr.json
    │   ├── issues_without_pr.json
    │   └── run_stats.json
    ├── 4_ai_validation/
    │   ├── validated_issues.json
    │   ├── discarded_issues_ai.json
    │   └── run_stats.json
    └── 5_dataset/
        ├── _checkpoint.json
        ├── _run_stats.json
        └── <repository_path>/   ← one subtree per issue
```

Active experiment selection: the ID is resolved in cascading order by
`config/experiment_paths.py`:

1. environment variable `EXPERIMENT_ID` (explicit override)
2. file `pipeline/config/current_experiment.txt`
3. fallback `exp_default`

All phase scripts import `from config.experiment_paths import paths` and
use `paths.step1_repo_json`, `paths.step2_issues_json`, etc., instead of
hardcoding paths. This means changing the ID switches the entire output
tree without touching the code.

## 5. Cross-cutting components

Three modules are used by multiple phases and deserve a dedicated section
in the documentation:

- **Adaptive rate limiting** (`utils/github_pacing.py`) — manages GitHub
  API calls (REST + GraphQL) respecting the 5000 requests/hour limit, with
  dynamic throttling, handling of 403/secondary rate limits, and
  automatic pause when the call budget is about to be exhausted.
  See `08_rate_limiting.md`.
- **Per-phase checkpointing** (`utils/checkpoint.py`) — allows resuming an
  execution interrupted mid-phase (especially useful in phases 4 and 5,
  which can take hours). See `09_pipeline_state.md`.
- **Logging** (`utils/logging_utils.py`) — consistent log formatting
  across phases, separation between per-phase log and unified log.

## 6. The Streamlit dashboard

The `user_story_analyzer` dashboard is a multi-page Streamlit application
living in `software_stats/user_story_analyzer/`. It works both as an
"experiment reader" and as a "pipeline launcher".

High-level structure:

- `app.py` — entry point: sidebar configuration, active experiment selection
- `core/` — modules shared across pages:
  - `loader.py` — loads experiment JSON files with Streamlit caching
  - `launcher.py` — starts pipelines as subprocesses
  - `queue_runner.py` — sequential experiment queue system
  - `llm_context.py` — builds the context for the LLM chat page
  - `ui.py` — reusable components
- `pages/` — ~10 Streamlit pages, each with a specific purpose:
  - `0_Overview.py` — at-a-glance view of the active experiment
  - `1_Discovery.py` ... `5_Dataset.py` — per-phase analysis
  - `6_Comparisons.py` — ablation studies across experiments
  - `7_Chat.py` — chat with the dataset via Ollama
  - `8_Experiments.py` — experiment management (creation, deletion)
  - `9_Launch.py` — launch interface + queue

Details in `10_streamlit_app.md`.

## 7. Technologies and rationale

| Choice | Why |
|--------|-----|
| **Python 3.13** | Mature ecosystem for data wrangling, first-class GitHub libraries |
| **PyGithub** | REST wrapper for GitHub, automatic pagination, typed objects |
| **Direct GraphQL via `requests`** (phase 3) | `closingIssuesReferences` is exposed only in GraphQL, not in REST. More efficient for PR linking |
| **Local Ollama** | Local LLM means no token cost, reproducible, no data sent to third-party clouds |
| **`qwen2.5:7b`** | Good quality/speed balance on local hardware; supports `format="json"` |
| **Streamlit** | Quick dashboard without writing a dedicated frontend |
| **JSON** as exchange format | Readable, debuggable by hand, perfect for semi-structured data |

Detailed rationale lives in `12_design_decisions.md`.

## 8. Glossary

| Term | Meaning |
|---------|-------------|
| **User story (Connextra)** | A requirement written in the format *As a \<role\>, I want \<feature\> so that \<benefit\>*. "Complete" has all three parts, "partial" has two |
| **Discovery strategy** | One of the 5 modes phase 1 uses to find repositories: LABEL, TEMPLATE, TAG, MENTION, CONNEXTRA |
| **Experiment** | An isolated pipeline run with a frozen config and a dedicated folder |
| **Issue** | On GitHub, a ticket associated with a repository (bug, feature request, discussion). For us, it is the **vehicle** that carries the user story |
| **PR (Pull Request)** | A proposed code change. It can "close" one or more issues through references like `Closes #123` or `Fixes #45` |
| **Cutoff date** | Date limit for filtering issues/repos. The pipeline supports one (`cutoff_date_closed`), but there is a misalignment between phase 1 and phase 2 documented in `13_limitations_future_work.md` |
| **"Seen" repo** | In phase 1, every repository is assigned to ONE AND ONLY ONE strategy through a global `repo_visti` set. Overlap between strategies is always 0 by design |
| **AI validation** | Step in phase 4 where Ollama decides whether an issue actually contains a user story, and extracts role/feature/benefit in structured JSON |
| **Connextra complete / partial** | "complete" = has all three parts (As a / I want / so that); "partial" = has only two |
| **CoT / fallback** | Two prompting paths in phase 4: Chain-of-Thought (more reasoned) and fallback (more direct). See `06_phase4_llm_validation.md` |
| **Marker files** | State files like `pipeline_state.json`, `_runtime.json`, `_done.txt` polled by the dashboard to learn how far a running pipeline has progressed |