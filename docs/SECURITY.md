# Sutradhar — Security

> Scope: the CLI, production WSGI service, web UI, provider transports and
> exported plans in this repository. The public deployment terminates TLS at
> Render and serves the application through exact-pinned Waitress 3.0.2.

## Trust boundaries

Untrusted data enters through:

1. `POST /api/plan` JSON fields;
2. free-text notes, taste preferences and natural-language briefs;
3. model responses from Groq, Gemini or another configured endpoint;
4. environment configuration;
5. static URL paths and proxy headers.

The deterministic engines trust only a validated `Brief`. A model never
allocates money, schedules dates, names real vendors or changes risk scores.

## Credentials

- Keys are read from environment variables or a git-ignored `.env`.
- Secret fields use `repr=False`.
- Keys are sent in headers, never URLs.
- `redact()` covers OpenAI, Groq (`gsk_`), Gemini, GitHub, AWS, bearer and
  key/value credential shapes before an error can reach a terminal.
- Plans, exports, health responses and provenance never contain credentials.
- CI scans the complete Git history with Gitleaks.

The key used during development must be rotated after being exposed outside a
secret manager. Render stores the replacement only as
`SUTRADHAR_OPENAI_API_KEY`; it is never committed.

## HTTP boundary

The public service is a typed WSGI application (`server/wsgi.py`) behind
Waitress, configured with eight threads, a 30-second idle channel timeout,
tracebacks disabled and a 32 KB request-body ceiling.

Requests are rejected as follows:

- cross-site browser write: `403`;
- missing/wrong media type: `415`;
- malformed JSON, invalid UTF-8, empty/truncated body: `400`;
- oversized body or brief: `413`;
- invalid values or unknown top-level/taste fields: `422`;
- per-client request/model limit: `429` with `Retry-After`;
- unknown API route: `404`.

Waitress accepts proxy headers only when `SUTRADHAR_TRUST_PROXY=1` (set on
Render, whose managed ingress is the service's only public network path). It
trusts exactly one ingress hop, normalizes `REMOTE_ADDR`, and removes untrusted
forwarding headers before calling the application. The limiter uses only that
normalized address; it never reparses `X-Forwarded-For`. Client cardinality is
bounded to 4,096 LRU entries to prevent a spoofed-address memory leak.

Every response receives:

- a random `X-Request-Id` also present in the structured access line;
- HSTS (`max-age=63072000; includeSubDomains; preload`);
- `nosniff`, `DENY`, no-referrer, COOP and CORP;
- a restrictive Permissions Policy;
- route-specific CSP (`default-src 'none'` for API; self-only UI policy);
- `Cache-Control: no-store` for personal API output.

Static files are loaded only from an allowlisted web root and extension set.
Path traversal is rejected after URL decoding. Assets are preloaded once,
served with SHA-256 ETags and gzip, and never interpreted as templates.

## Model safety and cost controls

The model path is:

```
screen input -> bounded prompt -> provider -> sanitize -> validate schema
             -> at most one repair -> ground against engine budget -> render
```

Controls:

- injection-shaped host instructions are refused before prompting;
- prompt and response sizes are bounded;
- one total 40-second budget covers the initial and repair calls;
- transport performs at most one retry within that same deadline;
- successful identical calls use a 128-entry, six-hour LRU/TTL cache;
- cache keys are SHA-256 digests, never raw prompts;
- concurrent duplicates share one in-flight call;
- failures are never cached;
- 500 upstream calls per instance/day by default;
- three consecutive failures open a 60-second circuit breaker;
- malformed, blocked, unavailable or timed-out output falls back to curated,
  budget-grounded catalogue ideas with explicit provenance and warning.

All bounds are clamped when loaded from the environment.

## Data handling

Sutradhar has no database, account system, cookies or analytics. A plan exists
only in request memory and in the response/export chosen by the host. The
model-response cache is process-local, bounded and expires automatically; raw
prompts are not retained as cache keys or written to logs.

## Supply chain and CI

- Runtime: `waitress==3.0.2`; the planning core/provider transports are stdlib.
- Build and development tools are exact-pinned in `pyproject.toml`.
- GitHub Actions are pinned by commit SHA with persisted credentials disabled.
- CI enforces Ruff, formatting, strict mypy, 85% combined coverage, tests on
  Python 3.10–3.12 across Linux/macOS/Windows, live WSGI smoke checks, a 50 KB
  web-asset budget and the challenge repository-size budget.

## Residual risks

- Process-local limits/cache are not a substitute for a distributed edge
  limiter if the service scales to multiple instances.
- Render controls TLS and host-level DDoS protection.
- Reference prices remain planning estimates, not quotes or financial advice.
- Anyone with the public URL can request a plan; the daily cap, per-client
  limiter and circuit breaker bound abuse but do not provide user identity.

## Verification

```bash
make check
python -m pytest --cov=sutradhar --cov-report=term-missing
sutradhar web --port 8787
curl -fsS http://127.0.0.1:8787/api/health
curl -fsSI -H 'Accept-Encoding: gzip' http://127.0.0.1:8787/app.js
```

The tests specifically cover same-origin writes, unknown fields, 32 KB bodies,
security headers, traversal, ETag/gzip/HEAD semantics, bounded rate-limiter
cardinality, LRU/TTL expiry, concurrent model-call deduplication, circuit
breaking, daily caps, deadlines and catalogue fallback.
