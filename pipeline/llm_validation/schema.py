"""Parsing, normalization and validation of the JSON produced by the
model in phase 4.

All functions here are pure: they do not call the model, do not read
files and have no side effects on global state, so they can be tested
in isolation.

Functions:
  extract_json_from_text: JSON extraction from a chain-of-thought reply.
  normalize_enum_in_place: mapping of common enum variants to the schema.
  complete_schema_if_partial: completion of partial fallback replies.
  validate_ollama_output: strict schema and enum validation.
  smart_truncate_body: body truncation that preserves user-story content.
  normalize_role, extract_action_verb: helpers for phase 4 statistics.
"""
from __future__ import annotations

import re


# Normalization tables: map values the model sometimes produces
# (language variants, synonyms) onto the enums expected by the schema.
# Values not listed here are rejected by the strict validation.

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
    "title": "title", "title": "title",
    "body": "body", "corpo": "body",
    "both": "both", "entrambi": "both", "both title and body": "both",
    "title and body": "both", "titolo e corpo": "both",
}


def extract_json_from_text(raw: str) -> str | None:
    """Extract the JSON string from the model's response.

    The model emits its reasoning before the JSON, so the raw text
    cannot be parsed directly. A ```json fenced block is tried first,
    then a generic fenced block, then the span from the first ``{`` to
    the last ``}``. The regexes are greedy so JSON containing nested
    objects is captured whole. Returns the JSON string, or None.
    """
    match = re.search(r"```json\s*\n?(\{.*\})\s*\n?```", raw, re.DOTALL)
    if match:
        return match.group(1).strip()

    match = re.search(r"```\s*\n?(\{.*\})\s*\n?```", raw, re.DOTALL)
    if match:
        return match.group(1).strip()

    start = raw.find("{")
    end = raw.rfind("}") + 1
    if start != -1 and end > start:
        return raw[start:end].strip()

    return None


def normalize_enum_in_place(data: dict) -> dict:
    """Normalize the enum values before the strict validation.

    The model sometimes returns values that are semantically correct
    but off-schema; the alias tables map the most common variants onto
    the expected enums. Modifies ``data`` in place and returns it.
    """
    if not isinstance(data, dict):
        return data

    conf = data.get("confidence")
    if isinstance(conf, str):
        norm = CONF_ALIASES.get(conf.lower().strip())
        if norm:
            data["confidence"] = norm

    cnx = data.get("connextra")
    if isinstance(cnx, str):
        norm = CONNEXTRA_ALIASES.get(cnx.lower().strip())
        if norm:
            data["connextra"] = norm

    ub = data.get("location")
    if isinstance(ub, str):
        norm = LOCATION_ALIASES.get(ub.lower().strip())
        if norm:
            data["location"] = norm

    return data


def complete_schema_if_partial(data: dict) -> dict:
    """Fill the missing fields of a partial fallback response.

    ``format="json"`` guarantees syntactically valid JSON but not the
    full schema: the model may omit optional fields. They are filled
    with neutral defaults so the record is not discarded outright; the
    strict validation still runs afterwards.
    """
    if not isinstance(data, dict):
        return data

    for key in ("role", "feature", "benefit", "location"):
        data.setdefault(key, None)

    data.setdefault("confidence", "low")

    # Rebuild the metadata dict with neutral defaults if missing.
    md = data.get("metadata")
    if not isinstance(md, dict):
        md = {}
    md.setdefault("gherkin", False)
    md.setdefault("acceptance_criteria", False)
    md.setdefault("other_format", False)
    data["metadata"] = md

    return data


def validate_ollama_output(data: dict) -> bool:
    """Check that the JSON has all the expected fields and valid enums.

    ``normalize_enum_in_place`` must run before this function: any
    off-enum value reaching this point is discarded.
    """
    if not isinstance(data, dict):
        return False
    required = {"is_user_story", "connextra", "role", "feature",
                "benefit", "location", "metadata", "confidence"}
    if not required.issubset(data.keys()):
        return False
    if not isinstance(data.get("metadata"), dict):
        return False
    if not isinstance(data.get("is_user_story"), bool):
        return False
    if data.get("connextra") not in ("complete", "partial", "absent"):
        return False
    if data.get("confidence") not in ("high", "medium", "low"):
        return False
    if data.get("location") not in (None, "title", "body", "both"):
        return False
    return True


def smart_truncate_body(body: str, max_chars: int) -> str:
    """Body truncation that tries to preserve structure and relevant context.

    A body within the limit is returned whole. Otherwise the cut is
    moved back to the nearest paragraph, sentence or line boundary
    within 500 characters. If user-story or Gherkin patterns appear
    beyond the cut, a short excerpt around the first match is appended
    so the model does not lose the relevant content.
    """
    if not body:
        return ""
    if len(body) <= max_chars:
        return body

    window = body[max(0, max_chars - 500):max_chars]
    cut = max_chars
    # Boundary preference: paragraph, end of sentence, single newline.
    for pattern, offset in (("\n\n", 2), (". ", 2), (".\n", 2), ("\n", 1)):
        idx = window.rfind(pattern)
        if idx != -1:
            cut = max_chars - 500 + idx + offset if max_chars > 500 else idx + offset
            break

    head = body[:cut].rstrip()

    # Look for relevant patterns beyond the cut.
    tail = body[cut:]
    relevant_patterns = [
        r"###?\s*User\s+Stor",
        r"\bAs\s+a[n]?\s+\w",
        r"\bGiven\b.*\bWhen\b.*\bThen\b",
        r"##?\s*Acceptance\s+[Cc]riteria",
        r"\bDefinition\s+of\s+Done\b",
    ]
    relevant_snippet = None
    for pat in relevant_patterns:
        m = re.search(pat, tail, re.IGNORECASE)
        if m:
            start = max(0, m.start() - 100)
            relevant_snippet = tail[start:start + 800]
            break

    if relevant_snippet:
        return (
            head
            + "\n\n[... body truncated — RELEVANT EXCERPT FROM BELOW ...]\n"
            + relevant_snippet.strip()
            + "\n[... end of excerpt ...]"
        )
    else:
        return head + f"\n[... body truncated at ~{max_chars} chars ...]"


# Post-processing normalizations used by the phase 4 statistics.

def normalize_role(text) -> str:
    """Normalize a role to a lowercase string without articles.

    Defensive against model variance: a list yields its first element,
    any other non-string value is coerced to a string, and falsy input
    yields an empty string.
    """
    if not text:
        return ""
    if isinstance(text, list):
        if not text:
            return ""
        text = text[0] if isinstance(text[0], str) else str(text[0])
    elif not isinstance(text, str):
        text = str(text)
    t = text.lower().strip()
    t = re.sub(r"^(a|an|the)\s+", "", t)
    t = re.sub(r"[^a-z\s]+$", "", t).strip()
    return t


def extract_action_verb(text) -> str:
    """Extract the main verb of a user-story action.

    Applies the same defensive input handling as ``normalize_role``.
    """
    if not text:
        return ""
    if isinstance(text, list):
        if not text:
            return ""
        text = text[0] if isinstance(text[0], str) else str(text[0])
    elif not isinstance(text, str):
        text = str(text)
    t = text.lower().strip()
    t = re.sub(r"^to\s+", "", t)
    if t.startswith("be able to"):
        return "be able to"
    words = t.split()
    return words[0] if words else ""