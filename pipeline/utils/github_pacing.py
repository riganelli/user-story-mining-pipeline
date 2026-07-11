"""Centralized GitHub API client with adaptive rate limiting.

Instead of fixed delays or exponential backoff, the delay between calls
is derived from the rate-limit headers GitHub returns on every response:

    delay = (seconds_to_reset / available_tokens) * SAFETY_FACTOR

The formula is recomputed after every call, so the pace adapts in real
time and the token budget is consumed exactly at the reset, never
before. GitHub exposes three separate buckets (core, search, graphql);
the module keeps an independent state per bucket.

403 responses are classified into three kinds:
  - primary (Remaining=0): wait until the reset indicated by GitHub;
  - secondary (anti-abuse): wait Retry-After, or 60 s by default;
  - permanent (blocked/disabled/suspended): give up, no retry.

All internal state is protected by locks, so the module is safe to use
from a thread pool. When a thread hits a 403 a global pause stops all
threads together, so they do not extend the block window.
"""

from __future__ import annotations

import os
import time
import threading
from typing import Optional, Any

import requests
from dotenv import load_dotenv


# --- Configuration -------------------------------------------------------

# Tokens kept as a safety margin per bucket: below this amount the pace
# slows down sharply to avoid overshooting the limit.
SAFETY_BUFFER = {
    "core":    100,
    "search":  3,
    "graphql": 100,
}

# Multiplier on the theoretical delay, to absorb clock skew, network
# latency and other consumers of the same token.
SAFETY_FACTOR = 1.15

# Cap on the computed delay: above this it is cheaper to wait for the
# bucket reset instead.
MAX_DELAY_SEC = 30.0

# Delay used for the first call of each bucket, before any headers are
# available.
DELAY_INIZIALE = {
    "core":    1.0,
    "search":  3.0,
    "graphql": 1.0,
}

# Maximum attempts per call for hard errors (network failures, 5xx).
MAX_RETRIES = 3

# Cumulative cap on time spent waiting out rate-limit pauses (403/429)
# for a single call. Rate-limit waits do not consume MAX_RETRIES.
MAX_RATE_LIMIT_WAIT_SEC = 180.0

# Print the bucket state every N successful calls.
LOG_EVERY_N = 100


# --- Authentication ------------------------------------------------------

load_dotenv()
_TOKEN = os.getenv("GITHUB_TOKEN")
if not _TOKEN:
    raise ValueError("GITHUB_TOKEN not found in the .env file")

_HEADERS = {
    "Authorization": f"bearer {_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

GRAPHQL_URL = "https://api.github.com/graphql"


# --- HTTP session --------------------------------------------------------

def _build_session() -> requests.Session:
    """Create an HTTP session with default headers and a reusable TCP pool."""
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s

_SESSION = _build_session()


# --- Per-bucket state ----------------------------------------------------

class _BucketState:
    """Mutable pacing state for one rate-limit bucket."""
    def __init__(self, name: str, initial_delay: float):
        self.name           = name
        self.last_delay     = initial_delay
        self.last_call_time = 0.0
        self.call_count     = 0
        self.lock           = threading.Lock()

_BUCKETS = {
    "core":    _BucketState("core",    DELAY_INIZIALE["core"]),
    "search":  _BucketState("search",  DELAY_INIZIALE["search"]),
    "graphql": _BucketState("graphql", DELAY_INIZIALE["graphql"]),
}


# --- Global pause --------------------------------------------------------
# When one thread hits a 403, all threads are paused together.

_PAUSE_UNTIL = 0.0
_PAUSE_LOCK  = threading.Lock()

def _wait_global_pause() -> None:
    """If a global pause is in progress, wait for it to expire."""
    while True:
        with _PAUSE_LOCK:
            now = time.monotonic()
            if now >= _PAUSE_UNTIL:
                return
            wait = _PAUSE_UNTIL - now
        time.sleep(min(wait, 5.0))

def _set_global_pause(seconds: float) -> None:
    """Set a global pause; a longer pause already in progress is kept."""
    global _PAUSE_UNTIL
    target = time.monotonic() + seconds
    with _PAUSE_LOCK:
        if target > _PAUSE_UNTIL:
            _PAUSE_UNTIL = target


# --- Adaptive delay ------------------------------------------------------

def _compute_delay(resp: requests.Response, bucket_name: str) -> float:
    """Compute the delay before the next call from the response headers.

    If the headers are missing or malformed the previous delay is kept
    unchanged (never speed up without data).
    """
    try:
        remaining = int(resp.headers.get("X-RateLimit-Remaining", -1))
        reset_ts  = int(resp.headers.get("X-RateLimit-Reset", 0))
    except (ValueError, TypeError):
        return _BUCKETS[bucket_name].last_delay

    if remaining < 0 or reset_ts <= 0:
        return _BUCKETS[bucket_name].last_delay

    seconds_to_reset = max(1.0, reset_ts - time.time())

    buffer = SAFETY_BUFFER.get(bucket_name, 50)
    available_tokens = max(1, remaining - buffer)

    delay = (seconds_to_reset / available_tokens) * SAFETY_FACTOR

    return min(delay, MAX_DELAY_SEC)


def _identify_bucket(url: str, method: str = "GET") -> str:
    """Map a URL to the rate-limit bucket it consumes."""
    if "/graphql" in url:
        return "graphql"
    if "/search/" in url:
        return "search"
    return "core"


# --- 403 classification ---------------------------------------------------

def _classify_403(resp: requests.Response) -> tuple[str, Optional[float]]:
    """Classify a 403 as ("primary" | "secondary" | "permanent", wait_seconds)."""
    remaining_str = resp.headers.get("X-RateLimit-Remaining", "?")
    retry_after   = resp.headers.get("Retry-After")
    body_lower    = resp.text.lower() if resp.text else ""

    permanent_markers = ["have been blocked", "has been disabled",
                         "account has been suspended", "abuse"]
    if any(m in body_lower for m in permanent_markers):
        return ("permanent", None)

    if remaining_str == "0":
        try:
            reset_ts = int(resp.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait = max(1, reset_ts - int(time.time()))
            return ("primary", wait)
        except (ValueError, TypeError):
            return ("primary", 60)

    if retry_after:
        try:
            return ("secondary", float(retry_after))
        except ValueError:
            pass
    # Default recommended by GitHub when Retry-After is missing.
    return ("secondary", 60.0)


# --- Periodic log ---------------------------------------------------------

def _periodic_log(resp: requests.Response, bucket_name: str, delay: float) -> None:
    """Print the bucket state every LOG_EVERY_N successful calls."""
    bucket = _BUCKETS[bucket_name]
    with bucket.lock:
        bucket.call_count += 1
        n = bucket.call_count
    if n % LOG_EVERY_N != 0:
        return
    try:
        remaining = int(resp.headers.get("X-RateLimit-Remaining", -1))
        limit     = int(resp.headers.get("X-RateLimit-Limit", -1))
        reset_ts  = int(resp.headers.get("X-RateLimit-Reset", 0))
    except (ValueError, TypeError):
        return
    if remaining < 0 or limit <= 0:
        return
    pct = (remaining / limit) * 100
    reset_in = max(0, reset_ts - int(time.time()))
    print(f"    📊 [gh:{bucket_name}] {remaining}/{limit} remaining "
          f"({pct:.0f}%, reset in {reset_in // 60}m{reset_in % 60:02d}s, "
          f"current delay: {delay:.2f}s)")


# --- Core request ---------------------------------------------------------

def _request(method: str, url: str, *,
             params: dict = None, json_body: Any = None,
             timeout: float = 30.0) -> Optional[requests.Response]:
    """HTTP request with adaptive pacing and 403/429/5xx handling.

    Returns the response on 200, None on a definitive error.
    """
    bucket_name = _identify_bucket(url, method)
    bucket = _BUCKETS[bucket_name]

    # hard_errors counts only real failures (network, 5xx) against
    # MAX_RETRIES; rate-limit pauses accumulate in rate_limit_waited and
    # are bounded separately by MAX_RATE_LIMIT_WAIT_SEC.
    hard_errors = 0
    rate_limit_waited = 0.0
    while True:
        # Honor a global pause set by another thread.
        _wait_global_pause()

        # Apply the delay computed after this bucket's last call.
        with bucket.lock:
            now = time.monotonic()
            time_since_last = now - bucket.last_call_time
            remaining_wait  = bucket.last_delay - time_since_last
        if remaining_wait > 0:
            time.sleep(remaining_wait)

        try:
            with bucket.lock:
                bucket.last_call_time = time.monotonic()
            resp = _SESSION.request(
                method, url,
                params=params, json=json_body,
                timeout=timeout,
            )
        except requests.RequestException as e:
            hard_errors += 1
            if hard_errors >= MAX_RETRIES:
                print(f"    ❌ [gh] Failed after {MAX_RETRIES} attempts: {url}")
                return None
            wait = min(MAX_DELAY_SEC, 2.0 * hard_errors)
            print(f"    ⚠️  [gh] Network error ({e}), retry in {wait:.0f}s "
                  f"(attempt {hard_errors}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        # Success: update the adaptive delay and return.
        if resp.status_code == 200:
            new_delay = _compute_delay(resp, bucket_name)
            with bucket.lock:
                bucket.last_delay = new_delay
            _periodic_log(resp, bucket_name, new_delay)
            return resp

        # 404 is permanent for the single call: no retry.
        if resp.status_code == 404:
            print(f"    ⚠️  [gh] 404 Not Found: {url}")
            return None

        # Rate limiting: classify and wait, or give up if permanent.
        if resp.status_code in (403, 429):
            type, wait = _classify_403(resp)
            if type == "permanent":
                print(f"    ❌ [gh] 403 PERMANENT (ban/disabled/suspended): {url}")
                print(f"       body: {resp.text[:200]}")
                return None
            wait_s = float(wait)
            # A rate-limit pause does not consume a retry attempt, but the
            # total wait is capped so a persistent limit cannot hang the job.
            if rate_limit_waited + wait_s > MAX_RATE_LIMIT_WAIT_SEC:
                print(f"    ❌ [gh:{bucket_name}] Rate limit still active after "
                      f"{rate_limit_waited:.0f}s waited "
                      f"(cap {MAX_RATE_LIMIT_WAIT_SEC:.0f}s), giving up: {url}")
                return None
            rate_limit_waited += wait_s
            print(f"    ⏳ [gh:{bucket_name}] Rate limit {type} "
                  f"({resp.status_code}), GLOBAL pause {wait_s:.0f}s "
                  f"(waited {rate_limit_waited:.0f}/{MAX_RATE_LIMIT_WAIT_SEC:.0f}s total)")
            _set_global_pause(wait_s)
            _wait_global_pause()
            continue

        # Server errors: retry with a small backoff.
        if resp.status_code >= 500:
            hard_errors += 1
            if hard_errors >= MAX_RETRIES:
                print(f"    ❌ [gh] Failed after {MAX_RETRIES} attempts: {url}")
                return None
            wait = min(MAX_DELAY_SEC, 2.0 * hard_errors)
            print(f"    ⚠️  [gh] Server error {resp.status_code}, "
                  f"retry in {wait:.0f}s (attempt {hard_errors}/{MAX_RETRIES})")
            time.sleep(wait)
            continue

        # Any other 4xx is unrecoverable.
        print(f"    ❌ [gh] HTTP {resp.status_code} unrecoverable: {url}")
        return None


# --- Public API -----------------------------------------------------------

def get(url: str, params: dict = None, timeout: float = 30.0
        ) -> Optional[requests.Response]:
    """HTTP GET with adaptive pacing on the bucket implied by the URL.

    Returns the response on 200, None on 404, permanent 403 or errors.
    """
    return _request("GET", url, params=params, timeout=timeout)


def post(url: str, json_body: Any = None, timeout: float = 30.0
         ) -> Optional[requests.Response]:
    """HTTP POST. Used mainly for the GraphQL endpoint."""
    return _request("POST", url, json_body=json_body, timeout=timeout)


def graphql(query: str, timeout: float = 30.0) -> Optional[dict]:
    """Execute a GraphQL query and return ``response["data"]``, or None.

    GitHub can return 200 with an ``errors`` list in the body; such
    cases are logged but the data is returned anyway, leaving the
    decision to the caller.
    """
    resp = _request("POST", GRAPHQL_URL, json_body={"query": query}, timeout=timeout)
    if resp is None:
        return None
    try:
        result = resp.json()
    except ValueError as e:
        print(f"    ❌ [gh:graphql] Non-JSON response: {e}")
        return None
    if "errors" in result:
        # Log only the first errors to avoid flooding stdout.
        print(f"    ⚠️  [gh:graphql] GraphQL errors: {result['errors'][:2]}")
    return result.get("data")


def rate_limit_state() -> Optional[dict]:
    """Query ``/rate_limit`` and return GitHub's ``resources`` dict, or None."""
    resp = _request("GET", "https://api.github.com/rate_limit", timeout=10)
    if resp is None:
        return None
    try:
        return resp.json().get("resources")
    except ValueError:
        return None


def print_state(label: str = "") -> None:
    """Log the state of the three buckets (useful at phase start/end)."""
    print()
    print("=" * 70)
    print(f"RATE LIMIT — {label}")
    print("=" * 70)
    resources = rate_limit_state()
    if not resources:
        print("  (could not retrieve rate limit state)")
        print()
        return
    for name in ("core", "search", "graphql"):
        info = resources.get(name, {})
        limite   = info.get("limit", "?")
        rimaste  = info.get("remaining", "?")
        reset_ts = info.get("reset", 0)
        reset_str = time.strftime("%H:%M:%S", time.localtime(reset_ts))
        print(f"  {name:10s}  {rimaste}/{limite}  (reset at {reset_str})")
    print()
