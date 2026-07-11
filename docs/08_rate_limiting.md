# 08 — Rate Limiting

> Adaptive GitHub API client used by every phase that touches GitHub.
> Self-tuning delay formula based on response headers. Three independent
> buckets. Smart 403 handling without exponential backoff.

## 1. The problem

GitHub's API has three independent rate limit pools:

- **Core REST** — 5000 requests/hour. Used for almost everything
  (issues, PRs, file contents, commits).
- **Search REST** — 30 requests/minute. Used for `/search/issues` and
  `/search/code` (heavily used by phase 1 and phase 2 strategies).
- **GraphQL** — 5000 points/hour, where a "point" depends on the
  query complexity. Used by phase 3.

The naive approach is "sleep N seconds between calls". That either
wastes the budget (too slow) or trips the limiter (too fast). The
right approach is **adapt the delay to what GitHub tells you in the
response headers**.

`github_pacing.py` does exactly that, transparently. Every phase
imports it and calls `gh.get(...)` or `gh.graphql(...)` like a normal
HTTP client.

## 2. The core formula

After every successful call, GitHub returns headers that look like:

```
X-RateLimit-Remaining: 3247
X-RateLimit-Reset: 1747663200
```

`Remaining` = available tokens for this bucket. `Reset` = Unix
timestamp when the bucket refills.

From these the module computes:

```
delay = (seconds_to_reset / available_tokens) * SAFETY_FACTOR
```

The intuition: if you have 3247 tokens and 2400 seconds until reset,
you can afford one call every 0.74 seconds and you'll spend the budget
exactly on schedule. The `SAFETY_FACTOR = 1.15` adds a 15% margin to
absorb clock skew and other processes using the same token.

The formula is **recomputed after every successful call**. If the
margin drops more than expected (e.g. a parallel script is using the
same token), the next call waits longer. If it drops less, the next
call goes faster. Pure adaptive control, no manual tuning.

A hard ceiling protects against pathological cases:

```python
MAX_DELAY_SEC = 30.0
```

If the formula says "wait 5 minutes between calls", it's better to
just wait for the reset and resume at full speed. Above the ceiling
the module switches to "wait for reset" mode.

## 3. Independent buckets

Three buckets, three independent states:

```python
SAFETY_BUFFER = {
    "core":    100,   # of 5000/h, ~2%
    "search":  3,     # of 30/min,  ~10%
    "graphql": 100,   # of 5000 points/h, ~2%
}

DELAY_INIZIALE = {
    "core":    1.0,   # 1 req/sec
    "search":  3.0,   # 0.33 req/sec (search has only 30/min)
    "graphql": 1.0,
}
```

Each bucket has its own `_BucketState`:

```python
class _BucketState:
    def __init__(self, name: str, initial_delay: float):
        self.name           = name
        self.last_delay     = initial_delay
        self.last_call_time = 0.0
        self.call_count     = 0
        self.lock           = threading.Lock()
```

The buckets are independent. Phase 5 can pound the `core` bucket at
~1 req/sec while phase 1 is sipping the `search` bucket at one call
every few seconds. The two don't interfere.

The bucket for a given call is picked from the URL:

```python
def _identify_bucket(url: str, method: str = "GET") -> str:
    """Pick the bucket based on the URL prefix."""
    if url.startswith("https://api.github.com/graphql"):
        return "graphql"
    if "/search/" in url:
        return "search"
    return "core"
```

## 4. Initial delay (before headers exist)

The very first call on a fresh process has no headers to base the
formula on. The module uses a conservative starting delay:

```python
DELAY_INIZIALE = {
    "core":    1.0,
    "search":  3.0,
    "graphql": 1.0,
}
```

Better to start slow and speed up than the opposite. After the first
successful response, the formula takes over.

## 5. 403 handling: three flavors, no exponential backoff

GitHub returns HTTP 403 for three completely different reasons. The
module distinguishes them automatically via `_classify_403`:

### Primary rate limit (Remaining = 0)

You've spent the bucket. The `X-RateLimit-Reset` header tells you
exactly when it refills. Wait for that timestamp and resume.

### Secondary rate limit (Remaining > 0, but you're going too fast)

GitHub has soft limits on burst patterns: too many requests in a
short window, too many concurrent connections, abuse heuristics.
These return 403 with `Retry-After: N` or, if that header is missing,
a default of 60 seconds.

### Permanent (account / repo blocked)

If the response body contains `"blocked"`, `"disabled"`, or
`"suspended"`, retrying is pointless. The module returns `None`
immediately and the caller treats it as a skip.

```python
def _classify_403(resp: requests.Response) -> tuple[str, Optional[float]]:
    """Returns (type, seconds_to_wait)."""
    body = resp.text.lower()
    if any(k in body for k in ("blocked", "disabled", "suspended")):
        return ("permanent", None)

    remaining = int(resp.headers.get("X-RateLimit-Remaining", "1"))
    if remaining == 0:
        reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
        wait = max(0, reset - time.time())
        return ("primary", wait)

    retry_after = resp.headers.get("Retry-After")
    wait = float(retry_after) if retry_after else 60.0
    return ("secondary", wait)
```

**No exponential backoff.** GitHub tells you exactly how long to wait;
guessing with `wait = 2 ** attempt` is at best wasteful and at worst
wrong (you might wait less than required and trip again).

## 6. Global pause on 403

When a 403 hits, **all threads** pause together:

```python
def _set_global_pause(seconds: float) -> None:
    """Tell every thread to wait until this timestamp."""
    ...

def _wait_global_pause() -> None:
    """If a global pause is set, sleep until it's over."""
    ...
```

This avoids the worst-case scenario where one thread is sleeping
through a 403 while another thread keeps issuing requests, extending
the block window indefinitely.

## 7. Token configuration

The token is loaded once at module import:

```python
load_dotenv()
_TOKEN = os.getenv("GITHUB_TOKEN")
if not _TOKEN:
    raise ValueError("GITHUB_TOKEN not found in the .env file")

_HEADERS = {
    "Authorization": f"bearer {_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
```

The session is built once and reused across calls (connection pooling):

```python
def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s

_SESSION = _build_session()
```

Reusing the session avoids TLS handshake overhead and keeps a small
pool of TCP connections open.

## 8. Public API

The module exposes three call functions and a state inspection helper:

```python
import github_pacing as gh

# REST calls — bucket picked automatically from the URL
resp = gh.get("https://api.github.com/repos/foo/bar/contents/x.py")
resp = gh.get("https://api.github.com/search/issues", params={"q": "..."})

# POST (rarely needed; mostly here for completeness)
resp = gh.post("https://api.github.com/...", json_body={...})

# GraphQL calls — returns the parsed JSON or None
data = gh.graphql("query { ... }")

# Inspect the current state of all buckets
state = gh.rate_limit_state()
gh.print_state(label="end of phase 2")
```

All call functions return `requests.Response` on success or `None` on
permanent failure (after up to 3 retries on transient errors). Callers
check for `None`:

```python
resp = gh.get(url)
if resp is None:
    # permanent failure, already logged — skip
    continue
data = resp.json()
```

## 9. Periodic logging

Every 100 successful calls per bucket, the module prints a one-line
state report so you can see live what's happening:

```python
LOG_EVERY_N = 100
```

The report shows:

- bucket name
- current `last_delay`
- tokens remaining
- percentage of budget left
- time to reset

This is how you spot, e.g., that the search bucket is at 5/30 and the
next 100 calls will take a long time.

## 10. Retries on transient errors

For 5xx server errors and network timeouts the module retries up to
`MAX_RETRIES = 3` times with a simple linear backoff:

```python
wait = min(MAX_DELAY_SEC, 2.0 * attempt)
```

After 3 failed attempts the call returns `None`. The caller decides
what to do (skip the issue, retry later, abort the phase).

This is the **only** place the module uses anything resembling
exponential backoff — and even here it's linear, capped, and only for
true server-side / network errors, not for rate limits.

## 11. Why this design beats fixed delays

A typical alternative is `time.sleep(0.8)` between every call,
regardless of the response. The problems:

- **Too slow when the budget is healthy** — you waste rate limit
  doing nothing.
- **Too fast when the budget is low** — you trip the limiter and
  block for an hour.
- **No knowledge of buckets** — a phase using search at full speed
  doesn't slow down core, but a fixed delay treats them the same.

The adaptive formula uses the same headers you already pay for in
every response — they're free — and turns them into a near-optimal
schedule. The result is concretely visible in the per-phase
`run_stats.json`: phase 1 finishes in ~10 minutes instead of ~30, with
zero 403 errors.
