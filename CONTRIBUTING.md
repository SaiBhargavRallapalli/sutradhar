# Contributing to Sutradhar

Thanks for helping. Sutradhar is deliberately small and dependency-light: the
planning core is standard-library Python, and the web UI has no build step. Most
changes fall into one of three buckets — a **catalogue** entry, an **engine**
change, or an **interface** change.

## Setup

```bash
python -m pip install -e ".[dev]"   # runtime + dev tooling (ruff, mypy, pytest)
make check                          # lint + format + types + coverage-gated tests
```

`make help` lists every target. The suite must stay green on Python 3.10–3.12 on
Linux, macOS and Windows (see `.github/workflows/ci.yml`).

## Before you open a pull request

Run the same gate CI runs:

```bash
make check
```

That is: `ruff check`, `ruff format --check`, `mypy` in strict mode, and
`pytest` with an 85% coverage floor. A change that lowers coverage needs a test
that earns it back rather than a lowered threshold.

## House rules

- **Numbers are deterministic, words are optional.** Engines compute; a model
  only writes prose. Never let model output change a budget, date or risk score.
- **Money is integer minor units.** Use `sutradhar.money`; never do float
  arithmetic on currency.
- **`today` is injected.** Engines take a `today` argument instead of reading
  the clock, so plans are reproducible and testable.
- **Keep functions small.** Cyclomatic complexity stays at 10 or below, and CI
  fails the build if it does not. Extract a helper rather than nesting.
- **Name your constants.** A threshold with domain meaning (a lead-time band, a
  deposit cap) gets a name and a one-line comment, not an inline literal —
  `PLR2004` fails the build on the alternative.
- **Public callables get a docstring** that says *why*, not just *what*. Tests
  read like sentences (`test_remaining_budget_is_zero_by_construction`).

## Adding a new event vertical

The catalogue is data, not code, so this is the easiest kind of contribution:

1. Create `src/sutradhar/catalog/<vertical>.py` returning an
   `EventTypeProfile` (see `wedding.py` for the fullest example).
2. Register it in `src/sutradhar/catalog/registry.py`.
3. Add an `EventType` member in `src/sutradhar/enums.py`.
4. Add a pipeline test that plans one event end-to-end and asserts that budget
   lines reconcile (`unallocated_minor == 0`).
5. Check that `severity_for`, lead-time bands and vendor→budget mappings still
   make sense for the new vertical.

No engine or interface changes should be required. If they are, that is a
design smell worth raising in the pull request first.

## Adding a test

Tests live in `tests/` and are grouped by module. Prefer asserting on public
behaviour and invariants (reconciliation, feasibility, redaction, determinism)
over implementation details. Goldens for the exporters live in `test_export.py`;
if you change output intentionally, update them in the same commit.

## Commits and pull requests

- One logical change per commit; use the imperative mood in the subject line
  ("Add anniversary catalogue", not "Added").
- Explain the *why* in the body when it is not obvious from the diff.
- Keep the tree green at every commit — CI runs on each push to `main`.

## Security

Do not include real API keys, customer names or event data in issues, tests or
fixtures. Report vulnerabilities privately as described in
[`SECURITY.md`](SECURITY.md), not in a public issue.

## License

By contributing you agree that your work is released under the MIT license in
[`LICENSE`](LICENSE).
