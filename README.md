# STORY2DIFF — User Story Mining Pipeline

> A dataset of real-world user stories linked to their implementations,
> and the pipeline that builds it. Each record connects a Connextra user
> story written in a GitHub issue to the merged pull request that closed
> it, its commit history, and the file-level code changes.

This repository accompanies the paper *"STORY2DIFF: A Dataset Linking Real-World User Stories to Code Changes"*, submitted to the
ICSE 2027 Demonstrations and Data Showcase track.

- **Dataset:** release [`v4.0.1`](https://github.com/riganelli/user-story-mining-pipeline/releases/tag/V4.0.)
  
## What this is

The project crawls GitHub looking for issues that contain **user
stories** in the Connextra format ("As a / I want / so that"), and for
each one builds a self-contained record with:

- Issue metadata (title, body, labels, dates, author)
- The Pull Request that closed it (commits, branches, merge info)
- The source code **before and after** the change
- An AI-classified summary (role, feature, benefit, confidence)
- Computed fields (timeline deltas, language breakdown, file diff
  summary)

The output is intended as a corpus for empirical software engineering
research on the requirements → implementation link.

Alongside the pipeline there is a **Streamlit dashboard** for
exploring, comparing, and launching experiments — including a chat
interface to ask natural-language questions about the data.

## Stack

- **Python 3.13**
- **`requests`** for the GitHub REST API
- **GraphQL** (direct via `requests`) for PR linking
- **Ollama** running locally for the LLM validation (default model:
  `qwen2.5:7b`)
- **Streamlit** for the dashboard

Everything runs locally; no hosted LLM API is required.

## Quick start

```bash
# 1. Clone and install
git clone <repo-url>
cd user-story-mining-pipeline 
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 2. Configure the GitHub token
echo "GITHUB_TOKEN=ghp_xxx..." > .env

# 3. Install and pull the LLM model
ollama pull qwen2.5:7b
ollama create qwen-gpu -f Modelfile

# 4. Create and run an experiment
python pipeline/orchestration/new_experiment.py my_first_run
cd pipeline
python orchestration/run_pipeline.py

# 5. Or launch the dashboard
streamlit run software_stats/user_story_analyzer/app.py
```

Full setup details, including troubleshooting, in
[`docs/02_setup_installation.md`](docs/02_setup_installation.md).

## Repository structure

```
user-story-mining-pipeline/
├── README.md                      ← this file
├── pipeline/                      ← the 5-phase pipeline
│   ├── orchestration/             ← run_pipeline.py, new_experiment.py
│   ├── steps/                     ← one script per phase
│   ├── llm_validation/            ← Ollama client and schema helpers
│   ├── utils/                     ← rate limiting, checkpoint, logging
│   └── config/                    ← experiment_paths, current_experiment.txt
├── software_stats/
│   └── user_story_analyzer/       ← the Streamlit dashboard
│       ├── app.py
│       ├── core/                  ← shared modules (loader, launcher, ...)
│       └── pages/                 ← one file per dashboard page
├── experiments/                   ← per-experiment output folders (git-ignored)
│   └── <experiment_id>/
│       ├── config.json
│       ├── 1_discovery/
│       ├── 2_issue_discovery/
│       ├── 3_pr_linking/
│       ├── 4_ai_validation/
│       └── 5_dataset/             ← the final deliverable
├── tools/                         ← dataset release tooling
│   ├── build_release.py           ← packages the per-window archives
│   ├── build_subset_release.py    ← packages the verbatim subset
│   └── extract_exact_match_benefit_opt.py  ← verbatim criterion definition
└── docs/                          ← documentation
    ├── 00_index.md                ← start here
    ├── 01_architecture.md
    └── ...
```

## Dataset

The dataset produced by the pipeline (20,618 records, 2017–2026) is
**not** stored in this repository: the `experiments/` folder is
git-ignored because of its size. The data is distributed as release
assets, one archive per temporal window plus the verbatim subset:

| Asset | Records | Size | SHA-256 |
|---|---|---|---|
| `R1_2023_2026.tar.gz` | 11,610 | 178 MB | `a58242c7f8afef44a6cfdff9aa824053bc633dcad548dec74b0b8cdf8d642436` |
| `R2_2020_2022.tar.gz` | 5,737 | 72 MB | `8d694a3ebbdc90637ab140cb1e5cb609dd3c43f394b39d641bab05b8849b67de` |
| `R3_2017_2019.tar.gz` | 3,271 | 74 MB | `4ae0a412687caff083a9e736608503826d7b7b7ce1378e4225e7a5e41cd7fdbe` |
| `verbatim_subset.tar.gz` | 15,734 | 232 MB | `77b458ffd20bccf5cad7b9907b856e761267f85e8b280488647db3bcd9aee226` |

Each window archive contains the final records (`5_dataset/`, full
code context included), the validation output
(`validated_issues.json`) and the per-run configuration and statistics
(`_meta/`) needed to verify the numbers reported in the thesis. R1 is
the merge of two runs (`w1` and `w1_plus`, disjoint by construction);
their metadata is kept separate under `_meta/`.

The **verbatim subset** (15,734 records, 76.3% of the corpus) is the
literal-match gold standard: records whose role and feature — and
benefit, when present — appear verbatim in the source issue text
(whitespace-normalized, case-insensitive). It ships in two forms:
the full archive above, and a lightweight index
(`verbatim_subset_index.json`) that references records inside the
window archives without duplicating them. A diff-free version of the
included records (`verbatim_subset_light.json.gz`) is also attached.

To reproduce the archives from raw experiment folders:

```bash
python tools/build_release.py --experiments-dir experiments \
    --w1plus-dir exp_w1_plus_2025_2026 --out release

python tools/build_subset_release.py --experiments-dir experiments \
    --w1plus-dir exp_w1_plus_2025_2026 --out release
```

Both scripts verify every record count against the published figures
and abort on any mismatch. The inclusion criterion of the subset is
defined in `tools/extract_exact_match_benefit_opt.py`.

## Documentation

Documentation lives under [`docs/`](docs/). Start with
[`docs/00_index.md`](docs/00_index.md) for a navigable map; the entry
points for each audience are:

- **Just want to run it** →
  [`docs/02_setup_installation.md`](docs/02_setup_installation.md)
- **Want to understand the architecture** →
  [`docs/01_architecture.md`](docs/01_architecture.md)
- **Want to use the dataset** →
  [`docs/11_output_catalog.md`](docs/11_output_catalog.md)
- **Want to know why things are the way they are** →
  [`docs/12_design_decisions.md`](docs/12_design_decisions.md)

## Context

This project was developed at the University of Milano-Bicocca.
