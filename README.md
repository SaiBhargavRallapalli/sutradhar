# Sutradhar — the Celebration & Event Planner

[![CI](https://github.com/SaiBhargavRallapalli/sutradhar/actions/workflows/ci.yml/badge.svg)](https://github.com/SaiBhargavRallapalli/sutradhar/actions/workflows/ci.yml)

**Live app:** [https://sutradhar-it6j.onrender.com](https://sutradhar-it6j.onrender.com)

**Sutradhar** (सूत्रधार — *"the one who holds the thread"*) is a GenAI planning
co-pilot for **weddings, birthdays, community events, housewarmings and
anniversaries**. Give it a date, a guest count and a budget and it returns a
complete plan: an exact line-item budget that adds up, a critical-path schedule
with slack and a minimum lead time, a vendor checklist with a dated cash-flow
plan, a scored risk register, and personalised concepts grounded against real
numbers.

*Standard-library planning core. One exact-pinned production server dependency.
A language model is optional and never required.*

```
$ python -m sutradhar.cli demo
  Sutradhar 1.0.0 - celebration & event planner
  deterministic engines, optional LLM, complete offline fallback
```

---

## The vertical and the problem it solves

Celebration planning is one of the most common real-world projects people
undertake, and it fails in three predictable ways:

1. **The numbers never reconcile.** Ask a language model for a ₹15,00,000 budget
   across twelve categories and the column misses the total by a few thousand;
   the percentages don't reach 100; nobody can audit it.
2. **Nobody knows what must happen first.** A to-do list is not a schedule.
   Without dependencies, slack and a critical path, a host cannot answer *"what
   can slip?"* or *"am I already too late?"* — the two questions they actually
   have.
3. **Advice arrives ungrounded.** "Have a string-light garden theme!" with no
   price attached is a Pinterest board, not a plan.

Sutradhar solves all three by construction: **every quantity is computed by
deterministic code, and every sentence is written by a model (or the curated
catalogue when no model is configured)**, then validated and grounded against
those quantities before a host ever sees it.

That split is the whole design. It is why the budget always sums to the paisa,
why the same brief produces the same plan today and next month, and why the
product demonstrates perfectly with **no API key, no network and no GPU**.

---

## Quick start

Requires **Python 3.10+**.

```bash
git clone https://github.com/SaiBhargavRallapalli/sutradhar && cd sutradhar
python -m pip install -e .

sutradhar demo                        # a full wedding plan, in your terminal
make web                              # accessible web UI at http://127.0.0.1:8787
sutradhar brief --text "plan a 150 guest wedding in Hyderabad on 14 Feb 2027, budget 25 lakh, outdoor, garden vibes"
sutradhar export --brief plan.json --out out/   # .md + 3× .csv + .ics
```

The deterministic planning core and all provider transports use the Python
standard library. `waitress==3.0.2` is the only runtime dependency; it supplies
the bounded, production WSGI server used locally and on Render.

Optional model access (everything works without it):

```bash
cp .env.example .env      # then set SUTRADHAR_OPENAI_API_KEY or SUTRADHAR_GEMINI_API_KEY
```

`SUTRADHAR_PROVIDER` accepts `auto` (default: use a configured key, else fall
back to the offline catalogue), `openai`, `gemini`, or `mock`. The
OpenAI-compatible adapter speaks `POST /chat/completions`, so it also works with
Groq, Together, OpenRouter, Mistral, LM Studio, llama.cpp, vLLM and Ollama's
`/v1` endpoint — one implementation, many backends.
For the deployed Groq GPT-OSS model, the adapter enables strict constrained
JSON Schema output with low reasoning effort; local validation and grounding
still run before any model text reaches a plan.

---

## How it works

One call turns a brief into a complete plan:

```
brief ─▶ budget ─▶ timeline ─▶ vendors ─▶ risks ─▶ ideas ─▶ Plan
```

`pipeline.plan_event(brief)` is the entire public API. Each stage depends on a
real number produced by the stage before it — vendors need the budget to
schedule payments against, ideas need the budget to be grounded, risks need the
season and lead time already computed. That dependency chain is *why* the model
is consulted **last**, and only for prose: by the time it runs, every number it
might mention already exists and has been reconciled.

```python
from datetime import date
from sutradhar.inputs import brief_from_dict
from sutradhar.pipeline import plan_event

brief = brief_from_dict({
    "event_type": "wedding",
    "event_date": "2027-06-06",
    "guest_count": 250,
    "budget": "1500000",          # or "15 lakh"
    "currency": "INR",
    "location": "Pune",
    "city_tier": "tier_2",
    "setting": "open_air",
})

plan = plan_event(brief, today=date.today())
plan.budget.unallocated_minor    # 0 — always, by construction
plan.timeline.critical_path      # the chain that decides the date
plan.provenance["ideas"]         # Source.CATALOG or Source.MODEL
```

### Sample output

```
-- Budget ------------------------------------------------------------------------
  Catering & Beverages       ##################   ₹4,19,424.46  30.4%
  Venue & Rentals            ###########.......   ₹2,57,270.56  18.6%
  Decoration & Florals       ######............   ₹1,48,308.86  10.8%
  Photography & Videography  ######............   ₹1,29,508.09   9.4%
  ... 8 more lines ...
  --------------------------------------------------------------------
  spendable                            ₹13,80,000.00
  contingency float                     ₹1,20,000.00  (8.0%)
  unallocated                                  ₹0.00  <- always 0, by construction

  If you need to free up cash, in order of least harm:
    +₹62,913.67  trim Catering & Beverages [impact: high]
        Fewer live counters, a simpler dessert table and a smaller premium bar.

-- Timeline ----------------------------------------------------------------------
  Planning window 226 days before the event
  Critical path   54 days of hard dependencies
  Feasible        yes - the window comfortably fits the critical path

  The chain that decides your date:
    1. Fix the date & confirm the muhurat
    2. Finalise the guest list
    3. Design & print invitations
    4. Send invitations & track RSVPs

-- Vendors (8 categories) ---------------------------------------------------------
  Venue & Banquet   30% deposit  book by 09 Sep 2026
  ...               24 dated payment milestones in total
```

Run `python -m sutradhar.cli demo` for the full output including risks, ideas,
warnings and the day-of run sheet.

---

## What's inside every plan

| Section | What you get | How it's computed |
|---|---|---|
| **Budget** | 12 line items, contingency float, per-guest cost, ordered trim suggestions | Deterministic paise-integer arithmetic; the column **always sums to the total** (`unallocated == 0` by construction, asserted in tests) |
| **Timeline** | Dated tasks with slack, critical path, feasibility verdict, day-of run sheet | Forward/backward-pass scheduling over catalogue dependencies (see `docs/ALGORITHMS.md`) |
| **Vendors** | 8 categories, book-by dates, deposit schedule, dated cash-flow milestones | Derived from the budget totals and the scheduled dates — payments land on real calendar days |
| **Risks** | Scored register (likelihood × impact) plus contextual risks for *your* situation | Rule engine over season, setting, lead time and budget pressure |
| **Ideas** | Personalised concepts, each with an attached price grounded in your budget | Offline catalogue by default; model prose when configured, then validated and price-grounded before display |
| **Warnings** | Machine-checkable advisories (`short_lead_time`, `tight_but_feasible`, …) | Every warning carries a severity, a section and a suggested next action |

Every plan also records **provenance**: `plan.provenance["ideas"]` tells you
whether prose came from the catalogue or a model. The JSON output and web UI
surface that source directly.

---

## Lead-time vocabulary (read this before you demo)

Three numbers sound similar and mean different things. Getting them right is
half the product:

- **`min_lead_days`** (catalogue, per event type) — the *advisory* minimum for a
  comfortable plan: wedding 180, community 90, birthday/anniversary 30,
  housewarming 21. Fall below it and you get a `short_lead_time` warning plus a
  larger contingency buffer, because rush fees are real.
- **`ideal_lead_days`** (catalogue, per event type) — the *comfortable* runway:
  wedding 330, community 150, the rest 45–75. Inside the ideal window the
  schedule is published as-is; up to 60 days short of it you get a
  `tight_but_feasible` nudge to book venue + top vendors this week.
- **`min_required_lead_days`** (computed, per plan) — the *hard floor*: the sum
  of task durations along the critical path (54 days in the sample above). The
  schedule compacts proportionally down toward this floor when time is short,
  and below it the plan is honestly marked **infeasible** with a
  `needs at least N days` verdict instead of a fantasy schedule.

In short: **catalogue minimums advise, the critical-path floor decides.**

Contingency also scales with slack: the shorter the runway, the larger the
buffer (3% floor, up to 15%), because a host planning six weeks out can't shop
around or absorb a price rise.

---

## CLI reference

```
sutradhar demo                        # sample wedding, end to end
sutradhar plan --brief plan.json      # structured JSON brief (see tests/conftest.py for the schema)
sutradhar brief --text "..."          # plain-English brief, parsed into the same schema
sutradhar export --brief plan.json --out out/ [--format md|ics|budget.csv|timeline.csv|vendors.csv]
sutradhar web [--port 8787]           # accessible web UI, no build step
```

Global flags: `--json` (full plan on stdout), `--out DIR` (write artefacts),
`--no-color` (or `NO_COLOR=1`), `--offline` (never call a model).

```bash
python -m sutradhar.cli brief --text "plan a 150 guest wedding in Hyderabad on 14 Feb 2027, budget 25 lakh, outdoor, garden vibes"
python -m sutradhar.cli export --brief plan.json --out out/   # .md + 3x .csv + .ics
```

---

## Web UI and exports

- **Web** (`make web` → `http://127.0.0.1:8787`): the same pipeline behind a
  dependency-free page (`web/index.html` + `app.js` + `styles.css`, no build,
  no framework). Brief in, plan out, with the identical JSON the CLI produces
  available at `POST /api/plan`.
- **Exports** (all generated from the same in-memory plan, so they can never
  disagree): a shareable Markdown brief, `budget.csv` / `timeline.csv` /
  `vendors.csv` for spreadsheets, and an `.ics` calendar file with the dated
  milestones so the schedule lives where the host actually looks.

---

## Configuration

Everything works with **zero configuration**. To enable model prose, copy
`.env.example` to `.env` and set one key:

| Variable | Default | Purpose |
|---|---|---|
| `SUTRADHAR_PROVIDER` | `auto` | `auto` (first configured key, else offline catalogue), `openai`, `gemini`, `mock` |
| `SUTRADHAR_OPENAI_API_KEY` / `_BASE_URL` / `_MODEL` | — / `api.openai.com/v1` / `gpt-4o-mini` | OpenAI-compatible `POST /chat/completions` — also covers Groq, Together, OpenRouter, Mistral, LM Studio, llama.cpp, vLLM, Ollama `/v1` |
| `SUTRADHAR_GEMINI_API_KEY` / `_MODEL` | — / `gemini-2.5-flash` | Gemini `generateContent` endpoint |
| `SUTRADHAR_AI_ENABLED` | `1` | `0` = pure deterministic planning, no outbound calls ever |
| `SUTRADHAR_AI_MAX_REPAIRS` | `1` | Repair attempts on malformed model JSON (clamped 0–2) |
| `SUTRADHAR_AI_MAX_PROMPT_CHARS` | `6000` | Hard ceiling on outbound prompt size |
| `SUTRADHAR_AI_TIMEOUT` | `40` | Total model budget across initial + repair calls (clamped 5–60 seconds) |
| `SUTRADHAR_AI_CACHE_ENTRIES` / `_TTL` | `128` / `21600` | Bounded in-memory LRU size and TTL; keys are SHA-256 prompt digests |
| `SUTRADHAR_AI_DAILY_CAP` | `500` | Per-instance upstream model-call budget; cache hits do not consume it |
| `SUTRADHAR_AI_BREAKER_THRESHOLD` / `_COOLDOWN` | `3` / `60` | Consecutive failures before fail-fast fallback, and cooldown seconds |
| `SUTRADHAR_TRUST_PROXY` | auto on Render | Trust one managed ingress hop for per-client rate limiting |

`.env` is git-ignored; keys live in the environment only and are scrubbed from
all output by `ai.guardrails.redact`.

---

## Testing

**411 tests** with **92% combined statement/branch coverage** (95% lines,
85% branches); CI enforces a minimum of 85%:

| File | Tests | Covers |
|---|---|---|
| `test_pipeline.py` | 60 | End-to-end plans for every event type |
| `test_guardrails.py` | 57 | Secret redaction, prompt screening, safe output rendering |
| `test_server.py` | 55 | WSGI routes, origin policy, headers, gzip/ETag, limits |
| `test_money.py` | 47 | Paise arithmetic, rounding, formatting |
| `test_budget.py` | 43 | Reconciliation (`unallocated == 0`), contingency, trims |
| `test_export.py` | 40 | md/csv/ics byte-level output |
| `test_schema.py` | 35 | Brief parsing, natural-language inputs |
| `test_timeline.py` | 35 | Critical path, slack, compaction, feasibility |
| `test_ai.py` | 12 | Model parsing, repair, grounding, fallback, deadlines |
| `test_cli.py` | 8 | Every CLI command and error boundary |
| `test_provider.py` | 7 | Transport retries, redaction, response contracts |
| `test_config.py` | 6 | Environment parsing, clamping, secret-safe settings |
| `test_resilience.py` | 6 | LRU/TTL, in-flight dedupe, circuit breaker, daily cap |

```bash
make test    # or: PYTHONPATH=src python3 -m pytest
make check   # lint (ruff) + typecheck (mypy, strict) + test
```

---

## Security

Threat model and controls are documented in [`docs/SECURITY.md`](docs/SECURITY.md);
the summary for evaluators:

- **Secrets never persist**: env-only keys, `repr=False`, redacted from every
  printed string, never written to exports, logs or errors.
- **Server hardens by default**: strict content-type checks (415), malformed-JSON
  and empty-body rejection (400), oversized-body rejection (413), invalid-brief
  rejection (422), unknown-field rejection, same-origin writes, bounded per-client
  rate limits, path-safe static serving, and no `eval`/shell interpolation.
- **Bounded resources**: 32 KB bodies, 40-second total model budget, one retry,
  bounded LRU/TTL cache, in-flight deduplication, daily cap and circuit breaker.
- **Supply chain**: one exact-pinned runtime package, exact-pinned development
  tools, commit-pinned CI actions and full-history Gitleaks scanning.

---

## Accessibility

- Semantic HTML with labelled controls; fully keyboard-operable, no pointer-only flows.
- `--no-color` / `NO_COLOR=1` strips ANSI codes; status is never carried by
  colour alone (every verdict also prints as words: `Feasible yes/NO`).
- Plain-text CLI output and CSV exports work with screen readers; the web UI
  ships no animation or autoplay.
- Works offline on Python 3.10+ with no network and no GPU —
  the environments many real hosts actually have.

---

## Assumptions and limitations

- Prices are **reference estimates** scaled by city tier and season multipliers,
  not live vendor quotes — the plan tells you *what it should cost*, then you
  confirm against quotes. Trim suggestions are ordered by least harm, not by taste.
- Dates are interpreted in the Indian festive calendar context (peak Oct–Mar for
  weddings); the default currency is INR with lakh/crore parsing (`"25 lakh"` works).
- The model, when enabled, writes **prose only** — it can never move a number.
  Ungrounded or malformed model output is repaired (≤2 attempts) or discarded
  in favour of the catalogue, and provenance always says which happened.
- Single-event, single-day planning: no multi-city logistics, guest travel
  booking, or vendor marketplace integration — deliberately out of scope for a
  hackathon-sized, <10 MB, single-branch submission.

---

## Project structure

```
src/sutradhar/
  pipeline.py     # plan_event(): the entire public API, stage orchestration
  engines/        # budget, timeline, vendors, risk, context, personalize (pure functions)
  catalog/        # per-event profiles: wedding, birthday, community, housewarming, anniversary
  ai/             # optional model layer: provider, prompts, schema repair, guardrails, ideas
  inputs.py       # brief parsing ("15 lakh", "14 Feb 2027") + validation
  money.py        # paise-integer arithmetic — the reason budgets always reconcile
  export.py       # md + 3x csv + ics from one in-memory plan
  server/         # Waitress/WSGI API, security policy, limits, static cache
  cli.py          # demo | plan | brief | export | web
tests/            # 411 tests, see table above
web/              # dependency-free UI (index.html, app.js, styles.css)
docs/             # ARCHITECTURE.md, ALGORITHMS.md, SECURITY.md
```

Deep dives: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (module map, data flow),
[`docs/ALGORITHMS.md`](docs/ALGORITHMS.md) (budget/timeline math),
[`docs/SECURITY.md`](docs/SECURITY.md) (threat model, controls, verification).

---

## How this maps to the evaluation

| Criterion (impact) | Where it shows |
|---|---|
| Problem alignment (high) | Budgets that reconcile, schedules with critical paths, grounded ideas — the three real failure modes |
| Code quality (high) | `src/` layout, typed modules, pure-function engines, `make check` (ruff + strict mypy) |
| Security (medium) | `docs/SECURITY.md`, guardrails, server validation, env-only secrets |
| Efficiency (medium) | Integer money math, single-pass scheduling, gzip/ETag assets, AI LRU + in-flight dedupe |
| Testing (low) | 411 tests, 92% combined coverage, multi-OS CI, reconciliation + concurrency invariants |
| Accessibility (low) | Keyboard-first web UI, no-color mode, offline-first, screen-reader-safe output |

---

## License

MIT — see [LICENSE](LICENSE).
