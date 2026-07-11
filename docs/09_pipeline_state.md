# 09 — Pipeline State & Orchestration

> Coordination layer between the 5 phases. Experiment paths,
> orchestrator, marker files, checkpoint helpers. The plumbing that
> makes everything else work.

This file covers four tightly-related modules:

- `config/experiment_paths.py` — path resolution
- `orchestration/run_pipeline.py` — the orchestrator
- `utils/checkpoint.py` — resume helper for phases 3 and 4
- Marker files (`pipeline_state.json`, `_runtime.json`, `_done.txt`)

## 1. The experiment ID cascade

Every script needs to know **which experiment** it's running for, so
it can write to the right folder. The ID is resolved in cascading
order by `_resolve_experiment_id()`:

```python
def _resolve_experiment_id() -> str:
    # 1. EXPERIMENT_ID environment variable (explicit override)
    env_id = os.getenv("EXPERIMENT_ID")
    if env_id:
        return env_id.strip()

    # 2. current_experiment.txt file next to this module
    if _CURRENT_FILE.exists():
        content = _CURRENT_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content

    # 3. Fallback for backward compatibility
    return "exp_default"
```

This three-step cascade exists because the project has three usage
modes that need different selection mechanisms:

| Mode | Selection method |
|------|------------------|
| Dashboard launches a pipeline | Sets `EXPERIMENT_ID` env var in the subprocess |
| User runs `python run_pipeline.py` from terminal | Reads `current_experiment.txt` |
| Legacy / no setup | Falls back to `exp_default` |

The dashboard never writes `current_experiment.txt` — that file is
only touched by the manual workflow. This prevents two Streamlit
sessions running different experiments from clobbering each other.

## 2. Centralized paths

Every phase imports `paths` from `experiment_paths.py`:

```python
from config.experiment_paths import paths

INPUT_FILE  = str(paths.step1_repo_json)
OUTPUT_FILE = str(paths.step2_issues_json)
paths.ensure_step_dir(2)
```

`paths` is a frozen dataclass built once at import:

```python
@dataclass(frozen=True)
class ExperimentPaths:
    experiment_id: str
    base: Path
    # Step 1 — Discovery
    step1_dir: Path
    step1_repo_json: Path
    step1_stats_json: Path
    # Step 2 — Issue extraction
    step2_dir: Path
    step2_issues_json: Path
    step2_stats_json: Path
    # ... 3, 4, 5 ...
    config_json: Path
```

The path layout is fixed:

```python
def _build_paths(experiment_id: str) -> ExperimentPaths:
    base = _resolve_experiments_root() / experiment_id
    s1 = base / "1_discovery"
    s2 = base / "2_issue_discovery"
    s3 = base / "3_pr_linking"
    s4 = base / "4_ai_validation"
    s5 = base / "5_dataset"

    return ExperimentPaths(
        experiment_id=experiment_id,
        base=base,
        step1_dir=s1,
        step1_repo_json=s1 / "repo_user_story.json",
        ...
    )
```

The base of `experiments/` is itself resolvable via env var:

```python
def _resolve_experiments_root() -> Path:
    env_root = os.getenv("EXPERIMENTS_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return _DEFAULT_EXPERIMENTS_ROOT  # <repo_root>/experiments/
```

Useful for testing (point to an alternative folder) or for installing
the pipeline somewhere unusual.

## 3. The orchestrator

`run_pipeline.py` is the single entry point that runs the 5 phases in
sequence. It's a pure subprocess driver: each phase is launched as a
child Python process, with stdout drained line-by-line into multiple
log files.

### Phase declaration

```python
FASI = [
    (1, "1_discovery.py"),
    (2, "2_issue_discovery.py"),
    (3, "3_pr_linking.py"),
    (4, "4_us_validation.py"),
    (5, "5_build_dataset.py"),
]
```

### Sequential execution

Each phase starts only if the previous one exited with code 0:

```python
for phase_num, script_name in FASI:
    if not should_run_phase(phase_num, args):
        continue
    exit_code = run_phase(phase_num, script_name)
    if exit_code != 0:
        _log_phase_error(phase_num, exit_code)
        break  # do not run downstream phases
    _mark_phase_done(phase_num)
```

No polling, no shared in-memory state, no atomic writes between live
processes. Each phase writes its own files; when one finishes, nobody
is reading its outputs anymore.

### CLI flags

```bash
python run_pipeline.py                # all phases 1 → 5
python run_pipeline.py --from 3       # resume from phase 3 onward
python run_pipeline.py --only 4       # run only phase 4
```

`--from N` is the workhorse for resuming after a crash. `--only N` is
useful when iterating on a single phase (e.g. tweaking the AI prompt
in phase 4 without re-running phases 1-3).

### Log streaming

Subprocess stdout is drained by a thread, writing every line to three
places:

```python
def _stream_output(stream, phase_log_path: Path, prefix: str) -> None:
    """Drain a subprocess's stdout line by line.

    Each line ends up in 3 places:
      1. the orchestrator's stdout (prefixed with [F3], [F4], ...)
      2. _logs/phase_N.log          (log dedicated to the phase, no prefix)
      3. _pipeline.log              (unified log, with prefix and timestamp)
    """
```

A single lock (`_LOG_LOCK`) protects both stdout and the file writes,
so lines from parallel phases never get interleaved mid-line.

### Windows quirk: CREATE_NO_WINDOW

On Windows, subprocesses doing HTTP can crash with
`STATUS_CONTROL_C_EXIT` (exit code 3221225786) due to a deep interaction
between the Python launcher and console handling. The orchestrator
launches children with `CREATE_NO_WINDOW`:

```python
if sys.platform == "win32":
    creationflags = subprocess.CREATE_NO_WINDOW
else:
    creationflags = 0
```

Without this flag, phase 1 (the first one to do HTTP) crashes within
seconds.

## 4. Marker files

Three files in the experiment root coordinate the orchestrator with
the dashboard:

### `pipeline_state.json` — live progress

Written by the orchestrator after every phase finishes. The dashboard
polls this file to show the live state.

```json
{
  "phase_1_done": true,
  "phase_2_done": true,
  "phase_3_done": true,
  "phase_4_done": true,
  "phase_5_done": true,
  "started_at": "2026-05-19T11:26:37",
  "errors": [],
  "phase_1_done_at": "2026-05-19T11:38:05",
  "phase_2_done_at": "2026-05-19T12:17:43",
  ...
}
```

Only the orchestrator writes this file (no race). The dashboard reads
in `try/except`.

### `_runtime.json` — running-process info

Written at orchestrator startup, used by the dashboard to detect
externally-launched runs (e.g. one started from the terminal while the
dashboard is open):

```json
{
  "experiment_id": "run_statistica_prova_100Repo",
  "pid": 6508,
  "started_at": "2026-05-19T11:26:37.214211",
  "cwd": "C:\\Users\\...\\pipeline",
  "cmd": ["python.exe", "-u", "orchestration/run_pipeline.py"],
  "finished_at": null,
  "exit_code": null
}
```

`finished_at` and `exit_code` stay `null` until the orchestrator exits.
The dashboard checks if `pid` is alive (using `psutil`) to distinguish
"running" from "crashed without cleanup".

### `_done.txt` — terminal marker

Written **only** when all 5 phases have completed successfully:

```
completed_at: 2026-05-19T18:07:03
elapsed_sec: 24026
phases_completed: [1, 2, 3, 4, 5]
```

The dashboard uses this file to mark an experiment as "fully done" in
the sidebar (the `✅` icon next to the experiment name).

## 5. Checkpointing (phases 3 and 4)

Phases 3 and 4 can take hours. If they crash mid-run (network failure,
Ollama timeout, manual Ctrl+C), restarting from scratch would waste
everything. The `utils/checkpoint.py` module provides a shared resume
pattern:

```python
from utils.checkpoint import load_resume

issues_with_pr, issues_without_pr, processed_issues = load_resume(
    output_file=OUTPUT_FILE,
    discarded_file=DISCARDED_FILE,
    output_label="issues with PR already found",
    discarded_label="issues without PR already recorded",
)
```

The function:

1. Reads the two output files if they exist (the "ok" list and the
   "discarded" list).
2. Builds a `set` of `"{repo}#{number}"` keys from both lists.
3. Returns the lists and the set.

The phase then iterates over the input and skips any issue whose key
is already in the set:

```python
for issue in input_issues:
    key = f"{issue['repo']}#{issue['number']}"
    if key in processed_issues:
        continue
    # ... process the issue ...
```

The pattern is identical between phase 3 and phase 4. Before
centralization the two scripts had a duplicated ~23-line block; the
helper removed the duplication while keeping the original prints
intact.

### Phase 5 has its own checkpoint

Phase 5's checkpoint format is slightly different (and uses
`_checkpoint.json`, separate from the output files) because it tracks
folders, not list entries:

```json
{
  "processed": [
    "bcgov/STRR#1142",
    "Genesis-Embodied-AI/genesis-world#1365",
    ...
  ],
  "stats": { ... }
}
```

See `07_phase5_build_dataset.md` for details on phase 5's
checkpointing.

## 6. Strategy of skipping phases

Combined, the flags and marker files give you these workflows:

**Full run from scratch:**
```bash
python run_pipeline.py
```

**Resume after a crash in phase 4:**
```bash
python run_pipeline.py --from 4
```
Phases 1-3 are already done (their outputs exist), the orchestrator
runs only phase 4 onward. Phase 4 itself reads the partial
`validated_issues.json` via `checkpoint.load_resume` and resumes
issue-by-issue from where it stopped.

**Iterate on the AI prompt:**
```bash
# Edit PROMPT_COT in 4_us_validation.py
# Delete the phase 4 output files
rm experiments/<id>/4_ai_validation/*.json
python run_pipeline.py --only 4
```
Phase 4 reruns from scratch on the existing phase 3 input. Phase 5
will need a re-run too (since its input changed):
```bash
rm -r experiments/<id>/5_dataset
python run_pipeline.py --only 5
```

**Test phase 5 changes on a small subset:**
The phase 5 checkpoint allows quick iteration. Edit the script,
delete the `_checkpoint.json` for the issues you want to reprocess,
re-run. Other issues stay cached.

## 7. The bootstrap log

In addition to the per-phase logs and `_pipeline.log`, the
orchestrator writes a `_bootstrap.log` early in startup, before the
main logging is wired up. It captures things like:

- which Python interpreter is running
- the resolved experiment ID
- the working directory
- env var snapshot for `EXPERIMENT_ID` and `EXPERIMENTS_ROOT`

Useful to diagnose "the dashboard said it was running experiment X
but actually it ran Y" type bugs.

## 8. The blacklist mechanism

`config.json` has an `exclude_from_experiments` list:

```json
"exclude_from_experiments": ["exp_001_baseline_100"],
"exclude_issues_from_experiments": [],
"exclude_rejected_issues_from": [],
"exclude_validated_issues_from": []
```

Phase 1 reads these at startup and pre-populates its `seen_repos` set
with all the repos already accepted by the named experiments:

```python
seen_repos = set(REPO_BLACKLIST)
```

Effect: a repo already discovered in `exp_001_baseline_100` is
automatically skipped here. Useful for ablation studies: "what new
repos does adding the TEMPLATE strategy find that the baseline didn't
have?"

Similar mechanisms exist for issues at phase 2 (`exclude_issues_from`)
and for validated/rejected issues at phase 4
(`exclude_rejected_issues_from`, `exclude_validated_issues_from`).

These are pure additive features: an empty list means no exclusion,
which is the default.
