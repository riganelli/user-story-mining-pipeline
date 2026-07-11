"""English detection for issue text (used in phase 2).

Detects the language of an issue's title and body using ``langid``
(bundled model, deterministic). Code blocks, inline code, URLs and HTML
tags are stripped before detection because they are language-neutral
noise that skews the result on technical issues.

The policy is conservative: an issue is dropped only when the detector
is confident the text is not English. Short texts and low-confidence
classifications are kept.
"""
from __future__ import annotations

import re
import threading

_CODE_BLOCK  = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_URL         = re.compile(r"https?://\S+")
_HTML        = re.compile(r"<[^>]+>")

# Below this length the cleaned text is too short for reliable detection.
_MIN_CHARS = 20

_identifier = None
_import_error: "Exception | None" = None
_lock = threading.Lock()


def _clean(text: str) -> str:
    if not text:
        return ""
    text = _CODE_BLOCK.sub(" ", text)
    text = _INLINE_CODE.sub(" ", text)
    text = _URL.sub(" ", text)
    text = _HTML.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _get_identifier():
    """Lazily build the langid identifier with normalized probabilities.

    On import failure the error is stored so the caller can report it.
    """
    global _identifier, _import_error
    if _identifier is None and _import_error is None:
        try:
            from langid.langid import LanguageIdentifier, model
            _identifier = LanguageIdentifier.from_modelstring(model, norm_probs=True)
        except Exception as e:
            _import_error = e
    return _identifier


def available() -> bool:
    """Return True if langid is importable and the identifier is ready."""
    return _get_identifier() is not None


def import_error() -> str:
    _get_identifier()
    return str(_import_error) if _import_error else ""


def classify_english(title: str, body: str, min_confidence: float = 0.8) -> dict:
    """Classify the issue text and return ``{keep, is_english, language, confidence}``.

    ``keep`` is False only when the detector reports a non-English
    language with confidence at or above ``min_confidence``. The
    classification is wrapped in a lock because langid is not guaranteed
    to be thread-safe and phase 2 calls it from a thread pool.
    """
    text = _clean(f"{title or ''} {body or ''}")
    if len(text) < _MIN_CHARS:
        return {"keep": True, "is_english": None, "language": None, "confidence": 0.0}

    ident = _get_identifier()
    if ident is None:
        # The caller checks available() at startup; keep as a safe default.
        return {"keep": True, "is_english": None, "language": None, "confidence": 0.0}

    with _lock:
        lang, prob = ident.classify(text)

    prob = round(float(prob), 3)
    is_en = (lang == "en")
    keep = is_en or prob < min_confidence
    return {"keep": keep, "is_english": is_en, "language": lang, "confidence": prob}
