import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from ollama import Client

# Make the pipeline package importable when run directly or via the
# orchestrator.
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))

from config.experiment_paths import paths

# Validation helpers: schema.py (parsing, normalization and validation of
# the model JSON) and ollama_client.py (chain-of-thought and fallback calls).
from llm_validation import schema as _schema
from llm_validation import ollama_client as _llm

from utils.checkpoint import load_resume, load_progress
from utils.atomic_io import dump_json_atomic

# Force UTF-8 output (needed on Windows for non-ASCII characters).
sys.stdout.reconfigure(encoding='utf-8')

# Phase 4 runs after phase 3 in the sequential pipeline; argparse is kept
# only for --help and the optional --workers override.

_parser = argparse.ArgumentParser(description="Phase 4 — AI validation with Ollama")
_parser.add_argument(
    "--workers", type=int, default=None,
    help="Number of parallel Ollama requests for phase 4. Overrides "
         "OLLAMA_NUM_PARALLEL. Default: env OLLAMA_NUM_PARALLEL, else 1.",
)
_args = _parser.parse_args()


# Number of parallel validation workers. Precedence: --workers, then the
# OLLAMA_NUM_PARALLEL env var, then 1 (sequential).
def _resolve_n_workers(cli_workers) -> int:
    if cli_workers is not None:
        return max(1, cli_workers)
    env = os.getenv("OLLAMA_NUM_PARALLEL")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            print(f"   ⚠️  OLLAMA_NUM_PARALLEL='{env}' is not an int — falling back to 1.")
    return 1


N_WORKERS = _resolve_n_workers(_args.workers)

# Ollama runs locally with no API key or rate limit. The 180s timeout
# leaves room for the chain-of-thought without hanging on a stall.
OLLAMA_TIMEOUT_SEC = 180
client = Client(host=os.getenv("OLLAMA_ENDPOINT", "http://localhost:11434"), timeout=OLLAMA_TIMEOUT_SEC)

# Configuration.

INPUT_FILE     = str(paths.step3_with_pr_json)
OUTPUT_FILE    = str(paths.step4_validate_json)
DISCARDED_FILE    = str(paths.step4_discarded_json)
RUN_STATS_FILE = str(paths.step4_stats_json)

MODEL_NAME = "qwen-gpu"

# The body is truncated to keep the prompt within the model's context
# window; smart truncation preserves the user story if it appears past
# the limit.
MAX_BODY_CHARS   = 2500

# Output token budget, sized so the chain-of-thought plus JSON is not
# truncated.
MAX_PREDICT      = 2500   # Output tokens.

CHECKPOINT_EVERY = 25

# Circuit breaker: this many back-to-back Ollama failures almost certainly
# mean the service is down, so the phase aborts with exit 1 instead of
# marking the rest failed. Unprocessed issues stay out of the checkpoint
# and are picked up on resume.
MAX_CONSECUTIVE_OLLAMA_ERRORS = 25

# Final gate: an error rate above this fraction indicates a systematic
# problem and the run is not reported complete. Sparse failures stay well
# below it and are tolerated.
MAX_OLLAMA_ERROR_RATE = 0.30

# With temperature 0 a retry is deterministic, so after the first failure
# a single JSON-only fallback attempt is made instead of repeating.
MAX_RETRIES      = 2


# Chain-of-thought prompt with four few-shot examples covering the main
# cases: full Connextra with Gherkin and acceptance criteria; a [US-XX]
# tag without Connextra; inline Gherkin; and a long technical issue with
# the user story at the start.

PROMPT_TEMPLATE = """You are an expert in Agile requirements engineering. Your task is to determine whether a GitHub issue contains a user story in Connextra format AND to detect related metadata.

CONNEXTRA FORMAT DEFINITION:
- PARTIAL (minimum valid): "As a [role], I want/need/would like/can/should be able to [functionality]"
- COMPLETE: PARTIAL + "so that / in order to / so I can [benefit]"

ACCEPTED ROLE VARIANTS — all of these are valid:
- "As a user", "As an admin", "As the developer", "As a registered user"
- "As a [Company] user", "As a [system] administrator", "As a new user"
- Compound roles: "As a developer writing tests for X" or "As a user managing Y"
- Plural: "As users, we want..." or "As developers, we need..." count as partial Connextra

ACCEPTED ACTION VARIANTS — all of these count as Connextra action verbs:
- "I want", "I need", "I would like", "I'd like", "I wish to"
- "I can", "I should be able to", "I must be able to", "I expect to"
- "we want", "we need", "we would like" (when role is plural)

ACCEPTED LOCATIONS — the pattern can appear anywhere:
- Directly in the title: "As a user I want to reset my password"
- Anywhere in the body: bullet points, numbered lists, tables, plain paragraphs
- Under section headers like "## User Story", "**Story:**", "Story:", "US:", "Feature:"
- Even if split across two adjacent sentences, if role + action are both present

CRITICAL — LONG TECHNICAL ISSUES:
In long issues with code snippets, API sketches, design sections, or detailed acceptance criteria, the Connextra pattern often appears in the FIRST FEW LINES of the body (often right under a "### User Story" header), BEFORE any code block. ALWAYS scan the opening 500-1000 characters of the body very carefully, even if the rest of the body is dominated by Java/Python code, JSON examples, or technical specifications. Do NOT let long code blocks distract you from the opening user story.

NOT SUFFICIENT ALONE — mark is_user_story=false even if present:
- Gherkin (Given/When/Then) without a Connextra pattern
- Acceptance criteria without a Connextra pattern
- A "[US-42]" tag in title without a Connextra pattern in title or body
- A plain feature description ("The system should allow users to...") without "As a... I want..."

IMPORTANT — LEAN TOWARD TRUE for is_user_story:
If you find ANY text that resembles a Connextra pattern, even if imperfect, informal, or fragmented, lean toward marking it as a user story with confidence "medium" or "low". Only mark false if there is clearly NO Connextra structure anywhere in the title or body.

---

METADATA DETECTION (INDEPENDENT from the Connextra check):
The metadata.* flags must be evaluated SEPARATELY from is_user_story. A valid user story can have all metadata flags true, none, or any combination. An issue WITHOUT a Connextra pattern can still have gherkin=true or acceptance_criteria=true.

Set metadata.gherkin = true if the body contains Given/When/Then in ANY of these forms:
- On separate lines (classic Gherkin):
    Given a user is logged in
    When they click logout
    Then they are redirected to home
- On a SINGLE line, comma-separated:
    "Given a user is logged in, When they click logout, Then they are redirected"
- Multiple scenarios stacked, even inline:
    "Given X, When Y, Then Z. Given A, When B, Then C."
- The keywords Given + When + Then appearing in close proximity (within 1-2 sentences) as scenario description, in any case (lowercase, capitalized, mixed)
- Sections labeled "Scenario:", "Feature:", "Background:"
- BDD-style step definitions
- IMPORTANT: line breaks/newlines DO NOT MATTER. If you see Given/When/Then describing a scenario, gherkin=true regardless of formatting.

Set metadata.acceptance_criteria = true if the body contains ANY of:
- Headers like "Acceptance Criteria", "AC:", "Acceptance:", "Acceptance tests"
- Headers like "Definition of Done", "DoD", "Requirements:", "Conditions:"
- Numbered/bulleted lists of testable conditions following such headers
- Checkboxes like "- [ ]" or "- [x]" describing testable requirements
- Explicit "must" / "should" / "shall" statements organized as a checklist

Set metadata.other_format = true if there is a structured requirement format that is NOT Connextra and NOT Gherkin/AC. Examples:
- IEEE-style "Shall" statements as the main format
- Use case format ("Use Case: ...", "Actor: ...", "Steps: ...")
- Job stories ("When [situation], I want to [motivation], so I can [outcome]")

CRITICAL: Do NOT default metadata to false. Scan the body specifically looking for these patterns. If you see "Given/When/Then" anywhere → gherkin=true. If you see "Acceptance Criteria" or a checklist of testable conditions → acceptance_criteria=true.

CRITICAL — EXACT VALUES for the enum fields:
- "confidence" must be EXACTLY one of: "high", "medium", "low". Do NOT use "media", "mean", "median", "elevata", "bassa" or other variants.
- "connextra" must be EXACTLY one of: "complete", "partial", "absent".
- "location" must be EXACTLY one of: "title", "body", "both" or null.

---

INSTRUCTIONS:
STEP 1 — Connextra check: scan title and body, paying SPECIAL ATTENTION to the opening of the body (first 500-1000 chars). Quote the exact text that looks like a Connextra pattern. Identify role, action verb, functionality, and benefit (if present). Note where you found it.
STEP 2 — Metadata check: SEPARATELY scan the body for Gherkin keywords (Given/When/Then in ANY format — separate lines, single line, comma-separated), Acceptance Criteria headers/checklists, and other formats. Quote any matches you find.
STEP 3 — Output the JSON on a new line starting with ```json

The JSON must match this schema exactly:
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

---

FEW-SHOT EXAMPLES:

EXAMPLE 1 — Complete Connextra in body alongside Gherkin and AC:
Title: "Password reset feature"
Body: "As a registered user, I want to reset my password via email so that I can regain access to my account.\\n\\nAcceptance Criteria:\\n- User receives email within 1 minute\\n- Token expires after 24h\\n\\nGiven I am on the login page\\nWhen I click 'Forgot password'\\nThen I receive a reset link"
Reasoning: Connextra found in body: "As a registered user, I want to reset my password via email so that I can regain access to my account." Complete pattern. Metadata scan: body contains "Acceptance Criteria:" header followed by checklist → AC=true. Body contains "Given/When/Then" steps on separate lines → gherkin=true.
```json
{"is_user_story": true, "connextra": "complete", "role": "registered user", "feature": "reset my password via email", "benefit": "regain access to my account", "location": "body", "metadata": {"gherkin": true, "acceptance_criteria": true, "other_format": false}, "confidence": "high"}
```

EXAMPLE 2 — [US] tag + Gherkin but NO Connextra pattern → false (but metadata still true):
Title: "[US-14] Dark mode support"
Body: "The application should support dark mode. Users have been requesting this.\\n\\nGiven the user enables dark mode\\nWhen they open the app\\nThen the interface should be dark"
Reasoning: Title has [US-14] tag but no "As a... I want" structure. Body describes the feature, no Connextra pattern → is_user_story=false. BUT metadata scan: body has clear Given/When/Then → gherkin=true. This shows metadata is INDEPENDENT from Connextra.
```json
{"is_user_story": false, "connextra": "absent", "role": null, "feature": null, "benefit": null, "location": null, "metadata": {"gherkin": true, "acceptance_criteria": false, "other_format": false}, "confidence": "high"}
```

EXAMPLE 3 — Connextra + AC header + Gherkin written INLINE (single line, comma-separated):
Title: "Display references from CVEs inline"
Body: "As a security team member I want to see linked information from a CVE while triaging a matching suggestion, so that I can save clicks checking nist.gov.\\n\\nReferences typically link to upstream issues. Showing them inline saves time.\\n\\n## Acceptance criteria\\n\\nGiven a matching suggestion with references, When I view the suggestion, Then the references are displayed.\\n\\nGiven a marked reference, When I mark it as ignored, Then it will not be displayed in published issues."
Reasoning: Connextra found at start of body. Complete pattern with "so that". Metadata scan: header "## Acceptance criteria" → acceptance_criteria=true. Body contains "Given X, When Y, Then Z" written inline on a single line, comma-separated → this IS Gherkin and gherkin=true. Inline format does NOT change classification: line breaks are irrelevant for Gherkin detection.
```json
{"is_user_story": true, "connextra": "complete", "role": "security team member", "feature": "see linked information from a CVE while triaging a matching suggestion", "benefit": "save clicks checking nist.gov", "location": "body", "metadata": {"gherkin": true, "acceptance_criteria": true, "other_format": false}, "confidence": "high"}
```

EXAMPLE 4 — Long technical issue with Connextra at the START followed by code blocks:
Title: "Implement Judge Assertions for AI Agent based Tests"
Body: "### User Story\\n\\nAs a developer writing tests for processes containing AI agent subprocesses, I want to assert that the agent behavior satisfies outlined expectations using a so-called \\"LLM-as-a-judge\\", so that I can evaluate correctness that cannot be captured by similarity metrics.\\n\\n#### Background\\nIntegration tests for AI agent processes face challenges...\\n\\n#### Acceptance Criteria\\n- [ ] Developer can assert content of process variable\\n- [ ] Developer can configure the Judge\\n\\n### Design\\n```java\\n@Test\\nvoid shouldUseCorrectTools() { ... }\\n```\\n\\n### Technical requirements\\n- Configurable chat model"
Reasoning: Following the CRITICAL rule for long technical issues, I scan the OPENING of the body first. Found under "### User Story" header at the very start: "As a developer writing tests for processes containing AI agent subprocesses, I want to assert that the agent behavior satisfies outlined expectations [...] so that I can evaluate correctness". Compound role accepted. Complete Connextra. The Java code block and technical requirements that follow do NOT distract from this. Metadata scan: "#### Acceptance Criteria" header with "- [ ]" checklist → AC=true. No Given/When/Then anywhere → gherkin=false.
```json
{"is_user_story": true, "connextra": "complete", "role": "developer writing tests for processes containing AI agent subprocesses", "feature": "assert that the agent behavior satisfies outlined expectations using LLM-as-a-judge", "benefit": "evaluate correctness that cannot be captured by similarity metrics", "location": "body", "metadata": {"gherkin": false, "acceptance_criteria": true, "other_format": false}, "confidence": "high"}
```

---

NOW ANALYZE THIS ISSUE:

ISSUE TITLE:
{title}

ISSUE BODY:
{body}

Remember:
1. Scan the OPENING of the body carefully — Connextra is often at the start, even in long technical issues with code.
2. Detect Gherkin in ANY format (separate lines, single line, comma-separated) — line breaks do not matter.
3. Do NOT default metadata to false — actively scan for Gherkin, AC headers, and checklists.
4. Use ONLY the exact enum values: "high"|"medium"|"low" for confidence, "complete"|"partial"|"absent" for connextra.
Think step by step first (Connextra check, then metadata check), then output the JSON block."""


# Fallback prompt: a terse JSON-only version used when the chain-of-thought
# fails, recovering the issue instead of discarding it. It states the exact
# enum values explicitly, since the fallback has no few-shot examples.

PROMPT_FALLBACK = """Analyze this GitHub issue and return ONLY a JSON object (no other text).

Schema:
{"is_user_story": bool, "connextra": "complete"|"partial"|"absent", "role": str|null, "feature": str|null, "benefit": str|null, "location": "title"|"body"|"both"|null, "metadata": {"gherkin": bool, "acceptance_criteria": bool, "other_format": bool}, "confidence": "high"|"medium"|"low"}

CRITICAL: The enum values must be EXACT. For "confidence" use ONLY "high", "medium", or "low" (NOT "media", "mean", "median"). For "connextra" use ONLY "complete", "partial", or "absent". For "location" use ONLY "title", "body", "both", or null.

CRITICAL: Scan the FIRST 1000 characters of the body very carefully — Connextra patterns ("As a... I want...") usually appear at the very start of the body, often right under a "### User Story" header, even when the rest of the body is long technical content (code snippets, API sketches, acceptance criteria, design sections). Do NOT miss the opening user story just because the body has lots of code afterwards.

Rules:
- is_user_story=true if body or title contains "As a [role], I want/need/would like/should be able to [action]" (any variant). Compound roles like "As a developer writing tests for X" are valid.
- connextra="complete" if there is also "so that"/"in order to"/"so I can". Else "partial" if only role+action. Else "absent".
- gherkin=true if body contains Given/When/Then in ANY format: separate lines, single line comma-separated ("Given X, When Y, Then Z"), or close-proximity keywords describing scenarios. Line breaks do not matter.
- acceptance_criteria=true if body has "Acceptance Criteria", "Definition of Done", or a checklist of testable conditions (e.g. "- [ ]").
- other_format=true for IEEE/Use Case/Job Story formats.
- Metadata flags are INDEPENDENT from is_user_story.

TITLE: {title}
BODY: {body}

JSON:"""


print("=" * 70)
print("PHASE 4 — AI VALIDATION OF THE CONNEXTRA PATTERN (local Ollama)")
print("=" * 70)
print(f"  Input:                 {INPUT_FILE}")
print(f"  Output (validated):    {OUTPUT_FILE}")
print(f"  Discarded (AI):        {DISCARDED_FILE}")
print(f"  Model:                 {MODEL_NAME}")
print("  Endpoint:              http://localhost:11434 (local)")
print(f"  Timeout per call:      {OLLAMA_TIMEOUT_SEC}s")
print(f"  Max body chars:        {MAX_BODY_CHARS}")
print(f"  Max predict tokens:    {MAX_PREDICT}")
print(f"  Max retries:           {MAX_RETRIES} (1st CoT, 2nd JSON-only fallback)")
print(f"  Checkpoint every:      {CHECKPOINT_EVERY} issues")
print(f"  Parallel workers:      {N_WORKERS}  (1 = sequential, original behaviour)")
print("  Prompt strategy:       chain-of-thought + few-shot (4 examples) + metadata + long-body hint")
print()


# Ensure Ollama is running, auto-starting it if needed.

def _ollama_is_up() -> bool:
    """Return True if the local Ollama server answers."""
    try:
        client.list()
        return True
    except Exception:
        return False


def _try_start_ollama() -> bool:
    """Attempt to start the Ollama server in the background.

    Runs `ollama serve` detached and polls until the server answers
    (up to ~30s). Returns True if Ollama becomes reachable.
    """
    import subprocess
    import shutil

    if shutil.which("ollama") is None:
        print("   'ollama' command not found in PATH — cannot auto-start.")
        return False

    print("⏳ Ollama not reachable — ing to start it automatically...")
    try:
        # Detached background process that outlives this script; on Windows
        # CREATE_NO_WINDOW avoids opening a console window.
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as start_err:
        print(f"   Could not launch 'ollama serve': {start_err}")
        return False

    # Poll until the server is up (about 30s max).
    for _ in range(30):
        time.sleep(1)
        if _ollama_is_up():
            return True
    return False


if _ollama_is_up():
    print("✅ Ollama running and reachable.")
elif _try_start_ollama():
    print("✅ Ollama started automatically and is now reachable.")
else:
    print("❌ Ollama not reachable and could not be started automatically.")
    print("   Make sure Ollama is installed and start it manually")
    print("   (llama icon in the tray, or run 'ollama serve' in a terminal).")
    sys.exit(1)

print()


# Experiment path setup.

paths.ensure_step_dir(4)
print(f"📁 Current experiment: {paths.experiment_id}")


# Load the issues from phase 3.

if not os.path.exists(INPUT_FILE):
    raise FileNotFoundError(f"File {INPUT_FILE} not found. Run phase 3 first.")


def _load_input() -> list | None:
    """Re-reads the input file. Returns None on an I/O error."""
    try:
        with open(INPUT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"   ⚠️  {INPUT_FILE} not readable: {e}. Retrying on the next poll.")
        return None


issues_list = _load_input()
if issues_list is None:
    issues_list = []
print(f"Loaded {len(issues_list)} issues from {INPUT_FILE}")


# Load existing progress (resume from an interruption).

validated_issues, discarded_issues, processed_issues = load_resume(
    output_file=OUTPUT_FILE,
    discarded_file=DISCARDED_FILE,
    output_label="issues already validated",
    discarded_label="issues already discarded by the AI",
)


# Cross-experiment exclusions.

_exp_config = paths.load_config()
_skip_rejected_from  = _exp_config.get("exclude_rejected_issues_from", []) or []
_skip_validated_from = _exp_config.get("exclude_validated_issues_from", []) or []


def _load_keys_from(exp_id, filename):
    """Loads the 'repo#number' keys from a JSON file of another experiment."""
    other_file = paths.base.parent / exp_id / "4_ai_validation" / filename
    if not other_file.exists():
        print(f"⚠️  Exclusion: {filename} not found for '{exp_id}' — skip.")
        return set()
    try:
        with other_file.open("r", encoding="utf-8") as f:
            data_list = json.load(f)
        return {f"{i['repo']}#{i['number']}" for i in data_list}
    except Exception as e:
        print(f"⚠️  Error loading {filename} from '{exp_id}': {e}")
        return set()


_pre_skip = len(processed_issues)
for _exp_id in _skip_rejected_from:
    _keys = _load_keys_from(_exp_id, "discarded_issues_ai.json")
    processed_issues |= _keys
    print(f"📋 +{len(_keys)} discarded issues to exclude (from {_exp_id})")
for _exp_id in _skip_validated_from:
    _keys = _load_keys_from(_exp_id, "validated_issues.json")
    processed_issues |= _keys
    print(f"📋 +{len(_keys)} validated issues to exclude (from {_exp_id})")

_added_by_exclusions = len(processed_issues) - _pre_skip
if _added_by_exclusions:
    print(f"   Total added by exclusions: +{_added_by_exclusions} keys")

print()


# Thin wrapper over llm_validation: call_ollama injects this script's
# constants (client, model, prompt) into the extracted schema and
# ollama_client helpers. The pure functions are re-exported for the stats.
_extract_json_from_text        = _schema.extract_json_from_text
_normalize_enum_in_place    = _schema.normalize_enum_in_place
_complete_schema_if_partial = _schema.complete_schema_if_partial
validate_ollama_output         = _schema.validate_ollama_output
_normalize_role            = _schema.normalize_role
_extract_action_verb         = _schema.extract_action_verb

def _smart_truncate_body(body: str, max_chars: int = MAX_BODY_CHARS) -> str:
    """Wrapper that injects MAX_BODY_CHARS as the default (original signature preserved)."""
    return _schema.smart_truncate_body(body, max_chars)

# Quality gate: the prompt leans toward is_user_story=true, so an issue
# that is only a change description can come back true with placeholder
# role and benefit. Such records are discarded; both fields must be
# placeholders, so legitimate partial stories are kept.
_PLACEHOLDER_VALUES = {"", "none", "n/a", "unknown", "null"}
_PLACEHOLDER_SUBSTR = ("not specified", "not explicitly", "implied by the changes")

def _is_placeholder(value) -> bool:
    if isinstance(value, list):
        value = " ".join(str(v) for v in value)
    s = (str(value) if value is not None else "").strip().lower()
    return s in _PLACEHOLDER_VALUES or any(p in s for p in _PLACEHOLDER_SUBSTR)


# Anti-hallucination grounding: two paraphrase-tolerant checks on the full
# title+body. First, a Connextra anchor (a role opener plus an action
# marker). Second, token overlap of role and feature against the source
# above a threshold (role binding, feature with a softer threshold, benefit
# measured only). Scores are always stored; records are discarded only when
# grounding_enabled is true in validation_config.
import re as _re_grounding

_ROLE_OPENERS = ("as a ", "as an ", "as the ")
_ACTION_MARKERS = (
    "i want", "i need", "i would like", "i d like", "i wish to",
    "i can", "i should be able", "i must be able", "i expect to",
    "we want", "we need", "we would like", "we should be able",
    "i shall be able", "i shall be",
    "i only want", "i just want",
    "i expect",
    "i sometimes need", "i often need",
    "i would love", "i d love",
    "i d like to be able", "i want to be able",
    "it would be nice if", "it would be nice",
    "it would be great if", "it would be helpful if",
    "i m looking to", "i am looking to",
    "we want to be able", "we d like",
)

# Minimal stopword set, so the overlap measures content words only.
_GROUNDING_STOPWORDS = {
    "a", "an", "the", "i", "to", "so", "that", "as", "of", "and", "or",
    "in", "on", "for", "with", "my", "our", "we", "be", "able", "can",
    "want", "need", "would", "like", "should", "is", "are", "this", "it",
}


def _norm_text(s) -> str:
    """Lowercase, strip non-alphanumerics to spaces, collapse whitespace."""
    s = (s or "")
    if not isinstance(s, str):
        s = str(s)
    s = _re_grounding.sub(r"[^a-z0-9\s]", " ", s.lower())
    return _re_grounding.sub(r"\s+", " ", s).strip()


def _content_tokens(s) -> set:
    """Set of content tokens (normalized, stopwords removed, len > 1)."""
    return {
        t for t in _norm_text(s).split()
        if t not in _GROUNDING_STOPWORDS and len(t) > 1
    }


def _has_connextra_anchor(text_norm: str) -> bool:
    """True if a role opener AND an action marker BOTH appear anywhere in the
    normalized text (sparse, no proximity/order — by design)."""
    has_opener = any(op in text_norm for op in _ROLE_OPENERS)
    has_action = any(act in text_norm for act in _ACTION_MARKERS)
    return has_opener and has_action


def _grounding_overlap(field, source_tokens: set):
    """Fraction of the field's content tokens that appear in the source text.
    Returns None when the field has no content tokens (empty/placeholder) —
    in that case the field is not used to discard."""
    ft = _content_tokens(field)
    if not ft:
        return None
    return len(ft & source_tokens) / len(ft)


def _load_grounding_config():
    """Read validation_config from config.json (grounding disabled by default)."""
    try:
        cfg = paths.load_config()
    except Exception:
        cfg = {}
    vc = (cfg.get("validation_config", {}) or {})
    enabled = bool(vc.get("grounding_enabled", False))
    try:
        thr_role = float(vc.get("grounding_threshold", 0.5))
    except (TypeError, ValueError):
        thr_role = 0.5
    try:
        thr_feat = float(vc.get("feature_grounding_threshold", 0.34))
    except (TypeError, ValueError):
        thr_feat = 0.34
    return enabled, thr_role, thr_feat


GROUNDING_ENABLED, GROUNDING_THRESHOLD_ROLE, GROUNDING_THRESHOLD_FEATURE = _load_grounding_config()


def _compute_grounding(title: str, body: str, ai_output: dict) -> dict:
    """Compute the grounding result for one validated record. Always safe to
    call; never raises. `reason` is None when the record passes."""
    full = f"{title or ''}\n{body or ''}"
    text_norm = _norm_text(full)
    source_tokens = _content_tokens(full)

    anchor = _has_connextra_anchor(text_norm)
    role_score = _grounding_overlap(ai_output.get("role"), source_tokens)
    feat_score = _grounding_overlap(ai_output.get("feature"), source_tokens)
    benefit_score = _grounding_overlap(ai_output.get("benefit"), source_tokens)

    reason = None
    if not anchor:
        reason = "connextra_anchor_missing"
    elif role_score is not None and role_score < GROUNDING_THRESHOLD_ROLE:
        reason = "role_not_grounded"
    elif feat_score is not None and feat_score < GROUNDING_THRESHOLD_FEATURE:
        reason = "feature_not_grounded"

    return {
        "anchor": anchor,
        "role": round(role_score, 3) if role_score is not None else None,
        "feature": round(feat_score, 3) if feat_score is not None else None,
        "benefit": round(benefit_score, 3) if benefit_score is not None else None,
        "passed": reason is None,
        "reason": reason,
    }


def call_ollama(title: str, body: str, errors_acc: dict | None = None) -> tuple[dict | None, str]:
    """Thin wrapper over llm_validation.ollama_client.call_ollama.

    Injects the script's constants (client, MODEL_NAME, MAX_PREDICT,
    MAX_BODY_CHARS, prompt template, errors_by_category, OLLAMA_TIMEOUT_SEC)
    into the module's pure caller.

    Returns:
      (None, "failed")        if both attempts fail
      (dict, "cot")           if the chain-of-thought succeeded
      (dict, "fallback")      if only the fallback succeeded
                              (in this case the confidence is capped to
                              "medium" because the fallback does not see the
                              few-shot examples)
    """
    ai_output, val_path = _llm.call_ollama(
        title, body,
        client=client,
        model_name=MODEL_NAME,
        max_predict=MAX_PREDICT,
        max_body_chars=MAX_BODY_CHARS,
        prompt_template_cot=PROMPT_TEMPLATE,
        prompt_template_fallback=PROMPT_FALLBACK,
        errors_by_category=(errors_acc if errors_acc is not None else errors_by_category),
        timeout_sec=OLLAMA_TIMEOUT_SEC,
    )

    # Override a true verdict when role and benefit are both placeholders,
    # routing the issue to discarded.
    if ai_output is not None and ai_output.get("is_user_story") is True:
        if _is_placeholder(ai_output.get("role")) and _is_placeholder(ai_output.get("benefit")):
            ai_output["is_user_story"] = False
            ai_output["overridden_reason"] = "role_and_benefit_placeholder"

    # Store the grounding scores; discard only when enabled in config.
    if ai_output is not None and ai_output.get("is_user_story") is True:
        grounding = _compute_grounding(title, body, ai_output)
        ai_output["grounding"] = grounding
        if GROUNDING_ENABLED and not grounding["passed"]:
            ai_output["is_user_story"] = False
            ai_output["overridden_reason"] = grounding["reason"]

    return ai_output, val_path



# Atomic save.

def save_progress():
    """Saves both output files (incremental checkpoint).

    Uses atomic writes (write-to-tmp + os.replace) so a SIGKILL by SLURM
    during a json.dump cannot leave a truncated file on disk. On Windows
    the helper falls back to a direct overwrite if os.replace hits a
    PermissionError held by a reader (antivirus, editor).
    """
    dump_json_atomic(validated_issues, OUTPUT_FILE)
    dump_json_atomic(discarded_issues, DISCARDED_FILE)


# Progress file with cumulative diagnostic counters that cannot be rebuilt
# from validated/discarded issues alone (Ollama errors, model time,
# per-path counts, error categories, wall clock). Persisting them keeps the
# final run_stats correct across restarts.
_PROGRESS_FILE = paths.step4_dir / "_progress.json"


def _save_progress_phase4():
    """Persist diagnostic counters to _PROGRESS_FILE.

    Called together with save_progress() at every CHECKPOINT_EVERY tick.
    Reads the module-level globals so the call site stays trivial.
    """
    payload = {
        "ollama_errors":        ollama_errors,
        "_processing_seconds":  _processing_seconds,
        "_validation_path_counts": _validation_path_counts,
        "errors_by_category":      errors_by_category,
        "cumulative_wall_clock_seconds":
            _cumulative_wall_clock_seconds + (time.time() - _global_start),
    }
    try:
        paths.ensure_step_dir(4)
        dump_json_atomic(payload, _PROGRESS_FILE)
    except OSError as e:
        print(f"  ⚠️  Progress save failed: {e} (continuing)")


# Validation of all the issues.

_global_start      = time.time()
ollama_errors      = 0
_processing_seconds = 0.0
# Session-local circuit-breaker counters: they start at 0 each run and are
# not restored, so they describe only the current attempt.
_consecutive_ollama_errors = 0   # Reset after every successful call.
_session_errors            = 0   # Ollama failures in this run, for the rate gate.
# Track the per-chunk processing-time delta to detect Ollama regressions.
_last_checkpoint_processing_seconds = 0.0
# Per-path counters (cot vs fallback) for the final run_stats.
_validation_path_counts = {"cot": 0, "fallback": 0, "failed": 0}

# Diagnostic counters per failure category.
errors_by_category = {
    "format_error":      0,
    "timeout":           0,  # ReadTimeout / ConnectTimeout.
    "json_decode_error": 0,  # JSON extracted but malformed.
    "no_json_found":     0,  # Raw with no JSON block.
    "schema_invalid":    0,  # Valid JSON but missing fields or bad enum.
    "empty_response":    0,  # Empty response.
    "other":             0,
}

# Cumulative wall-clock across all jobs; _global_start measures only the
# current process, so previous-run time is added here.
_cumulative_wall_clock_seconds = 0.0

# Restore the persisted diagnostic counters from a previous run, if any.
_prog4 = load_progress(str(_PROGRESS_FILE))
if _prog4:
    ollama_errors                  = int(_prog4.get("ollama_errors", 0))
    _processing_seconds            = float(_prog4.get("_processing_seconds", 0.0))
    _cumulative_wall_clock_seconds = float(
        _prog4.get("cumulative_wall_clock_seconds", 0.0))
    _last_checkpoint_processing_seconds = _processing_seconds
    # Merge dict counters with the defaults so unknown keys do not crash.
    for k, v in (_prog4.get("_validation_path_counts") or {}).items():
        if k in _validation_path_counts:
            _validation_path_counts[k] = int(v)
    for k, v in (_prog4.get("errors_by_category") or {}).items():
        if k in errors_by_category:
            errors_by_category[k] = int(v)
    print(f"📂 RESUME: restored progress counters "
          f"(ollama_errors={ollama_errors}, "
          f"processing_seconds={_processing_seconds:.1f}, "
          f"cumulative_wall_clock={_cumulative_wall_clock_seconds:.1f}s, "
          f"path_counts={_validation_path_counts})")


def _probe_speed(sample: list, update_state: bool = False):
    """Pre-flight probe: 3 calls to estimate the average time per issue."""
    global ollama_errors, _processing_seconds

    new_sample = [
        i for i in sample
        if f"{i['repo']}#{i['number']}" not in processed_issues
    ]
    if not new_sample:
        return

    print("🔬 Speed probe on 3 issues (pre-flight check)...")
    probe_times = []
    for probe_idx, probe_issue in enumerate(new_sample[:3], 1):
        probe_start = time.time()
        ai_output, val_path = call_ollama(probe_issue.get("title", ""), probe_issue.get("body", ""))
        dt = time.time() - probe_start
        probe_times.append(dt)
        # The probe contributes to _processing_seconds so the average time
        # per issue is not underestimated.
        if update_state:
            _processing_seconds += dt
        print(f"   Probe {probe_idx}/3: {dt:.2f}s ({val_path})")

        if update_state:
            key = f"{probe_issue['repo']}#{probe_issue['number']}"
            if ai_output is None:
                ollama_errors += 1
                _validation_path_counts["failed"] += 1
            else:
                _validation_path_counts[val_path] += 1
                record = dict(probe_issue)
                record["ai_validation"] = ai_output
                # Track the validation path for quality filters in phase 5.
                record["_validation_path"] = val_path
                if ai_output.get("is_user_story") is True:
                    validated_issues.append(record)
                else:
                    discarded_issues.append(record)
                processed_issues.add(key)

    if not probe_times:
        print()
        return

    avg_probe = sum(probe_times) / len(probe_times)
    eta_min = (avg_probe * len(sample)) / 60
    print(f"   Average: {avg_probe:.2f}s/issue → batch ETA: ~{eta_min:.1f} min")

    if avg_probe > 25:
        print()
        print(f"   ⚠️  WARNING: {avg_probe:.1f}s/issue is SLOW.")
        print("   The chain-of-thought + metadata detection increases the tokens.")
        print("   If too slow, lower MAX_PREDICT or switch to the Groq API.")
        print("   Proceeding anyway. To stop: Ctrl+C (checkpoint is protected).")
    print()


def _new_errors() -> dict:
    """A fresh per-thread copy of the error-category counters (all zero).

    Each worker accumulates its failures here; the MAIN thread merges them
    into the shared `errors_by_category` when it collects the result. This
    keeps the worker pure: it never mutates shared state, so no Lock is
    needed (`dict[k] += 1` from many threads would otherwise lose counts).
    """
    return {k: 0 for k in errors_by_category}


def process_one(issue: dict) -> dict:
    """PURE worker: validate ONE issue and RETURN the result.

    Runs in a worker thread. It does NOT touch any shared list/set/counter —
    it only reads the issue and returns everything the main thread needs to
    update the shared state itself. A try/except guarantees that an unexpected
    exception on a single issue can never bring the whole pool down.
    """
    key = f"{issue['repo']}#{issue['number']}"
    local_errors = _new_errors()
    t0 = time.time()
    try:
        ai_output, val_path = call_ollama(
            issue.get("title", ""), issue.get("body", ""), errors_acc=local_errors
        )
    except Exception as e:
        ai_output, val_path = None, "failed"
        local_errors["other"] += 1
        print(f"  ⚠️  process_one crashed on {key}: {type(e).__name__}: {str(e)[:120]}")
    dt = time.time() - t0

    record = None
    if ai_output is not None:
        record = dict(issue)
        record["ai_validation"] = ai_output
        # Track the validation path for quality filters in phase 5.
        record["_validation_path"] = val_path

    return {
        "key":      key,
        "record":   record,
        "val_path": val_path,
        "errors":   local_errors,
        "elapsed":  dt,
    }


def process_batch(issues_input: list) -> int:
    """Validate a list of issues IN PARALLEL, skipping those already done.

    Design (see the phase-4 parallelization plan):
      - workers (`process_one`) are PURE: they compute and return, never write
        shared state -> no threading.Lock is required;
      - the MAIN thread is the only writer: it updates validated_issues /
        discarded_issues / processed_issues / the counters, merges the
        per-thread error dicts, runs the circuit breaker, and writes the
        periodic checkpoint. Because only the main thread mutates the lists,
        `dump_json_atomic` (which json.dumps them) can never iterate a list
        while a worker appends to it.

    With N_WORKERS=1 the behaviour is equivalent to the original sequential
    loop. Returns the number of issues actually processed.
    """
    global ollama_errors, _processing_seconds, _last_checkpoint_processing_seconds
    global _consecutive_ollama_errors, _session_errors

    to_process = [
        i for i in issues_input
        if f"{i['repo']}#{i['number']}" not in processed_issues
    ]

    if not to_process:
        return 0

    total = len(to_process)
    print("=" * 70)
    print(f"BATCH VALIDATION — {total} new issues to process | workers: {N_WORKERS}")
    print("=" * 70)
    print()

    batch_start  = time.time()
    n_processed = 0
    completed   = 0

    # The Ollama client is shared across threads: its httpx connection pool
    # is safe for concurrent requests.
    executor = ThreadPoolExecutor(max_workers=N_WORKERS)
    try:
        futures = [executor.submit(process_one, issue) for issue in to_process]

        # Results arrive in completion order; only the ordering differs from
        # the sequential run, not the contents.
        for fut in as_completed(futures):
            res = fut.result()
            completed += 1

            for _cat, _cnt in res["errors"].items():
                if _cat in errors_by_category and _cnt:
                    errors_by_category[_cat] += _cnt
            # Sum of model latencies across threads (not wall clock), used
            # only for the per-chunk regression hint.
            _processing_seconds += res["elapsed"]

            if res["record"] is None:
                ollama_errors += 1
                _session_errors += 1
                _consecutive_ollama_errors += 1
                _validation_path_counts["failed"] += 1
                print(f"[{completed}/{total}] {res['key']} → ⚠️  Ollama/JSON error "
                      f"(CoT + fallback both failed). Skipping.")

                # Circuit breaker in the main thread, so sys.exit(1) sets the
                # real exit code and phase 4 is not marked done. Consecutive
                # means consecutive in completion order.
                if _consecutive_ollama_errors >= MAX_CONSECUTIVE_OLLAMA_ERRORS:
                    print()
                    print("=" * 70)
                    print(f"❌ {_consecutive_ollama_errors} CONSECUTIVE Ollama errors "
                          f"— Ollama looks DOWN. Saving progress and aborting phase 4.")
                    print("   Restart Ollama, then re-run: the validated/discarded "
                          "issues are kept and the rest is retried.")
                    print("=" * 70)
                    save_progress()
                    _save_progress_phase4()
                    # Cancel not-yet-started tasks so we exit promptly.
                    executor.shutdown(wait=False, cancel_futures=True)
                    sys.exit(1)
            else:
                # Success: reset the consecutive-failure count.
                _consecutive_ollama_errors = 0
                val_path = res["val_path"]
                _validation_path_counts[val_path] += 1
                record = res["record"]
                ai_output = record["ai_validation"]

                if ai_output.get("is_user_story") is True:
                    validated_issues.append(record)
                    print(f"[{completed}/{total}] {res['key']} → ✅ USER STORY "
                          f"({ai_output.get('connextra', '?')}) | "
                          f"conf: {ai_output.get('confidence', '?')} | "
                          f"loc: {ai_output.get('location', '?')} | path: {val_path}")
                else:
                    discarded_issues.append(record)
                    print(f"[{completed}/{total}] {res['key']} → ❌ DISCARDED | "
                          f"conf: {ai_output.get('confidence', '?')} | path: {val_path}")

                processed_issues.add(res["key"])
                n_processed += 1

            # Periodic checkpoint; only the main thread writes, so no lock
            # is needed.
            if completed % CHECKPOINT_EVERY == 0:
                save_progress()
                _save_progress_phase4()
                elapsed = time.time() - batch_start
                rate    = completed / elapsed if elapsed > 0 else 0
                eta_sec = (total - completed) / rate if rate > 0 else 0
                delta_chunk = (_processing_seconds - _last_checkpoint_processing_seconds) / CHECKPOINT_EVERY
                _last_checkpoint_processing_seconds = _processing_seconds
                print()
                print(f"  📊 Checkpoint [{completed}/{total}] | "
                      f"Validated: {len(validated_issues)} | "
                      f"Discarded: {len(discarded_issues)} | "
                      f"Errors: {ollama_errors} | "
                      f"{elapsed:.0f}s (ETA ~{eta_sec/60:.1f}min) | "
                      f"⏱️  Σmodel-time/{CHECKPOINT_EVERY} over {N_WORKERS}thr: {delta_chunk:.1f}s | "
                      f"path: cot={_validation_path_counts['cot']} fb={_validation_path_counts['fallback']} fail={_validation_path_counts['failed']}")
                print()
    finally:
        # Release the pool without waiting on stragglers.
        executor.shutdown(wait=False, cancel_futures=True)

    save_progress()
    _save_progress_phase4()
    return n_processed


# -------------------------------------------------------------------------
# Execution: single sequential pass.
# -------------------------------------------------------------------------

issues_to_process = [
    i for i in issues_list
    if f"{i['repo']}#{i['number']}" not in processed_issues
]

if len(issues_to_process) > 10:
    _probe_speed(issues_to_process, update_state=True)

_session_processed = process_batch(issues_list)


save_progress()
_save_progress_phase4()

# Final completeness gate: sparse failures are tolerated, but an error rate
# too high for this run means a systematic problem, so exit 1 stops the
# orchestrator. The progress file already holds the diagnostic counters.
_session_attempted = _session_processed + _session_errors
if _session_attempted > 0:
    _err_rate = _session_errors / _session_attempted
    if _err_rate > MAX_OLLAMA_ERROR_RATE:
        print()
        print("=" * 70)
        print(f"❌ PHASE 4 error rate too high: {_session_errors}/{_session_attempted} "
              f"= {_err_rate:.0%} (threshold {MAX_OLLAMA_ERROR_RATE:.0%}). "
              f"The validation set is NOT trustworthy.")
        print("   Check the model name / prompt / Ollama health, then re-run.")
        print("=" * 70)
        sys.exit(1)


# Statistics.

# Cumulative wall-clock across all jobs; the persisted progress adds the
# time from previous runs to this process's clock.
elapsed_total = (time.time() - _global_start) + _cumulative_wall_clock_seconds

connextra_complete = sum(
    1 for r in validated_issues
    if r.get("ai_validation", {}).get("connextra") == "complete"
)
connextra_partial = sum(
    1 for r in validated_issues
    if r.get("ai_validation", {}).get("connextra") == "partial"
)

conf_high  = sum(1 for r in validated_issues if r.get("ai_validation", {}).get("confidence") == "high")
conf_medium = sum(1 for r in validated_issues if r.get("ai_validation", {}).get("confidence") == "medium")
conf_low = sum(1 for r in validated_issues if r.get("ai_validation", {}).get("confidence") == "low")

with_gherkin = sum(
    1 for r in validated_issues
    if r.get("ai_validation", {}).get("metadata", {}).get("gherkin") is True
)
with_acceptance_criteria = sum(
    1 for r in validated_issues
    if r.get("ai_validation", {}).get("metadata", {}).get("acceptance_criteria") is True
)

validated_per_strategy = {}
for r in validated_issues:
    s = r.get("strategy", "UNKNOWN")
    validated_per_strategy[s] = validated_per_strategy.get(s, 0) + 1

discarded_per_strategy = {}
for r in discarded_issues:
    s = r.get("strategy", "UNKNOWN")
    discarded_per_strategy[s] = discarded_per_strategy.get(s, 0) + 1

# Count validated and discarded issues per validation path.
validated_per_path = {"cot": 0, "fallback": 0}
discarded_per_path = {"cot": 0, "fallback": 0}
for r in validated_issues:
    p = r.get("_validation_path", "cot")
    if p in validated_per_path:
        validated_per_path[p] += 1
for r in discarded_issues:
    p = r.get("_validation_path", "cot")
    if p in discarded_per_path:
        discarded_per_path[p] += 1


# Extra statistics. _normalize_role and _extract_action_verb come from
# llm_validation.schema.


# The statistics computation is wrapped in try/except: on any failure a
# minimal run_stats.json is written and the phase still completes, so
# phase 5 does not hang.

# Emergency stats, written first so run_stats.json exists even if the
# detailed computation below fails.
_emergency_run_stats = {
    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    "model": MODEL_NAME,
    "_schema_version": 3,
    "_phase": 4,
    "_post_processing_status": "in_progress",
    "totals": {
        "input_issues":    len(issues_list),
        "validated_issues": len(validated_issues),
        "discarded_issues": len(discarded_issues),
        "ollama_errors":  ollama_errors,
    },
}
try:
    dump_json_atomic(_emergency_run_stats, RUN_STATS_FILE)
except Exception as _emerg_err:
    print(f"⚠️  Could not write emergency run_stats: {_emerg_err}")

try:
    role_counts = {}
    for r in validated_issues:
        role = r.get("ai_validation", {}).get("role", "")
        role_norm = _normalize_role(role)
        if role_norm:
            role_counts[role_norm] = role_counts.get(role_norm, 0) + 1
    top_10_roles = [
        {"role": r, "count": c}
        for r, c in sorted(role_counts.items(), key=lambda kv: -kv[1])[:10]
    ]

    verb_counts = {}
    for r in validated_issues:
        feature = r.get("ai_validation", {}).get("feature", "")
        verb = _extract_action_verb(feature)
        if verb:
            verb_counts[verb] = verb_counts.get(verb, 0) + 1
    top_10_action_verbs = [
        {"verb": v, "count": c}
        for v, c in sorted(verb_counts.items(), key=lambda kv: -kv[1])[:10]
    ]

    location_counts = {"title": 0, "body": 0, "both": 0, "unspecified": 0}
    for r in validated_issues:
        loc_raw = r.get("ai_validation", {}).get("location")
        loc = (loc_raw or "").lower().strip() if isinstance(loc_raw, str) else ""
        # The "both" key matches the schema enum.
        if loc in location_counts:
            location_counts[loc] += 1
        else:
            location_counts["unspecified"] += 1


    def _avg_body_len(issue_list: list) -> float:
        lengths = [len(i.get("body", "") or "") for i in issue_list]
        return round(sum(lengths) / len(lengths), 0) if lengths else 0


    body_length = {
        "validated_avg_chars": _avg_body_len(validated_issues),
        "discarded_avg_chars": _avg_body_len(discarded_issues),
    }

    validation_rates_per_strategy = {}
    all_strat = set(validated_per_strategy.keys()) | set(discarded_per_strategy.keys())
    for s in all_strat:
        v   = validated_per_strategy.get(s, 0)
        sc  = discarded_per_strategy.get(s, 0)
        tot = v + sc
        validation_rates_per_strategy[s] = {
            "validate": v,
            "discarded": sc,
            "total":   tot,
            "rate_pct": round(v * 100 / max(tot, 1), 1) if tot else 0.0,
        }

    run_stats = {
        "timestamp":  time.strftime("%Y-%m-%d %H:%M:%S"),
        "model":    MODEL_NAME,
        "endpoint":   "http://localhost:11434 (Ollama local)",
        "prompt_version": "chain-of-thought+few-shot-v6-replace+smart-truncate+enum-strict+fallback-cap",
        "_schema_version": 3,
        "_phase": 4,
        "wall_clock_seconds":         round(elapsed_total, 1),
        "total_time_seconds":       round(elapsed_total, 1),
        # Sum of per-call model latencies across threads; with N_WORKERS>1
        # this can exceed the wall clock and is not a wall-clock figure.
        "model_busy_time_all_threads_seconds": round(_processing_seconds, 1),
        "n_workers": N_WORKERS,
        # Wall-clock time per processed issue: elapsed over issues classified.
        "avg_time_per_issue_seconds": round(
            elapsed_total / max(len(validated_issues) + len(discarded_issues), 1), 2
        ),
        "configuration": {
            "max_body_chars":   MAX_BODY_CHARS,
            "max_predict":      MAX_PREDICT,
            "max_retries":      MAX_RETRIES,
            "checkpoint_every": CHECKPOINT_EVERY,
            "ollama_timeout":   OLLAMA_TIMEOUT_SEC,
        },
        "totals": {
            "input_issues":    len(issues_list),
            "validated_issues": len(validated_issues),
            "discarded_issues": len(discarded_issues),
            "ollama_errors":  ollama_errors,
            "errors_by_category": errors_by_category,
            "validation_rate_pct": round(
                len(validated_issues) * 100 / max(len(issues_list), 1), 1
            ),
        },
        # Validation-path breakdown (cot vs fallback), for dataset quality.
        "validation_path": {
            "cot_total":        _validation_path_counts["cot"],
            "fallback_total":   _validation_path_counts["fallback"],
            "failed_total":     _validation_path_counts["failed"],
            "validated_per_path": validated_per_path,
            "discarded_per_path": discarded_per_path,
            "cot_success_rate_pct": round(
                _validation_path_counts["cot"] * 100 / max(
                    _validation_path_counts["cot"] + _validation_path_counts["fallback"] + _validation_path_counts["failed"], 1
                ), 1
            ),
        },
        "connextra_distribution": {
            "complete": connextra_complete,
            "partial": connextra_partial,
        },
        "confidence_distribution": {
            "high":  conf_high,
            "medium": conf_medium,
            "low": conf_low,
        },
        "location_distribution": location_counts,
        "metadata_in_validated": {
            "with_gherkin":             with_gherkin,
            "with_acceptance_criteria": with_acceptance_criteria,
        },
        "validated_per_discovery_strategy": validated_per_strategy,
        "discarded_per_discovery_strategy": discarded_per_strategy,
        "validation_rates_per_strategy":  validation_rates_per_strategy,
        "top_10_roles":        top_10_roles,
        "top_10_action_verbs": top_10_action_verbs,
        "body_length":        body_length,
    }

    dump_json_atomic(run_stats, RUN_STATS_FILE)
except Exception as _stats_err:
    # The detailed statistics failed: write a minimal run_stats and proceed,
    # so phase 5 knows phase 4 has finished.
    print()
    print("⚠️  ERROR during phase 4 post-processing statistics computation:")
    print(f"   {type(_stats_err).__name__}: {_stats_err}")
    traceback.print_exc()
    print("   → Writing a minimal run_stats.json and proceeding. The validated")
    print("   → issues are already saved in validated_issues.json.")
    _fallback_run_stats = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": MODEL_NAME,
        "_schema_version": 3,
        "_phase": 4,
        "_post_processing_status": "failed",
        "_post_processing_error": f"{type(_stats_err).__name__}: {_stats_err}",
        "wall_clock_seconds": round(elapsed_total, 1),
        "total_time_seconds": round(elapsed_total, 1),
        "totals": {
            "input_issues":    len(issues_list),
            "validated_issues": len(validated_issues),
            "discarded_issues": len(discarded_issues),
            "ollama_errors":  ollama_errors,
            "errors_by_category": errors_by_category,
            "validation_rate_pct": round(
                len(validated_issues) * 100 / max(len(issues_list), 1), 1
            ),
        },
    }
    try:
        dump_json_atomic(_fallback_run_stats, RUN_STATS_FILE)
    except Exception as _final_err:
        print(f"   ⚠️  Could not write fallback run_stats: {_final_err}")
    # Safe defaults for the variables used by the final summary.
    run_stats = _fallback_run_stats



# Final summary, wrapped so a crash here cannot block mark_phase_done(4).
try:
    print()
    print("=" * 70)
    print("FINAL SUMMARY — PHASE 4 (AI validation with Ollama)")
    print("=" * 70)
    print("  Prompt:                    chain-of-thought + few-shot (4 examples) + metadata + long-body")
    print(f"  Total time (wall):         {elapsed_total:.1f}s ({elapsed_total/60:.1f} min)")
    print(f"  Model-busy time (Σ {N_WORKERS} thr): {_processing_seconds:.1f}s ({_processing_seconds/60:.1f} min)")
    print(f"  Input issues:              {len(issues_list)}")
    print(f"  Validated issues:          {len(validated_issues)}")
    print(f"  Discarded issues:          {len(discarded_issues)}")
    print(f"  Ollama errors:             {ollama_errors}")
    if any(v > 0 for v in errors_by_category.values()):
        print("    By category:")
        for cat, v in errors_by_category.items():
            if v > 0:
                print(f"      {cat:20s} {v}")
    print(f"  Validation rate:           {run_stats['totals']['validation_rate_pct']}%")
    print()
    # Report the cot vs fallback breakdown.
    print("  Validation path:")
    print(f"    CoT (primary):           {_validation_path_counts['cot']}")
    print(f"    Fallback (capped to average): {_validation_path_counts['fallback']}")
    print(f"    Failed (both KO):        {_validation_path_counts['failed']}")
    _tot_path = sum(_validation_path_counts.values())
    if _tot_path > 0:
        print(f"    CoT success rate:        {_validation_path_counts['cot']*100/_tot_path:.1f}%")
    print()
    print("  Connextra distribution:")
    print(f"    Complete:                {connextra_complete}")
    print(f"    Partial:                 {connextra_partial}")
    print()
    print("  Confidence distribution:")
    print(f"    High:                    {conf_high}")
    print(f"    Medium:                  {conf_medium}")
    print(f"    Low:                     {conf_low}")
    print()
    print("  Metadata in the validated ones:")
    print(f"    With Gherkin:            {with_gherkin}")
    print(f"    With Acceptance Criteria: {with_acceptance_criteria}")
    print()
    print("  Validated per discovery strategy:")
    for s, count in sorted(validated_per_strategy.items(), key=lambda x: -x[1]):
        print(f"    {s}: {count}")
    print()
    print(f"  Output (validated):        {OUTPUT_FILE}")
    print(f"  Discarded:                 {DISCARDED_FILE}")
    print(f"  Run stats:                 {RUN_STATS_FILE}")
    print("=" * 70)
except Exception as _summary_err:
    print(f"⚠️  Summary print error (non-blocking): {type(_summary_err).__name__}: {_summary_err}")


print("🏁 Phase 4 complete.")