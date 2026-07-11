# 10 — Streamlit Dashboard

> Multi-page Streamlit application that reads experiments, runs new
> ones, and lets you chat with the data through a local LLM.

## 1. What the dashboard is for

The `user_story_analyzer` is a Streamlit application that lives in
`software_stats/user_story_analyzer/`. It serves four use cases:

1. **Reading** experiments — explore the per-phase results, see
   charts and tables.
2. **Comparing** experiments — ablation studies, overlap analysis,
   strategy effectiveness across runs.
3. **Launching** new experiments — generate a `config.json` from a
   UI form, then run the pipeline as a subprocess.
4. **Chatting** with the data — natural-language Q&A using a local
   LLM with the experiment stats as context.

The dashboard is the "human-facing" half of the project. The pipeline
runs autonomously; the dashboard is where a human looks at the
results and decides what to do next.

## 2. How to launch it

```bash
streamlit run software_stats/user_story_analyzer/app.py
```

Or, if `start_analyzer.py` is present in the repo root, just:

```bash
python start_analyzer.py
```

By default it opens at `http://localhost:8501`. The app reads the
`experiments/` folder at the project root (sibling of `pipeline/`)
unless you point it to a different folder from the sidebar.

## 3. Application structure

```
software_stats/user_story_analyzer/
├── app.py                  ← entry point: experiment selection + landing
├── requirements.txt
├── core/                   ← shared modules
│   ├── loader.py           ← loads JSON files with caching
│   ├── launcher.py         ← spawns run_pipeline.py
│   ├── queue_runner.py     ← experiment queue worker
│   ├── llm_context.py      ← builds context for the chat page
│   └── ui.py               ← reusable UI components
├── pages/                  ← one file per Streamlit page
│   ├── 0_Overview.py
│   ├── 1_Discovery.py
│   ├── 2_Issue_extraction.py
│   ├── 3_PR_linking.py
│   ├── 4_AI_Validation.py
│   ├── 5_Dataset.py
│   ├── 6_Comparisons.py
│   ├── 7_Chat.py
│   ├── 8_Experiments.py
│   └── 9_Launch.py
├── static/                 ← CSS, images
└── .streamlit/             ← Streamlit config
```

Streamlit auto-generates the sidebar navigation from the `pages/`
folder — files are listed in the order their numeric prefix suggests.

## 4. The `core/` modules

### `loader.py` — data loading with caching

```python
@dataclass
class PipelineOutput:
    """Container for all data loaded from a pipeline run."""
    root: Path
    experiment_id: str
    config: dict
    repo: list
    issues: list
    issues_with_pr: list
    validated_issues: list
    discarded_issues: list
    discovery_stats: dict
    issue_stats: dict
    pr_link_stats: dict
    validation_stats: dict
    dataset_stats: dict
```

The `load_output(root)` function reads every JSON file in the
experiment folder into one `PipelineOutput` dataclass. The result is
cached in `st.session_state.data` so that switching between pages
doesn't re-read the disk.

`list_experiments(experiments_root)` reuses the same function from
`config/experiment_paths.py` (the dashboard imports a pipeline module
here) so the dashboard sees exactly the same experiments the pipeline
does. No double source of truth.

### `launcher.py` — spawning pipelines

The dashboard never calls phase code in-process. Pipelines are
launched as **detached subprocesses** with their own `EXPERIMENT_ID`
env var:

```python
def launch_pipeline(experiment_id: str, ...) -> int:
    env = os.environ.copy()
    env["EXPERIMENT_ID"] = experiment_id

    proc = subprocess.Popen(
        [sys.executable, "-u", "orchestration/run_pipeline.py"],
        cwd=PIPELINE_ROOT,
        env=env,
        stdout=open(LOG_FILE, "w"),
        stderr=subprocess.STDOUT,
        creationflags=DETACHED_PROCESS_FLAG,
    )
    return proc.pid
```

The PID is written to `_runtime.json` (see `09_pipeline_state.md`).
Parallel Streamlit sessions can launch different experiments without
clobbering each other because the env var, not
`current_experiment.txt`, is used to select the experiment in the
subprocess.

`build_config(...)` generates the `config.json` from the UI form,
mirroring the logic of `orchestration/new_experiment.py`.

### `queue_runner.py` — sequential experiment queue

The queue lets you stack experiments to run one after the other. Used
when you want to leave the machine running overnight on multiple
configurations.

Two files in `experiments/`:

- `_queue.json` — the queue state (list of pending/running/done items)
- `_queue_worker.json` — runtime info of the worker subprocess (PID,
  start time)

The worker is itself a subprocess: it picks the first pending item,
launches `run_pipeline.py` for it, waits for completion, marks it
done, picks the next. Only **one worker** can be active at a time
(enforced by checking if the PID in `_queue_worker.json` is alive).

The dashboard UI shows:

- The queue contents (pending / running / completed / failed)
- A "Start worker" button (no-op if a worker is already running)
- A "Stop worker" button (kills the worker subprocess)
- Per-experiment add/remove/reorder controls

### `llm_context.py` — chat context builder

For the chat page, the dashboard needs to give the LLM enough context
about the current experiment so it can answer questions like "how
many repos did the CONNEXTRA strategy find?" without hallucinating.

`build_context(data)` compiles a structured dictionary from the
already-loaded `PipelineOutput`, picking out the most informative
fields: discovery config, per-strategy stats, validation distribution,
top roles, top action verbs, etc. The dict is serialized to JSON and
prepended to the prompt.

The chat itself uses the same local Ollama endpoint as phase 4
(`http://localhost:11434`) so there's no extra dependency.

### `ui.py` — reusable components

CSS injection, the page hero banner, custom badges, and a few helper
widgets shared across pages. Small file, mostly to centralize style.

## 5. The pages

Each `pages/*.py` file is an independent Streamlit script. The
sidebar navigation is generated automatically.

### `0_Overview.py`

Single-screen dashboard that shows the headline numbers for the
active experiment: number of repos found, issues extracted, issues
linked to PRs, AI-validated, validation rate. Sparkline-style charts
where appropriate.

### `1_Discovery.py`

Phase 1 analysis: per-strategy acceptance breakdown, stars
distribution, creation-year distribution, language distribution,
top-10 repos by stars. Reads `discovery_stats` from `PipelineOutput`.

### `2_Issue_extraction.py`

Phase 2 analysis: issues per repo distribution, per-strategy regex
discard rates, top repos by issue count, repos that contributed zero
issues (the cutoff-misalignment symptom).

### `3_PR_linking.py`

Phase 3 analysis: linking success rate, distribution of PRs per
issue, link source breakdown (closing / cross_reference /
timeline_event), `time_issue_to_merge` histogram.

### `4_AI_Validation.py`

Phase 4 analysis: validation rate, CoT vs fallback path breakdown,
confidence distribution, Connextra complete/partial split, top roles,
top action verbs, validation rates per discovery strategy. The most
chart-heavy page.

### `5_Dataset.py`

Phase 5 browser: navigate the per-issue folders, view `data.json`,
list original/modified files, inspect diffs. Includes language
distribution, file-extension histogram, and the diff-size
distribution.

### `6_Comparisons.py`

Ablation studies across experiments: pick 2-N experiments and compare
their per-strategy results. Useful to answer questions like:
- "How much does adding the TEMPLATE strategy help?"
- "Does increasing `max_pages` from 5 to 10 help, and at what cost?"
- "Which model produces the highest CoT success rate?"

Numerical tables and side-by-side bar charts.

### `7_Chat.py`

Natural-language Q&A on the active experiment. Uses the local Ollama
endpoint with the chat-context dict from `core/llm_context.py`.
Example questions:
- "How many repos found by CONNEXTRA were validated?"
- "What's the median time from issue creation to PR merge?"
- "List the top 5 roles found in the validated user stories."

The model gets the structured context as JSON; it does not see the
raw issue bodies (too big to fit in the context window).

### `8_Experiments.py`

Experiment management: list all experiments with their config
summary, delete experiments, rename them. Pure file-management UI.

### `9_Launch.py`

The most complex page. UI form to configure a new experiment:

- Experiment ID (with validation: no spaces, no clashes)
- Description (free text)
- Run mode (`testing` or `full_coverage`)
- Discovery config:
  - max repos total / per strategy
  - min stars, languages, language mode
  - active strategies (checkboxes)
  - time windows granularity
- Dataset config:
  - cutoff date
  - max issues per repo
  - language file filters
- Exclusions:
  - which experiments' repos/issues/validated/rejected to skip

On "Launch", the page:

1. Calls `build_config(...)` to assemble the dict.
2. Creates the experiment folder via `experiments/<id>/`.
3. Writes `config.json`.
4. Spawns `run_pipeline.py` via `launcher.launch_pipeline(...)`.
5. Shows the live log tail + phase progress (reads
   `pipeline_state.json` and the per-phase logs).

The page also detects **externally-launched runs**: if `_runtime.json`
exists with a live PID, the page shows the live progress even if you
didn't launch the run from this Streamlit session. This is what makes
"start from terminal, watch from dashboard" possible.

The page also has a section for the queue (`queue_runner.py`): add
the current configuration to the queue, view the queue, start/stop
the worker.

## 6. Two-way communication via files only

There is **no in-memory communication** between the dashboard and a
running pipeline. Everything goes through files:

- Pipeline → Dashboard: `pipeline_state.json`, `_runtime.json`,
  `_done.txt`, per-phase output files, log files.
- Dashboard → Pipeline: `config.json` (read at the start of every
  phase), `EXPERIMENT_ID` env var (set at subprocess launch).

This file-only protocol means:
- The dashboard can crash without affecting the pipeline.
- The pipeline can crash without affecting the dashboard.
- A pipeline running in a terminal is fully visible to a dashboard
  opened later.
- You can run two dashboards (e.g. one local, one over SSH) reading
  the same `experiments/` folder.

## 7. Session state caveats

Streamlit's session state (`st.session_state`) is **per-browser-tab**.
The loaded `PipelineOutput` lives there. Consequences:

- Switching tabs in the same browser shares the state.
- Opening a new browser window loses the state (the experiment must
  be reloaded).
- Refreshing the page keeps the state.

The reload of an experiment is fast (~1 second for a 100-repo run)
because the JSON files are small. The slowest part is loading the
phase 5 file-by-file iteration on the dataset, which is lazy: it
only runs when the user opens the Dataset page.

## 8. Live progress polling

When a pipeline is running, the Launch page polls `pipeline_state.json`
and the active phase's `run_stats.json` (if present) at ~2-second
intervals. The poll is implemented with `st.rerun()` after a
`time.sleep(2)` — crude but reliable.

Showing live progress for a 4-hour phase is a tricky UX problem: the
page must:
- not block the Streamlit thread (use polling, not subscription)
- not hammer the disk (2-second poll is fine)
- recover gracefully if the pipeline dies (detects stale
  `_runtime.json`)

The current implementation works well enough but is a good candidate
for future refactoring with `streamlit.fragment` (introduced in
recent Streamlit versions) for more efficient partial reruns.
