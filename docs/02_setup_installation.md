# 02 — Setup & Installation

> How to install, configure and run the pipeline + dashboard from
> scratch. Covers both terminal use and dashboard launch.

## 1. Prerequisites

You need the following installed on your machine:

| Tool | Version | Why |
|------|---------|-----|
| **Python** | 3.13 | The pipeline uses 3.13-specific syntax (PEP 604 unions, etc.) |
| **Ollama** | latest | Local LLM runtime for phase 4 and the chat dashboard |
| **Git** | any recent | To clone the repo |
| **GitHub account + token** | — | For API access. A classic Personal Access Token with `public_repo` scope is enough |

The project has been developed and tested on Windows with PowerShell.
It also runs on Linux and macOS, but the Windows MAX_PATH workaround
in phase 5 (see `07_phase5_build_dataset.md`) is the only
Windows-specific quirk — on Unix it's a no-op.

## 2. Installation

### Clone the repository

```bash
git clone <repo-url>
cd stage-dataset
```

### Create a virtual environment

```bash
python -m venv .venv

# Windows PowerShell
.venv\Scripts\Activate.ps1

# Linux / macOS
source .venv/bin/activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

The main dependencies are:

- `requests` — direct HTTP for GraphQL and file downloads
- `PyGithub` — REST API wrapper (used incidentally; the pipeline
  mostly uses raw `requests` for finer control)
- `ollama` — Python client for the local Ollama server
- `streamlit` — the dashboard
- `python-dotenv` — `.env` loader for the GitHub token
- `pandas`, `plotly` — used by the dashboard

## 3. GitHub token

Create a Personal Access Token at
<https://github.com/settings/tokens>. Classic tokens with the
`public_repo` scope are enough; fine-grained tokens also work but
need read access to "Contents", "Issues", and "Pull requests" on all
public repositories.

Save the token in a `.env` file at the repository root:

```
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

The `.env` file is loaded automatically by `github_pacing.py` at
import. If the token is missing, the module raises a `ValueError` at
startup, so you can't accidentally run a phase with no token.

## 4. Ollama setup

### Install Ollama

Follow <https://ollama.com/download> for your platform. On Windows,
the installer registers Ollama as a background service; on Linux /
macOS, you start it manually with `ollama serve`.

### Pull the model

```bash
ollama pull qwen2.5:7b
```

The pipeline expects a model tagged `qwen-gpu`. Create the alias:

```bash
ollama cp qwen2.5:7b qwen-gpu
```

(Or change `MODEL_NAME` at the top of
`pipeline/steps/4_us_validation.py` to whatever model name you
prefer.)

### Verify it works

```bash
ollama run qwen-gpu "Hello"
```

You should get a response. If you don't, the rest of phase 4 won't
work either. Ollama listens on `http://localhost:11434` by default;
the pipeline assumes that endpoint.

## 5. Create your first experiment

Two ways:

### From the dashboard (recommended)

```bash
streamlit run software_stats/user_story_analyzer/app.py
```

Open <http://localhost:8501>, go to the **Launch** page, fill the
form (experiment ID, parameters), press **Launch**. The dashboard
generates `experiments/<id>/config.json` and spawns the pipeline.

### From the terminal

```bash
python pipeline/orchestration/new_experiment.py my_first_experiment
```

This creates `experiments/my_first_experiment/config.json` with
default values. Edit it to taste, then run:

```bash
cd pipeline
python orchestration/run_pipeline.py
```

The orchestrator uses the experiment ID written in
`pipeline/config/current_experiment.txt` (the `new_experiment.py`
script writes this for you). To run a different experiment, set the
env var explicitly:

```powershell
# PowerShell
$env:EXPERIMENT_ID = "my_other_experiment"
python orchestration/run_pipeline.py
```

```bash
# bash
EXPERIMENT_ID=my_other_experiment python orchestration/run_pipeline.py
```

## 6. Orchestrator flags

The `run_pipeline.py` script supports these flags:

```bash
python orchestration/run_pipeline.py                # all 5 phases
python orchestration/run_pipeline.py --from 3       # resume from phase 3
python orchestration/run_pipeline.py --only 4       # run only phase 4
```

The `--from N` flag is the most common: use it to resume after a
crash or after editing a phase. Phases earlier than N must already
have completed (their output files must exist).

The `--only N` flag is useful when iterating on a single phase. The
upstream phases' outputs are reused as-is; downstream phases are
**not** rerun automatically (delete their output folders manually if
you want them re-executed).

## 7. Launch the dashboard

```bash
streamlit run software_stats/user_story_analyzer/app.py
```

The first launch opens a browser tab automatically. Subsequent
launches connect to the existing server. To stop it: Ctrl+C in the
terminal.

The dashboard discovers experiments by scanning `experiments/`. Newly
created experiments appear immediately in the sidebar dropdown.

## 8. Verify everything works

A minimal end-to-end test that runs in ~5 minutes:

```bash
# 1. Create a tiny experiment
python pipeline/orchestration/new_experiment.py test_minimal
```

Edit `experiments/test_minimal/config.json` and set:

```json
{
  "discovery_config": {
    "max_total_repos": 3,
    "max_repos_per_strategy": 1,
    "min_stars": 100,
    "languages": ["Python"],
    "language_mode": "primary_only",
    "active_strategies": ["LABEL"],
    "time_windows_granularity": "yearly"
  },
  "dataset_config": {
    "max_issues_per_repo": 5
  }
}
```

```bash
# 2. Run the pipeline
cd pipeline
python orchestration/run_pipeline.py
```

If everything is set up correctly, you should see all 5 phases
complete in about 5 minutes and produce a small (~5-15 issues)
dataset under `experiments/test_minimal/5_dataset/`.

## 9. Common installation problems

### "GITHUB_TOKEN not found"

The `.env` file must be at the **repository root**, not inside
`pipeline/`. The `load_dotenv()` call looks up from the current
working directory; running scripts from `pipeline/` should still
find it, but if you've moved the file, restore it to the root.

### "Connection refused" on phase 4

Ollama is not running. On Windows, check the system tray for the
Ollama icon. On Linux/macOS, run `ollama serve` in a separate
terminal.

### "Model not found: qwen-gpu"

You haven't created the alias. Run:

```bash
ollama pull qwen2.5:7b
ollama cp qwen2.5:7b qwen-gpu
```

### `STATUS_CONTROL_C_EXIT` on Windows

This is the missing `CREATE_NO_WINDOW` flag — but the current code
already has it. If you see this anyway, you're probably running
phase scripts directly instead of through `run_pipeline.py`. Run
them through the orchestrator.

### Phase 1 returns 0 repos

Your token might lack the required scope, or rate limit is
exhausted. Check the headers in `_logs/phase_1.log`. The token must
have `public_repo` scope; classic-token authentication is required
(fine-grained tokens with read-only access work but need precise
permissions).

### Streamlit can't find any experiments

The dashboard expects `experiments/` to be a sibling of `pipeline/`.
If you've moved things, set the sidebar's "experiments/ folder"
field to the correct path.