# 06 — Phase 4: AI Validation

> For every issue, ask a local LLM if it really is a user story.
> Chain-of-Thought primary attempt + JSON-only fallback. Strict enum
> validation. Defensive post-processing to absorb LLM variance.

## 1. Goal

Phase 4 takes the issues that survived phase 3 and runs them through
a local LLM (Ollama). The LLM decides:

1. **Is this actually a user story?** (boolean)
2. **What is the Connextra structure?** (`complete` / `partial` / `absent`)
3. **What are the parts?** (role, feature, benefit)
4. **Where in the issue is the user story?** (title / body / both)
5. **Are there other markers?** (Gherkin, Acceptance Criteria, IEEE
   format, ...)
6. **How confident is the model?** (high / medium / low)

Issues classified as `is_user_story: true` (with valid metadata) end
up in `validated_issues.json`. The rest go to `discarded_issues_ai.json`.

In a typical 100-repo run, ~66% of phase 3 issues pass the validation.

## 2. Why local Ollama instead of OpenAI / Anthropic

Three reasons:

1. **Cost.** Validating 1000+ issues against a paid API would cost
   real money per experiment. We need to repeat experiments. Local
   inference has a fixed hardware cost and zero per-run cost.
2. **Reproducibility.** The exact model + prompt + seed combination is
   pinned. Hosted models silently change behind the same API endpoint.
3. **Privacy.** No issue body leaves the developer's machine.

The endpoint is `http://localhost:11434` (Ollama's default). The model
is configured via `MODEL_NAME = "qwen-gpu"` at the top of
`4_us_validation.py`. The "gpu" suffix is just a local Ollama tag; the
underlying weights are Qwen 2.5 7B Instruct.

## 3. The two-attempt strategy: CoT primary, fallback secondary

A single `call_ollama(...)` function (in `llm_validation/ollama_client.py`)
orchestrates two attempts:

```python
def call_ollama(...) -> tuple[dict | None, str]:
    """Two-attempt strategy:
       1) Chain-of-thought + few-shot (maximum accuracy)
       2) JSON-only fallback (recovers broken issues instead of discarding them)

    Returns the path used ("cot"/"fallback"/"failed") for traceability.
    """
    # Attempt 1: chain-of-thought
    result = ollama_call_cot(...)
    if result is not None and validate_ollama_output(result):
        return result, "cot"

    # Attempt 2: JSON-only fallback
    result = ollama_call_fallback(...)
    if result is not None and validate_ollama_output(result):
        # Cap confidence at "medium": fallback didn't see few-shot examples
        if result.get("confidence") == "high":
            result["confidence"] = "medium"
        return result, "fallback"

    return None, "failed"
```

### Attempt 1 — Chain-of-Thought

A long prompt (~250 lines) with:

- A clear problem statement
- A schema specification
- **Four few-shot examples** covering: full Connextra + Gherkin + AC,
  `[US-N]` tag without Connextra, inline Gherkin, long technical issue
  with Connextra hidden under a header.
- Step-by-step instructions: STEP 1 Connextra check → STEP 2 metadata
  check → STEP 3 JSON output.

The CoT response is **not** sent with `format="json"` — that would
suppress the chain-of-thought text. Instead, the model emits free-form
reasoning followed by a fenced ```json``` block, and the parser
extracts the JSON.

### Attempt 2 — JSON-only fallback

If CoT fails (truncated, malformed, schema-invalid), a terser prompt is
used with `format="json"` enforced. No few-shot examples, no
reasoning — just the schema and the issue:

```python
PROMPT_FALLBACK = """Analyze this GitHub issue and return ONLY a JSON object
(no other text). Schema: {...}. CRITICAL: The enum values must be EXACT. ...
TITLE: {title}
BODY: {body}
JSON:"""
```

The fallback recovers ~40% of the issues CoT couldn't handle (~276
out of 475 attempts in the benchmark). Its predictions are less
nuanced, so confidence is **capped at "medium"** to mark them as
lower quality:

```python
if result.get("confidence") == "high":
    result["confidence"] = "medium"
```

This avoids polluting the dataset with overconfident fallback labels.

In the validation stats, the two paths are tracked separately:

```json
"validation_path": {
  "cot_total": 701,
  "fallback_total": 475,
  "failed_total": 1,
  "validated_per_path": { "cot": 503, "fallback": 276 },
  "cot_success_rate_pct": 59.6
}
```

A `_validation_path` field is added to every validated issue so the
dashboard can filter by quality.

## 4. The output schema

The model is asked to emit JSON matching this schema exactly:

```json
{
  "is_user_story": boolean,
  "connextra": "complete" | "partial" | "absent",
  "role": string or null,
  "feature": string or null,
  "benefit": string or null,
  "location": "title" | "body" | "both" | null,
  "metadata": {
    "gherkin": boolean,
    "acceptance_criteria": boolean,
    "other_format": boolean
  },
  "confidence": "high" | "medium" | "low"
}
```

Field meanings:

- `is_user_story` — the final yes/no. If false, the issue goes to
  `discarded_issues_ai.json`.
- `connextra` — `complete` requires all three parts (As a / I want /
  so that); `partial` requires only role+action; `absent` means no
  Connextra pattern.
- `role` / `feature` / `benefit` — the extracted text spans. Used in
  the dashboard for the top-roles and top-verbs charts.
- `location` — where in the issue the user story was found.
- `metadata.gherkin` — Given/When/Then present, in any layout (separate
  lines, single line, comma-separated).
- `metadata.acceptance_criteria` — explicit AC header or testable
  checklist.
- `metadata.other_format` — IEEE, Use Case, Job Story formats.
- `confidence` — strictly `high`/`medium`/`low`.

## 5. Strict enum validation + defensive normalization

LLMs love to "improvise" on enums. Without strict validation, the
output gets polluted with `"media"` (Italian for "medium"),
`"median"`, `"high confidence"`, `"completa"` (Italian for
"complete"), etc.

`validate_ollama_output()` enforces strict enums. But before validation
runs, `normalize_enum_in_place()` translates the most common variants:

```python
CONF_ALIASES = {
    "alta": "high", "high": "high", "high confidence": "high", "elevata": "high",
    "media": "medium", "medio": "medium", "medium": "medium", "mean": "medium",
    "median": "medium", "mid": "medium", "medio-alta": "medium", "medio-bassa": "medium",
    "bassa": "low", "basso": "low", "low": "low", "scarsa": "low",
}

CONNEXTRA_ALIASES = {
    "completo": "complete", "completa": "complete", "complete": "complete", "full": "complete",
    "parziale": "partial", "partial": "partial", "incomplete": "partial",
    "assente": "absent", "absent": "absent", "none": "absent", "no": "absent",
}

LOCATION_ALIASES = {
    "title": "title",
    "body": "body", "corpo": "body",
    "both": "both", "entrambi": "both", "both title and body": "both",
    "title and body": "both", "titolo e corpo": "both",
}
```

If a value is not in the alias table, strict validation rejects the
output and the issue is discarded.

## 6. Defensive post-processing

Two pieces of "LLM variance armor" worth mentioning:

### `complete_schema_if_partial`

When the fallback model emits only the required fields and skips the
optional ones (`role`, `feature`, `benefit`, `location`, `metadata`),
the function fills them with defaults (`null` for strings, `{all: false}`
for metadata) before strict validation runs:

```python
def complete_schema_if_partial(data: dict) -> dict:
    """Fills missing fields with safe defaults before strict validation."""
```

This recovers a class of fallback responses that would otherwise be
discarded for missing fields.

### Defensive guard on `role`-as-list

Some models occasionally emit `role` as a list of strings instead of a
single string:

```json
"role": ["security team member", "admin"]
```

The original normalization code crashed with `AttributeError` when
trying to call `.lower()` on a list. The defensive guard now handles
list/dict/None gracefully:

```python
def normalize_role(text) -> str:
    if isinstance(text, list):
        text = text[0] if text else ""
    if not isinstance(text, str):
        return ""
    # normal normalization continues...
```

This bug was discovered the hard way: before the fix, the crash
prevented the writing of `run_stats.json`, which sent phase 5 into an
infinite polling loop waiting for a file that never appeared.

## 7. Smart body truncation

Issue bodies can be huge (multi-thousand-character technical specs).
The model has a token budget. Naively truncating to N chars often
chops off the Connextra pattern itself, which usually appears at the
**start** of the body.

`smart_truncate_body(body, max_chars)` keeps the head of the body
(where the pattern usually lives) and adds a marker if truncation
happened:

```python
MAX_BODY_CHARS = 2500
MAX_PREDICT = 2500   # output tokens
```

The cutoff was tuned empirically: 2500 chars covers >95% of the issues
fully, and the few that get truncated retain their opening section.

## 8. Statistics and quality signals

`4_ai_validation/run_stats.json` reports:

```json
{
  "_schema_version": 3,
  "model": "qwen-gpu",
  "endpoint": "http://localhost:11434 (Ollama local)",
  "prompt_version": "chain-of-thought+few-shot-v6-replace+smart-truncate+enum-strict+fallback-cap",
  "wall_clock_seconds": 12545.6,
  "totals": {
    "input_issues": 1177,
    "validated_issues": 779,
    "discarded_issues": 397,
    "ollama_errors": 1,
    "errors_by_category": {
      "format_error": 0,
      "timeout": 0,
      "json_decode_error": 226,
      "no_json_found": 0,
      "schema_invalid": 1
    },
    "validation_rate_pct": 66.2
  },
  "connextra_distribution": { "complete": 405, "partial": 315 },
  "confidence_distribution": { "high": 400, "medium": 377, "low": 2 },
  "validation_rates_per_strategy": {
    "LABEL":     { "rate_pct": 60.5 },
    "CONNEXTRA": { "rate_pct": 82.0 },
    "MENTION":   { "rate_pct": 67.2 },
    "TEMPLATE":  { "rate_pct": 61.5 },
    "TAG":       { "rate_pct": 70.1 }
  },
  "top_10_roles": [ {"role": "user", "count": 144}, ... ],
  "top_10_action_verbs": [ {"verb": "see", "count": 30}, ... ],
  "body_length": {
    "validated_avg_chars": 1247.0,
    "discarded_avg_chars": 2092.0
  }
}
```

A few interesting observations from the data:

- **CONNEXTRA strategy yields the highest validation rate (82%).**
  Issues found because their body literally matched "as a ... I want"
  are, unsurprisingly, the ones the LLM most easily confirms.
- **LABEL is the lowest at 60.5%.** Repos slap "user story" labels on
  many issues that don't actually contain a Connextra pattern (bug
  reports, feature requests phrased in conversational form).
- **Discarded issues are on average longer** (2092 vs 1247 chars).
  Long technical issues are noisier and harder to classify.

## 9. Manual validation of the AI output

The statistics above are **self-reported** by the validation phase —
they count how many issues the model labeled as user stories, not how
many of those are *actually* user stories. To measure real accuracy, a
random sample of 100 issues marked as `is_user_story: true` was
manually reviewed. The sample comes from the
`run_statistica_prova_100Repo` experiment, validated with model
`qwen-gpu` (qwen2.5:7b) and prompt version
`chain-of-thought+few-shot-v6-replace+smart-truncate+enum-strict+fallback-cap`.

### Headline numbers

- Sample size: **n = 100**
- Correctly classified: **94**
- False positives: **6**
- **Overall AI accuracy: 94%**
- **Overall false positive rate: 6%**

### Breakdown by validation path × confidence

| Path × confidence | Correct | Wrong | Total | Precision | FP rate |
|-------------------|--------:|------:|------:|----------:|--------:|
| High CoT          |      55 |     4 |    59 |    93.2%  |    6.8% |
| Medium CoT        |       5 |     2 |     7 |    71.4%  |   28.6% |
| Medium fallback   |      34 |     0 |    34 |   100.0%  |    0.0% |
| **Total**         |  **94** | **6** | **100** | **94.0%** | **6.0%** |

Distribution of the 6 false positives: **67% (4/6) from High CoT,
33% (2/6) from Medium CoT, 0% from fallback**.

### Observations

- **Medium CoT is the weak point.** Its precision (71.4%) is far below
  both High CoT (93.2%) and Medium fallback (100%). This is the
  "ambiguous" bucket: the model reasoned through the issue but didn't
  reach enough confidence to be accurate. The sample is small (n=7),
  but the signal is consistently negative. Worth investigating which
  thresholds and criteria push validations into this bucket.

- **High CoT is not perfect.** A 6.8% FP rate is in line with the
  overall rate, which means "reasoning more" is not improving accuracy
  as one would expect. The 4 wrong ones in High CoT deserve a
  qualitative look: are they model hallucinations, validation criteria
  that are too permissive, or genuinely ambiguous cases?

- **Medium fallback is the positive surprise.** Zero errors over 34
  cases is excellent. This is likely the bucket where the model
  rejects conservatively, or where the fallback logic is more
  stringent. Worth understanding *why* it works so well — maybe that
  behavior can be replicated elsewhere.

- **Error distribution.** All 6 false positives sit in the CoT paths
  (High + Medium), none in the fallback. This suggests the issue is
  **not** "too little reasoning" but rather **miscalibrated reasoning**
  in some cases.

### Statistical caveat

With n=100 and 6 errors, the 95% Wilson confidence interval on the
overall FP rate is roughly **2.2% – 12.6%**. The Medium CoT bucket
(n=7) is too small to be statistically solid on its own — the signal
is informative, but the precise number is unreliable. A larger sample
focused on that bucket would be needed to confirm the pattern.

These numbers are specific to a single experiment, model, and prompt
version. Changing any of them (different sample of repos, different
LLM, different prompt) requires re-validating against a fresh
manually-labeled sample.

## 10. Checkpointing

Phase 4 is slow (10+ seconds per issue, hours total). It checkpoints
every 25 issues to `_run_stats.json` so that if the process is killed,
restart resumes from the last checkpoint:

```python
CHECKPOINT_EVERY = 25
OLLAMA_TIMEOUT = 180
```

See `09_pipeline_state.md` for the checkpoint mechanism.

## 11. Configuration

The phase reads several knobs from the top of `4_us_validation.py`,
not from `config.json`:

```python
MODEL_NAME       = "qwen-gpu"
MAX_BODY_CHARS   = 2500
MAX_PREDICT      = 2500   # output tokens
CHECKPOINT_EVERY = 25
OLLAMA_TIMEOUT_SEC = 180
```

Changing the model: edit `MODEL_NAME` and ensure the model is
pulled in Ollama (`ollama pull qwen2.5:7b`, then tag it as `qwen-gpu`).

Changing the prompt: edit `PROMPT_COT` or `PROMPT_FALLBACK` directly
in the script. The `prompt_version` string in the stats output should
be updated to reflect the change, so the dashboard can distinguish
results from different prompt iterations.
