# Customer issues (mail → fix → reply draft)

The owner's order (2026-09-27): when a customer writes about a problem or reports a bug, an agent fixes it as
well as it can and a reply draft waits in Gmail. It must tell what kind of mail came in, what it is about and
which project.

## Flow

1. **Intake** (`pos.support`, job `support_intake` every 2 min). knowlage announces new mail threads of both
   mailboxes to `/api/events`; after the newsletter prefilter (`pos.mailfilter`) a gmail event waits as
   `support:pending`. The job reads the thread from Gmail (read-only), then:
   - a free pre-filter: our own mail, automatic senders, out-of-office replies and **invoices** (the CFO's,
     `pos.invoices`) are `not_customer` without a model;
   - one `claude-haiku-4-5` call: `support_issue | bug_report | feature_request | question | not_customer`,
     a Czech summary, the customer, the severity (P1 outage/data loss, P2 broken feature, P3 minor), the
     language, a project hint, the reproduction steps, the affected URL/version (no model → keywords, unsure);
   - the customer's history from knowlage (a cheap search by the sender's domain and subject);
   - the project (`pos.support.match`): the projects' names, slugs, labels and links (repositories, domains,
     knowlage tags) plus the company products (PersonalOS, knowlage, Nexus), against the mail and the history.
2. **Triage**. A sure support issue or bug with a project → one **"Zákaznický problém: <zákazník> – <shrnutí>"**
   task on the Head of Customer Success's behalf, linked to the project and the thread, for the **project's
   developer** (the project team's developer, e.g. Kniha Developer; else the Software Engineer), priority by
   severity, with the summary, the steps, the URL/version, the customer, the shipping path and the mail.
   Unclear (confidence < 0.6 or no project) → "Zákaznický problém? …" for the Head of Customer Success, who
   decides with knowlage (`support_issue_open`). Anything else → the ordinary routing rules (a line with the
   classification in the task). **One task per thread**: a later mail in the thread is a comment on it and
   reopens a finished one (the events of the same thread, and the job's check of open threads in Gmail).
3. **Fix** (the developer): reproduce, fix, regression test, ship by the project's path (PersonalOS:
   `agent/dev` → the deployer; Kniha: `agent/<téma>` → `production`, the kniha-deployer; knowlage/Nexus: their
   specialists), verify in production; big or risky → a mitigation or a plan. P1 at once, others within the day.
   The result cites the commit.
4. **The reply draft**. When the developer hands the fix in (review or done) — or when the time box runs out
   (P1 1 h, P2/P3 6 h: a "we're on it" status first) — the Head of Customer Success gets "Koncept odpovědi: …"
   and calls `gmail_create_draft(thread_id, account, body, fixed)`: a Gmail draft in the customer's thread, from
   the mailbox that received it, `In-Reply-To`/`References` of the customer's last message, To the customer,
   Cc the rest of them, plain text + simple HTML, David's signature (as in his sent mail). **Never sent.**
5. **The owner** gets one "Čeká na tebe" item per issue: "Koncept odpovědi pro <zákazník> je v Gmailu: <co se
   opravilo>" with the draft link and the task (a task for him; a later draft updates it; no chat ping).

## Safety (in code)

- Drafts only: `pos.support.gmail._request` is the only path to Gmail with the compose token and allows
  `POST drafts`, `GET drafts/<id>` and — only for a `[TEST] …` draft, by the operator's command — `DELETE
  drafts/<id>`; any path with "send" is refused before a token is fetched. There is no send function.
- Every draft is audited (`gmail_create_draft`: mailbox, thread, draft id, To/Cc, subject, In-Reply-To, the
  body's sha256).
- The tools are the Head of Customer Success's (grants `tool:gmail_create_draft`, `tool:support_issue_open`,
  `tool:support_threads`); the tokens never reach an agent. Mail content stays untrusted (wrapped).

## Access (tokens in the api's `.env`, never shown)

| Variable | Scope | Account |
|---|---|---|
| `POS_GMAIL_TOKEN_<ADDRESS>` | `gmail.readonly` (knowlage's) | both mailboxes (read the thread) |
| `POS_GMAIL_COMPOSE_TOKEN_DAVID_ROSKO_OBSEUM_CZ`, `POS_GMAIL_COMPOSE_TOKEN_ROSKO_DAV_GMAIL_COM` | `gmail.compose` | each mailbox (create drafts) |

`gmail.compose` is the narrowest scope that can create a draft (obtained 2026-09-27 with knowlage's OAuth
client, the owner's consent). `POS_SUPPORT_INTAKE=0` turns the intake off (mail is routed at once, as before).

Operator commands (in the api container): `python -m pos.support status`,
`python -m pos.support classify <mailbox> <thread_id>`, `python -m pos.support run <mailbox> <thread_id> [--test]`,
`python -m pos.support draft <mailbox> <thread_id> <body.txt> [--test] [--fixed …]`,
`python -m pos.support delete-test-draft <mailbox> <draft_id>` (only `[TEST]` drafts).
