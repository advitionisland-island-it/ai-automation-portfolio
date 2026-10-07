# P01 · AI Sales Operations Engine

English | [日本語](README.ja.md) · [Portfolio](../../README.md)

**A B2B inquiry pipeline with human approval before email sending.** n8n handles intake and notifications; Python validates input, qualifies leads, drafts replies and enforces approval. PostgreSQL stores state and jobs. Mail is delivered to local Mailpit only.

## Proof in 30 seconds

| Claim | Evidence and scope |
|---|---|
| 396 tests passed on the reorganized tree | [Test results](../../evidence/p01/TEST_RESULTS.md); fresh local rerun; release commit is also checked by CI |
| 13/13 seeded mutations detected | [Historical mutation scope](../../evidence/p01/MUTATION_RESULTS.md), checkpoint `f4e74a6`; not a whole-codebase mutation score |
| Malformed AI output goes to human review | [Failure → fix](../../evidence/p01/FAILURE_TO_FIX.md), source and regression tests |
| Real API qualification and token/cost/latency records | [Historical smoke test](../../evidence/p01/API_SMOKE.md); drafting remains fake-only |

## Run it

From the repository root:

```bash
cd projects/p01-lead-automation
uv sync --locked
make up
make check
make e2e
make down
```

Requires Docker Compose v2, make and uv. All ports bind to localhost. This stack has no authentication and is not a public service.

## Inspect it

- [Architecture](../../docs/ARCHITECTURE.md) and [engineering guide](docs/guide.en.md)
- [Case study](docs/case-study.md): decisions, defects and measured qualification results
- [`src/sales_ops/`](src/sales_ops/) · [`tests/`](tests/) · [synthetic API examples](examples/requests.http)
- [Limits](../../evidence/p01/LIMITATIONS.md): fake-only drafting, synthetic company enrichment, small evaluation and at-least-once email delivery

A crash after SMTP acceptance but before the database record can send twice. Qualification was tested with real models; an end-to-end run with real-model drafting has not been verified. Read the linked evidence before interpreting any number as current or production-ready.
