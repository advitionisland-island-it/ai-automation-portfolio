# ADR-001: The boundary between n8n and Python

- Status: accepted (2026-09-26)
- Deciders: the project owner (the boundary), the maintainer (how it is implemented)

## Context

The engine takes an inquiry from a web form, qualifies it with an LLM, drafts a follow-up, waits
for a person to approve the exact text, sends it, and tells people when something fails.

Two runtimes are available:

- **n8n**, a workflow tool with a visual editor, webhooks, schedules and many ready-made
  integrations (mail, chat, CRMs).
- **Python** (FastAPI, a worker, PostgreSQL), where code is tested, reviewed and versioned like
  any other software.

The work splits naturally into two kinds:

1. **Connecting systems**: receive a form post, send an email, run something every few
   seconds, notify someone when a run failed.
2. **Deciding things**: is this input valid, is this the same request as before, which state
   may a lead move to, when to retry, what counts as an approval of which text, what is stored.

Mistakes in the second kind are costly (a double send, a mail nobody approved, a lost lead) and
must be provable with tests. Mistakes in the first kind are usually visible and easy to fix.

## Options

**A. Everything in n8n.** Validation, the state machine, idempotency, the LLM calls and the
approval rules live in n8n nodes and Code nodes.
Fast to build, and non-developers can see every step. But the rules are spread over JSON and
JavaScript snippets, which are hard to unit-test, hard to review in a diff, and cannot rely on
database transactions across steps. Concurrency (two workers, two approvals) has to be
handled by hand in each workflow.

**B. Everything in Python.** Python also receives form posts, sends every notification and runs
its own scheduler.
One runtime and one test suite. But each new integration (a chat tool, a CRM, a different mail
service) is code to write and maintain, and people who operate the flow cannot change a
notification or add a channel without a developer.

**C. Split by kind of work (chosen).**
- **n8n**: webhook ingress, scheduling, system-to-system orchestration, notification, alerting.
- **Python**: validation, the lead state machine, the business workflow for one lead
  (qualify, draft, send), scoring, retry policy, idempotency, persistence, the LLM
  abstraction, and the tests.

## Decision

Option C. The line is drawn by the question "does this decide something about a lead?".

- **W1** (n8n) receives the form, shapes it, adds an `Idempotency-Key` computed from the exact
  body it sends, and posts to the API. Python validates the input and decides whether the lead
  is new. A 4xx is passed back to the sender. A 5xx, or an API that cannot be reached, fails
  the workflow and raises an alert.
- The **worker** (Python) owns everything that happens to one lead: qualify, draft, send.
  Every state change goes through one function that refuses illegal transitions.
- Python writes **notifications** (review requests, failure alerts) in the same transaction as
  the state change that causes them. **W2** and **W3** (n8n) poll the API, claim a batch for a
  short lease, send the emails, and mark each one delivered.
- A person **approves on a page served by Python**, not in n8n. The approval is bound to the
  sha256 of the exact text, and the database enforces that binding. The approval must not
  depend on a tool whose data is outside our transactions.
- **W3** also catches failures of n8n's own workflows (its error workflow), so a stopped API
  becomes an alert email.

## Consequences and trade-offs

Gains:

- The rules that can lose money or trust are plain Python with 300+ tests against a real
  PostgreSQL, plus mutation checks showing the tests catch deliberate bugs.
- Nothing in n8n can move a lead to a wrong state. The worst an n8n bug can do is fail to
  deliver or deliver twice, and both cases are visible.
- n8n keeps what it is good at. Adding a Slack alert or a CRM push is a node, not a release.

Costs:

- **Two runtimes to run and monitor.** Compose starts both, and each has a health check.
- **An internal API surface** (`/v1/review-requests/claim`, `/v1/alerts/claim`, `.../delivered`)
  exists only for n8n. Contract tests check that every URL in the workflow JSON is a real
  route.
- **At-least-once notifications and polling latency.** A review request can arrive up to about
  10 seconds late. If n8n stops between sending and marking, the email can arrive twice once
  the lease runs out. A duplicate "please review" email is cheap, and nothing that decides
  about a lead is at-least-once.
- **Workflow JSON is harder to review than code.** The files in `n8n/` are the source of truth
  and are imported on every start, so an edit made only in the n8n UI does not survive a
  restart. `tests/test_n8n_workflows.py` guards the parts that matter.
- **n8n-specific behavior to know about.** In n8n 2.x, workflows are published rather than
  activated. An error workflow runs only if it is published. Webhooks are registered a few
  seconds after n8n reports healthy. All three were found in small spikes before the
  workflows were written.

## When to choose differently

- **Non-developers must change business rules often** (for example, scoring rules): move that
  rule into a data table or a small rules service that n8n can edit, not into n8n JavaScript.
- **Volume grows beyond polling** (hundreds of notifications per minute): let Python push
  events to n8n webhooks, or put a message queue between them, instead of polling.
- **The team does not run n8n anyway**: option B with a small scheduler is simpler to operate.
- **A regulated approval trail is needed**: keep approvals in Python (as now) and add
  authentication and an audit export. Do not move approvals into a workflow tool.
