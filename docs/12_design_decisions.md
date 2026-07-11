# 12 — Design Decisions

> Why the project is built the way it is. Each section is one
> consequential design choice with the alternatives we considered
> and the reason for picking what we picked.

These are not "best practices" in some abstract sense — they are
trade-offs that made sense for this project's constraints (student
internship, single-developer, local hardware, public GitHub data).

## 1. Why local Ollama instead of a hosted LLM API

**The alternative considered:** OpenAI API or Anthropic API.

**Why local won:**

- **Cost.** Validating ~1200 issues per experiment against a paid
  API costs real money. We need to repeat experiments multiple times
  to tune prompts, change parameters, debug bugs. At ~$0.01 per
  issue, that's ~$12 per experiment — manageable for one run,
  prohibitive for iteration.
- **Reproducibility.** The exact model + weights are pinned locally.
  Hosted models silently change behavior behind the same API endpoint
  (OpenAI documents this explicitly for chat models). A run from
  three months ago cannot be reproduced bit-for-bit against the
  current GPT-4 endpoint.
- **Privacy.** Issue bodies don't leave the developer's machine. Not
  a strict requirement for public GitHub data, but a nice property
  to have by default.
- **No external dependency at run-time.** The pipeline works offline
  for everything except the GitHub API calls.

**Cost of the choice:**

- Inference is slower (~10 seconds per issue vs ~1 second for a
  hosted API). A full phase 4 run takes hours.
- Quality is lower than GPT-4 / Claude on subtle classification
  edges. The CoT+fallback strategy and aggressive prompt engineering
  partially compensate.

## 2. Why `qwen2.5:7b` and not a smaller/bigger model

**Alternatives considered:** `llama3.1:8b`, `mistral:7b`,
`qwen2.5:14b`, `qwen2.5:3b`.

**Why qwen2.5 7B:**

- Reliably emits JSON when prompted to. Smaller models (3B) fail
  schema validation 40-50% of the time.
- Fast enough on consumer GPUs (~10 seconds per issue) to do a full
  validation pass overnight.
- The 14B variant gives marginally better quality (~3-4% higher
  validation agreement on manual spot-checks) but is 2x slower —
  not worth it for the gain.
- `format="json"` support is solid in qwen2.5.

The choice is a balance between quality, speed and JSON reliability.
We didn't run a formal benchmark; the choice was driven by ~30 hours
of empirical comparison on the same issue set.

## 3. Why GraphQL `closingIssuesReferences` and not REST + heuristics

**The alternative considered:** scrape PR descriptions with
`requests` and parse out `Closes #N` / `Fixes #N` patterns with a
regex.

**Why GraphQL won:**

- GitHub already does the parsing. `closingIssuesReferences`
  returns the structured result of GitHub's own parser, including
  edge cases (numeric references in code blocks, references in
  different repos, etc.) that a custom regex would get wrong.
- One round-trip for N issues (via aliased GraphQL queries) vs N
  round-trips for the REST approach. ~10x fewer API calls.
- Cleaner rate limit story: the GraphQL bucket is independent of
  the core REST bucket used by phase 5, so phase 3 doesn't compete
  with phase 5 for the same budget.

**Cost of the choice:** more complex query strings, two-round
strategy (Round 1 = closing+cross, Round 2 = referenced_event)
needed to maximize recall.

## 4. Why single-pass PR linking instead of multi-pass

**The alternatives considered.** Besides the GraphQL pass
(`closingIssuesReferences` + cross references), two fallbacks were
prototyped and benchmarked:

- **Search API:** PRs whose body contains `#<issue_number>`
- **Commit Search API:** commits whose message contains
  `#<issue_number>`

**The benchmark over 1086 issues.** With all three passes:

- Success rate: 50.6%.
- GraphQL alone: 49.9%.
- The commit search found 0 PRs out of 1086 issues.
- The two fallbacks together took 43 extra minutes.

**The decision.** Keep GraphQL only. The 0.7% extra recall is not
worth 43 minutes of runtime and the rate limit pressure on the Search
bucket.

This is the kind of decision that looks obvious in retrospect, but
discovering that the commit search contributes literally zero required
actually building and benchmarking it.

## 5. Why 5 discovery strategies, not 1 or 10

**Alternatives:** start with just CONNEXTRA (most direct signal);
add more strategies opportunistically.

**Why 5 chosen strategies:**

- **LABEL** — cheap, low noise, high precision. Catches well-curated
  repos.
- **TEMPLATE** — catches repos with structural user-story discipline
  (often academic / consultancy projects).
- **TAG** — catches `[US-N]` style numbering, common in agile-heavy
  teams.
- **MENTION** — broad sweep, catches mentions in titles/bodies.
- **CONNEXTRA** — catches the actual Connextra pattern in the issue
  body. The most direct semantic signal.

Each strategy covers a different "type" of user-story discipline.
LABEL has near-zero false positives but misses anyone without the
label habit. CONNEXTRA has many false positives ("as a result"
matched literally) but catches issues no other strategy would.

The strict left-to-right ordering (LABEL → TEMPLATE → TAG → MENTION
→ CONNEXTRA) reflects precision-first: a repo that would match both
LABEL and CONNEXTRA gets attributed to LABEL, which gives cleaner
ablation numbers.

## 6. Why global deduplication of repositories between strategies

**The alternative considered:** allow a repo to be discovered by
multiple strategies. Each strategy reports its own set; overlap is a
property of the data.

**Why deduplication won:**

- **Clean ablation.** "How many repos did strategy X bring?" has a
  single, unambiguous answer.
- **No fairness bias.** A repo found by both LABEL (precise) and
  MENTION (noisy) would otherwise be double-counted, inflating
  MENTION's apparent yield.
- **Simpler dashboard.** No Venn diagrams needed; per-strategy
  numbers sum to the total.

**Cost:** the order of strategies matters. LABEL going first
"steals" repos from CONNEXTRA. Comparing strategies across
experiments that use a different ordering is invalid.

In practice we never change the ordering. It's documented as a
constant in the code and noted in the run stats.

## 7. Why Streamlit for the dashboard

**Alternatives:** Flask / FastAPI + React, Jupyter notebooks, Dash,
plain HTML reports.

**Why Streamlit:**

- **Speed of development.** Building 10 analysis pages, a launch
  form, and a chat interface in vanilla Flask + a frontend
  framework would have taken weeks. Streamlit took days.
- **No frontend skills required.** Pure Python.
- **Easy to deploy / share.** Single command (`streamlit run`),
  works locally, can be exposed over a tunnel.
- **Built-in caching, sessions, file widgets.** All the boilerplate
  is already there.

**Cost:**

- Limited control over UX. The "everything reruns on every
  interaction" model is sometimes awkward.
- Live progress polling is hacky (`time.sleep(2) + st.rerun()`
  pattern).
- Multi-user is not really supported.

For a research / internal tool with one user at a time, the trade is
strongly in Streamlit's favor.

## 8. Why a per-experiment folder structure (and `EXPERIMENT_ID`)

**Alternative considered:** one shared folder with a single
"current state". Each new run overwrites the previous.

**Why isolated experiments:**

- **Reproducibility.** Every experiment carries its own
  `config.json`. Anyone (including future-you) can know exactly
  what parameters produced a given dataset.
- **Ablation.** Running the same prompt change on 4 different
  configs in parallel (or in queue) without contamination is the
  default workflow, not a hack.
- **Crash isolation.** A bug in phase 5 doesn't corrupt a
  previously-completed phase 4 in a different experiment.

**Cost:**

- Disk usage. Five experiments at 100 repos each ≈ several GB
  (most of it is phase 5 source files). Disk space planning becomes
  real.
- The `EXPERIMENT_ID` resolution cascade (env var → file → default)
  is more complex than "always use the current folder".

The cost is well worth the gains.

## 9. Why CoT + fallback (two attempts) in LLM validation

**Alternative considered:** single attempt, either CoT only or
JSON-only. Discard issues where the model fails.

**Why two attempts:**

- Single-attempt CoT: ~60% success rate. The ~40% failures
  (truncated, malformed JSON, schema-invalid) would all be lost.
- Single-attempt JSON-only: faster but no reasoning, lower quality
  for borderline cases.

**The two-attempt strategy** runs CoT first (best quality), and
falls back to JSON-only on failure. The fallback's confidence is
capped at "medium" so high-quality and salvaged records are
distinguishable in the dataset.

The result: only ~0.08% of issues fail both attempts (1 out of
1177 in the benchmark), vs ~40% if we only used CoT.

## 10. Why JSON files and not a database

**Alternatives considered:** SQLite, DuckDB, MongoDB.

**Why JSON:**

- **Human-readable.** Debugging the pipeline often means opening a
  file and looking. With SQLite you need a viewer.
- **Diffable.** `git diff` on a JSON file is useful; on a SQLite
  binary it's not.
- **Trivial to share.** Tar up the experiment folder, send to a
  colleague. With SQLite you need to send the DB and document the
  schema.
- **Pandas-compatible.** `pd.read_json(..., lines=True)` is one
  line away from any analysis.

**Cost:**

- No query language. Filtering 100K records means loading them all
  in memory.
- No type safety. A schema bug in phase 4 produces a malformed
  JSON file rather than a CHECK constraint failure.

For our volume (max ~10K issues per experiment), the speed of
loading the whole file in memory is fine. If the project ever
scales to millions of issues, SQLite/DuckDB would make more sense.

## 11. Why pure file-based coordination between dashboard and pipeline

**Alternative considered:** message broker (Redis), socket
communication, in-process imports.

**Why files:**

- **Crash isolation.** Dashboard dies → pipeline keeps running.
  Pipeline dies → dashboard keeps showing the last known state.
- **No long-running daemon.** Nothing to install, nothing to
  configure.
- **Multi-machine works.** Dashboard on laptop, pipeline on a
  workstation, shared folder over the network. Same code.
- **Restart-resistant.** Reboot the dashboard, the running
  pipeline keeps writing its marker files. Reopen the dashboard,
  resume from where you left off.

**Cost:**

- Polling, not subscription. The dashboard checks
  `pipeline_state.json` every ~2 seconds.
- File locks are not used; race conditions are avoided by single-
  writer discipline (only the orchestrator writes
  `pipeline_state.json`).

For a one-person research tool, files beat brokers on every axis
that matters.
