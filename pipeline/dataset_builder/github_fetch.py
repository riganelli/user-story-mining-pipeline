"""GitHub API retrieval functions for the dataset construction (phase 5).

Pure helpers (primary-PR selection, datetime normalization, extension
checks) plus the HTTP functions that download PR details, files, commits
and file contents.

All HTTP functions accept the GET function as the ``gh_get`` parameter
(normally ``github_pacing.get``), and the threshold constants are passed
explicitly by the caller; this keeps the module free of global
dependencies and easy to test.

``get_pr_metadata_cached`` keeps an in-memory cache indexed by
``(repo_full, pr_number)`` so the metadata of a PR linked to several
issues is downloaded only once per process. The cache is not
thread-safe for concurrent writes; with the small worker pool used by
phase 5 the worst case is the same PR being fetched twice, which is
idempotent.
"""
from __future__ import annotations

import base64
import os
from datetime import datetime, timezone
from typing import Callable

import requests


# Cache of PR metadata, indexed by (repo_full, pr_number).
_PR_META_CACHE: dict = {}


def select_primary_pr(pr_list: list) -> dict:
    """Select the 'primary' PR to use for downloading the files.

    Priority:
      1. oldest merged closing_reference (the one that closed the issue)
      2. oldest merged referenced_event
      3. oldest merged search_api / search_api_aggregato
      4. oldest merged commit_search / commit_search_aggregato
      5. oldest merged cross_reference (fallback)

    If no PR turns out to be merged, returns the oldest one overall.
    """
    sorted_prs = sorted(pr_list, key=lambda p: p.get("merged_at", ""))

    # Sources ordered from strongest (explicit closure) to weakest.
    for fonte_priority in (
        "closing_reference",
        "referenced_event",
        "search_api",
        "search_api_aggregato",
        "commit_search",
        "commit_search_aggregato",
        "cross_reference",
    ):
        candidates = [p for p in sorted_prs if p.get("source") == fonte_priority and p.get("merged")]
        if candidates:
            return candidates[0]

    return sorted_prs[0]


def to_utc_aware(value) -> datetime | None:
    """Normalize a datetime/ISO-string value to an aware `datetime` in UTC.

    Accepts:
      - None / empty string           → None
      - aware datetime                → converted to UTC
      - naive datetime                → assumed UTC (GitHub emits all
                                        timestamps in UTC)
      - ISO 8601 string with 'Z'      → e.g. "2025-03-14T10:22:11Z"
      - ISO 8601 string with offset   → e.g. "2025-03-14T10:22:11+00:00"
      - ISO 8601 string without tz    → assumed UTC

    Returns None for any unparseable input (instead of raising).
    """
    if value is None:
        return None

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # fromisoformat on Python < 3.11 does not accept a trailing "Z".
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)

    return None


def compute_delta_days(date_a, date_b) -> int | None:
    """Delta in days between two timestamps (date_b - date_a).

    Accepts the same formats as to_utc_aware. Returns None if either of the
    two is invalid or if the delta cannot be computed.
    """
    a = to_utc_aware(date_a)
    b = to_utc_aware(date_b)
    if a is None or b is None:
        return None
    return (b - a).days


def is_binary_extension(filename: str, binary_extensions: set) -> bool:
    """True if the filename has an extension among the known binary ones."""
    _, ext = os.path.splitext(filename.lower())
    return ext in binary_extensions


def get_pr_details(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    gh_get: Callable,
) -> dict | None:
    """GET /repos/{owner}/{repo}/pulls/{pr_number} → normalized dict."""
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    resp = gh_get(url)
    if not resp:
        return None

    data = resp.json()
    return {
        "base_sha":         data["base"]["sha"],
        "head_sha":         data["head"]["sha"],
        "merge_commit_sha": data.get("merge_commit_sha"),
        "mergeable":        data.get("mergeable"),
        "mergeable_state":  data.get("mergeable_state"),
        "merged":           data.get("merged", False),
        "closed_at":        data.get("closed_at"),
        "merged_at":        data.get("merged_at"),
        "source_branch":    data["head"]["ref"],
        "base_branch":      data["base"]["ref"],
        "pr_date":          data.get("merged_at") or data.get("created_at"),
        "additions":        data.get("additions", 0),
        "deletions":        data.get("deletions", 0),
        "changed_files":    data.get("changed_files", 0),
    }


def get_pr_files(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    gh_get: Callable,
    max_files: int,
) -> tuple[list, dict]:
    """Paginated GET /pulls/{n}/files capped at `max_files`.

    Returns (files, meta) where meta is:
      {"complete": bool, "capped": bool}
      - complete=True  → every file GitHub has was fetched (no error, no cap).
      - capped=True    → stopped on purpose at `max_files` (complete=False).
      - complete=False & capped=False → a page failed (transient error after
        github_pacing retries): the files gathered so far are kept.
    """
    files = []
    page = 1
    meta = {"complete": True, "capped": False}
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
        resp = gh_get(url, params={"per_page": 100, "page": page})
        if not resp:
            # Page failed after retries: keep what we have, flag incomplete.
            meta["complete"] = False
            break
        batch = resp.json()
        if not batch:
            break
        files.extend(batch)
        if len(batch) < 100:
            break
        page += 1

        if len(files) >= max_files:
            print(f"    ⚠️  PR #{pr_number} has {len(files)}+ files — truncated to {max_files}")
            files = files[:max_files]
            meta["capped"] = True
            meta["complete"] = False
            break

    if meta["capped"] and len(files) > max_files:
        files = files[:max_files]

    return files, meta


def get_pr_commits(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    gh_get: Callable,
    max_commits: int,
) -> list:
    """Paginated GET /pulls/{n}/commits capped at `max_commits`."""
    commits = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/commits"
        resp = gh_get(url, params={"per_page": 100, "page": page})
        if not resp:
            break
        batch = resp.json()
        if not batch:
            break

        for c in batch:
            commits.append({
                "sha":                 c["sha"],
                "commit_date":         c["commit"]["committer"]["date"],
                "message":             c["commit"]["message"],
                "author":              c["commit"]["author"].get("name", ""),
                "files_changed_count": c.get("stats", {}).get("total", None),
            })

        if len(batch) < 100:
            break
        page += 1

        if len(commits) >= max_commits:
            print(f"    ⚠️  PR #{pr_number} has {max_commits}+ commits — truncated")
            commits = commits[:max_commits]
            break

    return commits


def get_file_content(
    owner: str,
    repo: str,
    path: str,
    ref: str,
    *,
    gh_get: Callable,
    binary_extensions: set,
    max_file_size_bytes: int,
    known_size: int = 0,
) -> tuple[str | None, str | None]:
    """Download the content of a file from the GitHub repo.

    Returns a tuple (content, error):
      - ("__BINARY__", None)     if the extension is a known binary one
      - ("__TOO_LARGE__", None)  if the size exceeds max_file_size_bytes
      - (utf8_or_latin1_text, None) if decoding succeeded
      - (None, "api_error")      if the GET fails
      - (None, "is_directory")   if the path is a directory
      - (None, "decode_error: ...") / (None, "unknown_encoding: ...") otherwise

    `known_size`: if already known from the PR file list, it avoids the API
    call when the file already exceeds max_file_size_bytes.
    """
    if is_binary_extension(path, binary_extensions):
        return "__BINARY__", None

    # A size known from the PR file list avoids the API call entirely.
    if known_size > max_file_size_bytes:
        return "__TOO_LARGE__", None

    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    resp = gh_get(url, params={"ref": ref})

    if not resp:
        return None, "api_error"

    data = resp.json()

    if isinstance(data, list):
        return None, "is_directory"

    size = data.get("size", 0)
    if size > max_file_size_bytes:
        return "__TOO_LARGE__", None

    encoding = data.get("encoding", "")
    content_b64 = data.get("content", "")

    if encoding == "base64" and content_b64:
        try:
            raw_bytes = base64.b64decode(content_b64)
            try:
                return raw_bytes.decode("utf-8"), None
            except UnicodeDecodeError:
                try:
                    return raw_bytes.decode("latin-1"), None
                except UnicodeDecodeError:
                    return "__BINARY__", None
        except Exception as e:
            return None, f"decode_error: {e}"

    if not content_b64:
        return "", None

    return None, f"unknown_encoding: {encoding}"


def get_pr_metadata_cached(
    repo_full: str,
    owner: str,
    repo_name: str,
    pr_number: int,
    *,
    gh_get: Callable,
    max_files_per_pr: int,
    max_commits_per_pr: int,
) -> dict | None:
    """Retrieve (and cache) details + files + commits of a PR.

    The cache is indexed by (repo_full, pr_number). If the key already exists
    (even with a None value), it is reused without calling the API again. A
    key mapped to None means "PR not retrievable": we do not retry to avoid
    infinite loops on removed PRs or bans.

    Returns None if get_pr_details fails, otherwise the dict
    {"details": ..., "files": ..., "commits": ...}.
    """
    key = (repo_full, pr_number)
    if key in _PR_META_CACHE:
        return _PR_META_CACHE[key]

    pr_details = get_pr_details(owner, repo_name, pr_number, gh_get=gh_get)
    if not pr_details:
        _PR_META_CACHE[key] = None
        return None

    pr_files, files_meta = get_pr_files(
        owner, repo_name, pr_number,
        gh_get=gh_get, max_files=max_files_per_pr,
    )
    pr_commits = get_pr_commits(
        owner, repo_name, pr_number,
        gh_get=gh_get, max_commits=max_commits_per_pr,
    )

    meta = {
        "details": pr_details,
        "files":   pr_files,
        "files_meta": files_meta,
        "commits": pr_commits,
    }
    _PR_META_CACHE[key] = meta
    return meta


def reset_cache() -> None:
    """Empty the PR cache. Useful in tests or to force a re-fetch."""
    _PR_META_CACHE.clear()