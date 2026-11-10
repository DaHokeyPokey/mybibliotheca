# Rate Limiting for Metadata Lookups

Outbound calls to metadata providers (Google Books, OpenLibrary and others) go
through `app/utils/adaptive_http.py`. It keeps one adaptive limiter per provider,
per worker process:

- On HTTP 429, or a Google Books 403 quota response, it backs off quickly.
- After a cooldown, it gradually speeds back up on successful responses.
- When a caller passes a wait budget (`max_wait_seconds`) and the cooldown is
  longer, it raises `RateLimitDeferred` instead of sleeping.

## Configuration

Tune it with environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `HTTP_MIN_DELAY_SECONDS` | 0 | Smallest delay between calls to one provider |
| `HTTP_MAX_DELAY_SECONDS` | 10 | Largest delay after backing off |
| `HTTP_BACKOFF_MULTIPLIER` | 2.0 | How fast the delay grows after a rate-limit response |
| `HTTP_RECOVERY_MULTIPLIER` | 0.9 | How fast the delay shrinks after a success |
| `HTTP_MAX_RETRIES` | 3 | Attempts per request |

The old fixed-delay helpers in `app/utils.py` that this page used to describe
were removed; nothing used them.
