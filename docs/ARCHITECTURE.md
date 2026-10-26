# Sutradhar — Architecture

> *Sutradhar* (सूत्रधार) means "the one who holds the thread" — the stage director
> of a Sanskrit play, the person who keeps the story on the rails while the
> actors improvise. That is the role the software plays: the maths is fixed, the
> prose is free.

This document explains **how the system is shaped and why**. For the algorithms
themselves see [`ALGORITHMS.md`](ALGORITHMS.md); for the security posture see
[`SECURITY.md`](SECURITY.md).

---

## 1. The one idea everything follows from

A celebration plan is made of two very different kinds of statement:

| Kind | Example | Who should produce it |
|---|---|---|
| **A number** | "Catering is ₹4,19,424.46, 30.4% of the spendable budget" | A deterministic engine |
| **A sentence** | "Fewer live counters, a simpler dessert table and a smaller premium bar." | A language model (or the catalogue) |

Language models are excellent at the second and unreliable at the first. Ask a
model to split ₹15,00,000 across twelve categories and the column will not add
up — it will drift by a few thousand, the percentages will not reach 100, and no
one can audit the result. So Sutradhar never asks.

**Every quantity in a plan is computed by code. Every phrase is written by a
model, or by the catalogue when there is no model.** The model is consulted
*last*, after every number it might reference already exists and has been
reconciled, and its output is validated and then *grounded* against those
numbers before a host ever sees it.

This single rule is what makes the product trustworthy, testable and
demonstrable offline.

---

## 2. Layered design

```
┌──────────────────────────────────────────────────────────────────────┐
│  Interfaces                                                          │
│    cli.py          sutradhar demo | plan | brief | export | web      │
│    server/wsgi.py  POST /api/plan, GET /api/health, static assets    │
│    web/            zero-build accessible SPA (index.html/js/css)     │
├──────────────────────────────────────────────────────────────────────┤
│  Orchestration                                                       │
│    pipeline.py     brief → budget → timeline → vendors → risks →     │
│                    ideas → Plan   (the only place the pieces meet)   │
├──────────────────────────────────────────────────────────────────────┤
│  Domain                                                              │
│    models.py       Brief, BudgetPlan, TimelinePlan, Plan, …          │
│    inputs.py       dict/JSON → Brief, with validation                │
│    serialise.py    models ↔ plain dicts (explicit computed opt-in)   │
│    export.py       Markdown, CSV ×3, ICS                             │
├──────────────────────────────────────────────────────────────────────┤
│  Deterministic engines (all pure functions of (brief, profile, ctx)) │
│    engines/context.py       shared CostContext: guests, season, tier │
│    engines/budget.py        bounded water-filling + LRM integerise   │
│    engines/timeline.py      CPM + compaction + compression           │
│    engines/vendors.py       vendor shortlist + payment milestones    │
│    engines/risk.py          contextual risk scoring                  │
│    engines/personalize.py   taste matching + budget grounding        │
├──────────────────────────────────────────────────────────────────────┤
│  Knowledge                                                           │
│    catalog/specs.py         the schema verticals are authored in     │
│    catalog/<vertical>.py    5 declarative profiles (data, not code)  │
│    catalog/registry.py      lookup + season classification           │
├──────────────────────────────────────────────────────────────────────┤
│  Optional AI boundary (the only place a network call can happen)      │
│    ai/guardrails.py         input screening, output scrubbing, redact│
│    ai/schema.py             tiny JSON schema + extractor + validator │
│    ai/provider.py           OpenAI-compat / Gemini / Mock / Null     │
│    ai/resilience.py         LRU/TTL, de-dup, cap, circuit breaker     │
│    ai/prompts.py            system + user prompt builders            │
│    ai/ideas.py              screen→prompt→call→validate→repair→ground│
│    ai/brief.py              one-sentence request → structured Brief  │
└──────────────────────────────────────────────────────────────────────┘
```

Two properties are enforced by construction:

1. **The engines never import from `ai`.** The dependency arrow points one way.
   You can delete the entire `ai/` package and every engine, export and test of
   a number still works — the planner degrades to "catalogue mode" and still
   produces a complete, budget-grounded plan.
2. **The engines are pure.** Each takes `(brief, profile, …, today=…)` and
   returns a value. Nothing reads the clock, the environment or the network
   internally. `today` is always injected, which is why every test is
   deterministic and why the same brief always yields the same plan.

---

## 3. The knowledge catalog

Vertical-specific knowledge is **data**, not code. Each vertical is one module
returning an `EventTypeProfile`:

```python
WEDDING = EventTypeProfile(
    event_type=EventType.WEDDING,
    reference_guests=250,
    min_lead_days=180,          # the catalogue's "typical minimum"
    ideal_lead_days=330,        # the comfortable runway
    contingency_base_pct=8.0,
    peak_months=(10, 11, 12, 1, 2, 3),
    categories=(...),           # 12 CategorySpec — weights, bands, elasticity
    tasks=(...),                # 25 TaskSpec — offsets, durations, dependencies
    vendors=(...),              # 8 VendorSpec — deposits, contract musts, red flags
    risks=(...),                # 10 RiskSpec — likelihood × impact + mitigation
    themes=(...),               # 8 ThemeSpec — offline personalisation concepts
    run_of_show=(...),          # 16 RunOfShowSpec — the day-of run sheet
)
```

The five shipped verticals:

| Vertical | Categories | Tasks | Vendors | Risks | Themes | Run sheet |
|---|---:|---:|---:|---:|---:|---:|
| wedding | 12 | 25 | 8 | 10 | 8 | 16 |
| birthday | 8 | 17 | 6 | 7 | 8 | 9 |
| community | 9 | 18 | 8 | 9 | 7 | 10 |
| housewarming | 8 | 12 | 5 | 5 | 5 | 8 |
| anniversary | 9 | 15 | 5 | 5 | 5 | 8 |

**Adding a sixth vertical is a one-file change** — write
`catalog/hackathon.py`, add one line to `PROFILES` in `registry.py`, and add the
member to `enums.EventType`. No engine, interface, export or test changes. The
registry is the only lookup point, so engines never couple to a vertical by
name. (A self-check test walks every registered profile and asserts the graph is
acyclic, every dependency resolves, and every band is sane — see
`tests/test_pipeline.py`.)

### Why declarative

- **Auditable.** A planner can read the wedding profile and disagree with a
  number without reading Python.
- **Diffable.** "Change the caterer deposit from 25% to 30%" is a one-line diff.
- **Portable.** The same schema can hold regional knowledge — the wedding
  profile models Haldi/Mehendi/Sangeet because that is the market it targets;
  a US profile would model rehearsal dinners instead.

---

## 4. Data flow — one request, end to end

```
   Brief  ──validate──▶  profile + season  ──▶ CostContext
                                                   │
              ┌────────────────────────────────────┤
              ▼                                    ▼
        allocate_budget                      build_timeline
              │                                    │
              ▼                                    ▼
         BudgetPlan ──────────▶ build_vendors ◀── TimelinePlan
              │                        │
              ▼                        ▼
         build_risks            generate_ideas  ◀── Provider (optional)
              │                        │
              └───────────┬────────────┘
                          ▼
                        Plan  ──▶ export / JSON / web / CLI
```

`pipeline.plan_event(brief)` is the entire public API for a one-shot plan.
`Planner` is the same thing bound to a configuration for callers who plan
repeatedly (and, in tests, for injecting a fake provider without touching global
state).

### Provenance is first-class

Every `Plan` records where each section came from:

```python
plan.provenance == {
    "budget":   Source.ENGINE,
    "timeline": Source.ENGINE,
    "vendors":  Source.ENGINE,
    "risks":    Source.ENGINE,
    "ideas":    Source.MODEL,   # or Source.CATALOG when degraded
}
```

An interface can therefore say *"ideas came from a language model"* and *"the
budget came from the allocator"* honestly, and a user knows which parts to
scrutinise.

When the configured endpoint is Groq GPT-OSS, the provider requests strict
constrained JSON Schema output with low reasoning effort. That improves
reliability and latency but does not bypass local extraction, validation,
sanitisation, budget grounding or catalogue fallback.

### Graceful degradation is total

There is no state in which Sutradhar returns *nothing*. Every failure path ends
in the curated catalogue:

| Failure | Result |
|---|---|
| No API key configured | `MockProvider` → catalogue ideas, full plan |
| Provider unreachable / times out | warning + catalogue ideas |
| Model returns prose instead of JSON | up to `max_repairs` repair rounds, then catalogue |
| Model returns unaffordable ideas | ideas marked `grounded=False`, host warned |
| Free text trips an injection guardrail | free text dropped, catalogue ideas, host warned |

The host always gets a complete, budget-grounded plan; only the *ideas* section
changes source, and the provenance field says so.

---

## 5. Dependency boundary

The planning core, model transports and exports use only the standard library.
The deployed interface adds exactly one dependency: `waitress==3.0.2`.

- **Production boundary.** Waitress supplies bounded channels, graceful
  shutdown and a maintained WSGI runtime on every supported operating system.
- **Offline planning.** Budgets, timelines, vendors, risks, catalogue ideas and
  exports still work without a key or network.
- **Auditability.** Every line touching money, model data or user input remains
  in this repository; the sole dependency handles HTTP mechanics.
- **Supply chain.** Runtime and development packages are exact-pinned, CI
  actions are commit-pinned and Gitleaks scans the complete history.

---

## 6. Interfaces

### CLI (`sutradhar` / `python -m sutradhar.cli`)

| Command | Purpose |
|---|---|
| `demo` | Full wedding plan rendered in the terminal |
| `plan` | Plan a brief from flags or a JSON file |
| `brief` | Parse one sentence into a brief, then plan it |
| `export` | Write Markdown + CSVs + ICS to a directory |
| `web` | Serve the accessible web UI on 127.0.0.1 |

`--json` prints machine-readable output, `--offline` forces catalogue mode,
`--no-color` / `NO_COLOR` disable ANSI (per the [NO_COLOR standard](https://no-color.org)).

### WSGI service (`server/wsgi.py`)

A hardened production boundary served by Waitress — see
[`SECURITY.md`](SECURITY.md) for the full posture. Two endpoints:

- `POST /api/plan` — brief JSON in, full plan JSON out.
- `GET /api/health` — liveness + version.

`server/security.py`, `server/limits.py` and `server/static.py` isolate the
origin policy, bounded admission controls and preloaded gzip/ETag assets from
route orchestration. The same WSGI path runs locally, in CI and on Render.

### Web UI (`web/`)

Zero-build, no framework, no CDN. `index.html` + `styles.css` + `app.js`,
served by the WSGI service. Rendering uses DOM APIs exclusively — no
`innerHTML` with user or model data anywhere — so there is no XSS path by
construction. Accessibility was audited in a real browser with **zero issues**
(skip link, ARIA live regions, focus management to the results heading,
`prefers-reduced-motion`, dark mode, `forced-colors`, print styles).

---

## 7. Serialization — the computed-value problem

Domain models are `@dataclass(slots=True)` for speed and immutability. That
creates a trap: a *class attribute with a type annotation becomes a field*. So a
derived value like `BudgetPlan.unallocated_minor` cannot simply be declared as a
property-with-annotation, and naively adding it as a field would force callers
to pass a value the engine should compute.

Sutradhar's answer is an **explicit opt-in**:

```python
@dataclass(slots=True)
class BudgetPlan:
    total_minor: int
    spendable_minor: int
    ...
    _COMPUTED = ("allocated_minor", "unallocated_minor", "per_guest_pct")
```

`_COMPUTED` is declared with **no type annotation**, so it is a plain class
attribute, not a field. `serialise.to_plain()` returns exactly the fields;
`serialise.with_computed()` merges the derived values in for display or export;
`serialise.dumps()` is the JSON entry point. This keeps the two questions —
*what must a caller supply?* and *what can be derived?* — structurally separate
rather than a matter of convention.

---

## 8. Determinism

Three rules, applied everywhere:

1. **`today` is injected.** Engines take `today: date | None = None` and default
   to `date.today()` only at the boundary. Tests pass an explicit date.
2. **Ordering is total.** Wherever a `sort` could tie (risk items, trade-offs,
   ranked themes, topological order), the key is extended with a stable
   secondary so identical inputs give identical output.
3. **Money is integer minor units.** No floats touch a currency amount. See
   [`ALGORITHMS.md`](ALGORITHMS.md#money).

The practical payoff: the same brief produces byte-identical engine output
today and next month, which makes 457 tests meaningful and the demo reproducible.

---

## 9. Testing strategy

457 tests, organised by the property each one protects — not by file:

| Module | Count | Protects |
|---|---:|---|
| `test_money.py` | 47 | rounding, grouping, parsing, zero-decimal currencies |
| `test_budget.py` | 43 | allocation always reconciles; bands; trade-offs |
| `test_timeline.py` | 35 | CPM correctness; compression; infeasibility reporting |
| `test_pipeline.py` | 60 | end-to-end plans; catalog self-checks; degradation |
| `test_guardrails.py` | 57 | injection, scrubbing, redaction |
| `test_schema.py` | 35 | JSON extraction + validation + repair |
| `test_export.py` | 40 | Markdown/CSV/ICS correctness (ICS: 50 unique UEVENTs) |
| `test_server.py` | 55 | WSGI contract, origin policy, gzip/ETag, bounded limits |
| `test_inputs.py` | 39 | untrusted mappings, date/amount parsing, validation errors |
| `test_ai.py` | 19 | parsing, repair, deadlines, grounding and fallback |
| `test_cli.py` | 8 | all commands and user-facing error boundaries |
| `test_provider.py` | 7 | HTTP retries, redaction and provider contracts |
| `test_config.py` | 6 | environment parsing, clamping and secret handling |
| `test_resilience.py` | 6 | LRU/TTL, concurrency, cap and circuit breaker |

The suite runs fully offline with no credentials — the `MockProvider` exercises
the real validation and grounding path, so the safety-critical model boundary is
covered without a network.

CI (`.github/workflows/ci.yml`) runs tests on Python 3.10/3.11/3.12 across
Linux, macOS and Windows, plus Ruff, formatting, strict mypy, an 85% combined
coverage floor, a live Waitress smoke job, full-history Gitleaks, a 50 KB web
asset budget and the challenge repository-size budget.

---

## 10. Module map (source)

| Path | Lines | Responsibility |
|---|---:|---|
| `models.py` | ~506 | the domain contract |
| `catalog/*.py` | ~4,443 | five declarative verticals, schema, registry |
| `engines/*.py` | ~1,540 | budget, timeline, vendors, risk, personalisation |
| `ai/*.py` | ~1,575 | guardrails, schema, providers, prompts, ideas, resilience |
| `cli.py` | ~527 | the command line |
| `export.py` | ~479 | Markdown, CSV ×3, ICS |
| `server/*.py` | ~686 | WSGI routes, security, limits, static delivery |
| `money.py` | ~209 | integer minor units + formatting |
| `inputs.py` | ~191 | dict/JSON → validated Brief |
| `pipeline.py` | ~139 | orchestration |
| `web/*` | ~1,188 | accessible zero-build UI |

---

## 11. Design decisions worth defending

**Why not fine-tune or RAG a model on event knowledge?**
Because the hard part of event planning is arithmetic and scheduling under
constraints, not vocabulary. A model that has read every wedding blog still
cannot reliably sum a budget or compute slack. Encoding the knowledge as data
and the maths as code is both more accurate and more inspectable; the model is
then free to do what it is genuinely good at — phrasing.

**Why five verticals rather than one general planner?**
Because "wedding" and "birthday" differ in *structure*, not tone: different
categories, different lead times, different risk shapes, different seasonal
peaks. A general planner averages them into mush. Verticals as data means the
specificity costs nothing at runtime.

**Why integer minor units?**
The floating-point budget bug — a column of rounded values that misses the total
by a few rupees — is the single most common failure of LLM-generated budgets.
Integers plus largest-remainder allocation make `unallocated == 0` a
*constructional* guarantee, not a hopeful rounding.

**Why `min_required_lead_days` (the dependency floor) rather than the ideal runway?**
The catalogue's `ideal_lead_days` is advice ("a comfortable wedding takes ~330
days"); the engine's `min_required_lead_days` is a *fact* — the sum of the
critical-path durations, i.e. the least time the work itself can occupy once
compacted. Conflating them would either panic perfectly feasible six-month plans
or reassure genuinely impossible two-week ones. Both numbers are reported, with
different jobs. See [`ALGORITHMS.md`](ALGORITHMS.md#timeline).

**Why a bespoke JSON validator instead of a library?**
The planning core and provider transports remain standard-library-only; Waitress
is reserved for production HTTP mechanics. The small validator also does
domain-specific work a generic package would not: it extracts JSON out of
chatty prose, lists *every* problem in one pass so a single repair round can fix
all of them, and caps every string and list length at the boundary.
