"""Ollama calls for the LLM validation of user stories (phase 4).

Functions:
  ollama_call_cot: primary attempt with a chain-of-thought prompt.
  ollama_call_fallback: JSON-only attempt used when the first fails.
  call_ollama: orchestrates the two attempts.

All external dependencies (client, model name, prompt templates,
counters) are explicit parameters, so the caller stays authoritative on
the configuration and the functions are easy to test.

Prompt placeholders are filled with ``str.replace`` rather than
``str.format`` because issue bodies often contain braces (JSON, Java
generics) that would break ``format``.
"""
from __future__ import annotations

import json

import httpx

from llm_validation.schema import (
    extract_json_from_text,
    normalize_enum_in_place,
    complete_schema_if_partial,
    validate_ollama_output,
    smart_truncate_body,
)


def ollama_call_cot(
    title: str,
    body_truncated: str,
    *,
    client,
    model_name: str,
    max_predict: int,
    prompt_template: str,
    errors_by_category: dict,
    timeout_sec: int,
) -> dict | None:
    """Primary attempt: chain-of-thought prompt with few-shot examples.

    Returns the parsed dict, or None if no valid JSON can be extracted.
    A single call is made: with temperature 0 a retry would be
    deterministic. ``errors_by_category`` is updated in place.
    """
    prompt = (prompt_template
              .replace("{title}", title or "(no title)")
              .replace("{body}", body_truncated or "(no body)"))
    try:
        response = client.generate(
            model=model_name,
            prompt=prompt,
            # format="json" is intentionally omitted: it would suppress
            # the chain-of-thought.
            options={
                "temperature": 0.0,
                "num_predict": max_predict,
            },
        )
        raw = (response.get("response") or "").strip()
        if not raw:
            errors_by_category["empty_response"] += 1
            return None
        json_str = extract_json_from_text(raw)
        if not json_str:
            errors_by_category["no_json_found"] += 1
            print(f"      [CoT] No JSON found. Raw (first 300 chars): {raw[:300]}")
            return None
        try:
            return json.loads(json_str)
        except json.JSONDecodeError as e:
            errors_by_category["json_decode_error"] += 1
            print(f"      [CoT] Malformed JSON: {e}")
            print(f"      Excerpt: {json_str[:300]}")
            return None
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        errors_by_category["timeout"] += 1
        print(f"      [CoT] TIMEOUT after {timeout_sec}s")
        return None
    except Exception as e:
        etype = type(e).__name__
        if etype in ("KeyError", "IndexError"):
            errors_by_category["format_error"] += 1
        else:
            errors_by_category["other"] += 1
        print(f"      [CoT] Error [{etype}]: {str(e)[:150]}")
        return None


def ollama_call_fallback(
    title: str,
    body_truncated: str,
    *,
    client,
    model_name: str,
    prompt_template: str,
    errors_by_category: dict,
    timeout_sec: int,
) -> dict | None:
    """Fallback attempt: terse prompt with ``format="json"``.

    Used when the chain-of-thought attempt fails. It is cheaper and
    behaves differently from the first attempt, so it can recover issues
    that would otherwise be discarded.
    """
    prompt = (prompt_template
              .replace("{title}", title or "(no title)")
              .replace("{body}", body_truncated or "(no body)"))
    try:
        response = client.generate(
            model=model_name,
            prompt=prompt,
            format="json",
            options={
                "temperature": 0.0,
                "num_predict": 600,
            },
        )
        raw = (response.get("response") or "").strip()
        if not raw:
            errors_by_category["empty_response"] += 1
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            json_str = extract_json_from_text(raw)
            if not json_str:
                errors_by_category["no_json_found"] += 1
                return None
            try:
                return json.loads(json_str)
            except json.JSONDecodeError as e:
                errors_by_category["json_decode_error"] += 1
                print(f"      [Fallback] Malformed JSON: {e}")
                return None
    except (httpx.ReadTimeout, httpx.ConnectTimeout):
        errors_by_category["timeout"] += 1
        print(f"      [Fallback] TIMEOUT after {timeout_sec}s")
        return None
    except Exception as e:
        etype = type(e).__name__
        if etype in ("KeyError", "IndexError"):
            errors_by_category["format_error"] += 1
        else:
            errors_by_category["other"] += 1
        print(f"      [Fallback] Error [{etype}]: {str(e)[:150]}")
        return None


def call_ollama(
    title: str,
    body: str,
    *,
    client,
    model_name: str,
    max_predict: int,
    max_body_chars: int,
    prompt_template_cot: str,
    prompt_template_fallback: str,
    errors_by_category: dict,
    timeout_sec: int,
) -> tuple[dict | None, str]:
    """Validate an issue with a two-attempt strategy.

    The chain-of-thought attempt is tried first; if it fails, the
    JSON-only fallback recovers the issue instead of discarding it.

    Returns:
        ``(result, path)`` where ``path`` is ``"cot"``, ``"fallback"``
        or ``"failed"``. The path is recorded in the dataset so records
        can be filtered by validation quality. When the fallback is
        used, confidence is capped at ``"medium"``.
    """
    body_truncated = smart_truncate_body(body or "", max_body_chars)

    result = ollama_call_cot(
        title, body_truncated,
        client=client, model_name=model_name, max_predict=max_predict,
        prompt_template=prompt_template_cot,
        errors_by_category=errors_by_category,
        timeout_sec=timeout_sec,
    )
    if result is not None:
        result = normalize_enum_in_place(result)
        if validate_ollama_output(result):
            return result, "cot"

    print(f"      [Ollama] CoT failed → JSON-only fallback")
    result = ollama_call_fallback(
        title, body_truncated,
        client=client, model_name=model_name,
        prompt_template=prompt_template_fallback,
        errors_by_category=errors_by_category,
        timeout_sec=timeout_sec,
    )
    if result is not None:
        result = complete_schema_if_partial(result)
        result = normalize_enum_in_place(result)
        # The fallback does not see the few-shot examples, so its "high"
        # confidence is not comparable with the CoT one.
        if result.get("confidence") == "high":
            result["confidence"] = "medium"
        if validate_ollama_output(result):
            return result, "fallback"
        else:
            errors_by_category["schema_invalid"] += 1

    return None, "failed"