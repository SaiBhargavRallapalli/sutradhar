# Security policy

Sutradhar plans events offline by default. A language model is optional, and no
model output is ever trusted to allocate money, schedule dates or change a risk
score — the deterministic engines compute every number.

The full threat model, control inventory and verification steps live in
[`docs/SECURITY.md`](docs/SECURITY.md).

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

- Use GitHub's [private vulnerability
  reporting](https://github.com/SaiBhargavRallapalli/sutradhar/security/advisories/new),
  or
- email the maintainer listed in `pyproject.toml`.

Include the affected version, a minimal reproduction, and the impact you
believe it has. You can expect an acknowledgement within a few days; please
allow time for a fix before any public disclosure.

## Supported versions

The project is pre-1.0 in API terms but ships as `1.0.0`. Fixes land on `main`;
there are no long-lived release branches.

| Version | Supported |
|---|---|
| 1.x | ✅ |

## What is in scope

- The CLI (`src/sutradhar/cli.py`).
- The WSGI service and JSON API (`src/sutradhar/server/`).
- The web UI (`web/`).
- Prompt handling, guardrails and provider transports (`src/sutradhar/ai/`).
- Exported artefacts (Markdown, CSV, ICS) and the `.ics` escaping rules.

Configuration mistakes in a *fork* (for example disabling the origin check) are
out of scope unless they stem from an insecure default in this repository.

## Quick checklist for reviewers

- Secrets are read from the environment only and redacted from every error path.
- The server rejects cross-site requests, non-JSON bodies, oversized payloads and
  path traversal, and sends a strict Content-Security-Policy.
- Model-produced strings pass through guardrails before they are rendered or
  exported.
