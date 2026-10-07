# Case study: an AI sales-operations engine with a human in the loop

> **Draft.** Everything below is backed by the code and tests in this repository. The numbers
> in "Measured result" are quoted from the two evaluation reports in `eval/reports/`.

## Problem

B2B companies receive inquiries through web forms. Someone has to read each one, decide how
promising it is, and write a first reply, quickly and without mistakes: an inquiry answered
twice, or answered with an unreviewed AI text, costs trust. An LLM can help with triage and
drafting, but only if its output is treated as a suggestion and a person stays in control of
what is sent.

## Before

This is a portfolio project, not a client engagement, and it models a common setup: inquiries
arrive in a shared inbox or a spreadsheet, a salesperson triages them by hand, and replies are
typed one by one. Nothing prevents duplicates, and nothing records why a lead was judged the
way it was. The project builds the pipeline that replaces this, and it uses only synthetic
data.

## Architecture

A form posts to an n8n webhook (W1). n8n adds an idempotency key and hands the post to a FastAPI
service, which validates it and stores the lead in PostgreSQL. A worker takes jobs from a queue
in the same database. It asks the LLM to score the lead, drafts a reply, and later sends the
reply a person approved. n8n polls the API to email reviewers (W2) and to send alerts (W3).
Reviewers approve, edit or reject on a small server-rendered page. All email goes to Mailpit,
a local mail catcher. The README has the diagrams.

## Why this architecture

- **n8n connects, Python decides.** Everything that can lose trust (validation, duplicates,
  the lead's state, what counts as an approval) is plain Python, tested against a real
  database. n8n does what it is good at: webhooks, schedules and notifications. ADR-001 weighs
  this against doing everything in n8n or everything in Python.
- **PostgreSQL as the queue.** `FOR UPDATE SKIP LOCKED` plus a lease gives safe parallel
  workers and crash recovery without adding Redis or Celery at this scale. State lives in one
  place, and a state change and the job it causes commit together.
- **The LLM suggests, rules decide.** The model returns a score, reasons and a confidence.
  Python turns the score into a tier and decides when a person must look. Invalid output
  never reaches the database: it goes to review with the raw text kept.
- **Approval bound to the exact text.** An approval carries the sha256 of the draft the
  reviewer read. The database refuses an outbox row whose text does not hash to its
  approval's hash, so the email sent is the text that was approved.

## Failure cases

Transient LLM and SMTP errors are retried with backoff. Exhausted retries or a permanent error
end in a failure state and exactly one alert. A worker that dies mid-job has its job taken over
when the lease runs out. A re-run send job, a double approval or ten approvals at once still
send one email. One case is a documented limit: if the worker dies after the SMTP server
accepted a mail but before it recorded that, the retry sends it again, with the same
Message-ID. Marking a mail as sent before sending would turn the same crash into a lost email,
and for a sales reply a rare duplicate is the smaller harm. A test reproduces this case.

## Security

The stack is local only, and it has no authentication yet. That is the first requirement for
any deployment. Secrets stay in a git-ignored `.env`, and a scanner runs on every check. The
prompt leaves out the sender's name and email address, inquiry text is fenced off as data, and
logs mask addresses. The review page escapes all outside text and refuses to be framed. Its
forms carry the hash of the text the reviewer saw, so a form posted from another site cannot
approve anything.

## Testing

The tests run against a real PostgreSQL and a real SMTP catcher. They cover parallel
duplicate requests, two workers on one queue, the lead state machine as a table of all 81
possible moves, and database constraints hit directly as if the code had a bug. The tests
themselves were checked by breaking the code on purpose, one defense at a time. Beyond the
evaluation, one real call went through the worker (the provider smoke test), and its record
held the model, prompt version, token counts, cost and latency. Drafting is tested with the
fake provider only.

Testing and small experiments found real defects before they shipped:

- The email validator library did not enforce the 64-character limit before the `@`.
- The LLM SDK's error classes put "overloaded" (529) and "unavailable" (503) outside its
  server-error class, so they were not retried. Errors are now classified by status code.
- Python's `smtplib` reports a refused recipient the same way for a temporary (4xx) and a
  permanent (5xx) refusal, so greylisting was treated as permanent.
- The hash that binds an approval to a draft could be split two ways when the subject
  contained a line break. Subjects are now one line, enforced by the database.
- A database error right after a successful send marked the lead as failed, although the
  mail had gone out.
- In n8n 2.x, an error workflow that is not published never runs, so alerts would have been
  lost without any error. A small spike found this before the workflows were written.
- The evaluation's budget check assumed a prompt costs at most one token per two characters.
  The first real run showed every request using more: the answer's schema and the API's own
  framing are tokens too. The bound now counts every byte sent plus an allowance, and each
  real report lists any call that goes over it.
- A NUL character in the model's answer failed the job instead of sending the lead to review,
  because PostgreSQL text cannot hold NUL. As a JSON escape it passed validation and broke
  the insert; as a raw character it broke the call record, so not even the call's cost was
  kept. A control character other than a tab or a line break now makes the output invalid,
  and the call record shows a NUL as `␀`. The same check then found that drafts dropped some
  control characters silently and the intake API answered a NUL with a 500; drafts, human
  edits and API input now follow the same rule.

## Deployment

The stack runs in a Linux VM on the same Mac, apart from the development Docker (Lima, Ubuntu
24.04). Nothing outside the VM can reach it: no port is forwarded and no folder is shared. The
VM runs the images built on the Mac, checked by digest; only settings differ. The database
password is generated in the VM and kept in a root-only file outside git. Migrations run on
every deploy, the api's health check includes the database, killed processes restart, and one
request can be followed through the logs to the sent mail. A worker killed in the middle of a
send left its job leased; once the lease ran out, the restarted worker finished it, and the
customer got one mail. There is no public URL and no
authentication: keeping the VM unreachable stands in for it, and a host that others can reach
would need authentication first.

## Measured result

From the two reports of 2026-09-27 (`make eval`, prompt `qualify-v1`, 30 synthetic inquiries
across five kinds, labeled by one person and frozen before the runs; output limit 2048 tokens,
one run per model):

| Model | Agreement with the person | Latency p50 / p95 | Cost per lead |
|---|---|---|---|
| Claude Sonnet 5 | 40.0% (12 of 30) | 4.4 s / 8.3 s | $0.0045 |
| Claude Haiku 4.5 | 43.3% (13 of 30) | 2.4 s / 4.6 s | $0.0011 |

Agreement by kind of inquiry (Sonnet 5 / Haiku 4.5, of 6 each): clear good 5 / 5, clear bad
3 / 3, missing data 3 / 3, borderline 1 / 2, conflicting data 0 / 0. Every answer passed the
output checks, and no call failed. The disagreements lean one way: the model rated the lead
lower than the person did in 17 of Sonnet 5's 18 disagreements and 15 of Haiku 4.5's 17. On
this set the cheaper model agreed about as often, answered in about half the time, and cost a
quarter as much. Thirty inquiries and one person's labels cannot rank the models; they show
where to look next.

These numbers cover qualification only. Drafting, and the flow from inquiry to sent mail, have
only run with the fake provider.

## What I would improve next

- Authentication for the review page and the internal endpoints, then deployment.
- Push events from Python to n8n (or through a queue) instead of polling, when volume grows.
- A mail provider that accepts an idempotency key, which closes the double-send window.
- A real enrichment source in place of the stub, and a CRM integration.
- A larger evaluation (more inquiries, more than one person's labels, repeated runs),
  starting with the inquiries that carry conflicting data, where the model and the person
  disagreed most.
