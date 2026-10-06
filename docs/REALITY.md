# Grounded in reality ("Co je živé")

The owner, 2026-10-06: marketing prepared e-mails saying "you can try it here, here's the link" while nothing
that could be tried existed; tasks were reported done with their parts dropped; agents told him "DNS is
missing" when it was not. The fix is a principle, enforced by the platform, not a rule agents may forget:
**nothing is claimed, linked, promised or reported as done unless reality backs it.**

## 1. The registry: what is live (pos.reality)

Per project, each capability or user-journey step: `status` (live, unverified, test_only, mock, missing),
`url`, `access` (public, password, internal), `aliases`, a probe, `verified_at` and the evidence.

- **Only a verification makes a capability live**: the platform's anonymous probe (`probe_mode=live`: a
  2xx page without password or login, showing `probe_expect`) or a reviewer accepting evidence
  (`reality_submit_evidence` → `reality_verify` by someone else with tasks:review, or the owner; the URL must
  pass the probe at that moment). Agents can declare everything else (`reality_upsert`), never live.
- **Verification expires** after 72 h (`POS_REALITY_EXPIRY_HOURS`) without a passing check: the capability
  reads, and is written down as, `unverified`. A failing probe reverts it at once.
- `probe_mode=protected` (a test instance): an outsider must NOT get in; if it opens without a password the
  check fails and `reality_exposed` is recorded.
- `probe_mode=test` (a capability deployed on a password-protected test instance): the platform opens
  `probe_url` with a registered credential (`probe_credential`, e.g. `kniha-test-basic-auth`; put into that
  one request by `pos.credentials.platform_header`, the use logged as the platform's, the value never) and
  anonymously. A passing authenticated fetch (2xx, `probe_expect`), and, with `deploy_ref`, a deployed
  version that contains that commit (the deployer's status file, `POS_DEPLOY_STATUS`), make it **test_only
  and verified** (expiring like live). It is never live; an instance that opens without a password is
  flagged. Only a person or an agent holding the credential may name it. Without a usable credential (its
  1Password item missing), a capability with `deploy_ref` and no `probe_expect` is verified by its
  deployment: the instance answers `/healthz` and runs a version containing the commit (method `deploy`).
- The job `reality_tick` (every 30 min) runs the probes, the expiry and the release of held tasks.
- Agents read it with `reality_list`; the owner sees "Co je živé" on the project page (with the content the
  gate stopped, and his override button).

## 2. The claim gate (pos.grounding)

Before anything outbound or owner-facing is created, queued or offered to the owner: request_outbound
(email.send, linkedin.post, discord.post, web.post), gmail_create_draft, gmail_update_draft,
request_approval, ask_owner, and a blocking chat question to the owner.

1. Every URL is fetched as an anonymous outsider. Our domains (`POS_OWN_DOMAINS`, default obseum.cz, plus
   registry hosts) need a clean 2xx; a third-party site fails only on DNS, 404/410/5xx, a password or login.
   A test/mock/admin host or path, or a registry capability that is not live and public, always blocks.
   Owner-facing items check only links the registry knows.
2. A sentence with a call to action that names a capability that is not live blocks deterministically.
3. When the text makes product claims and a registry applies, one claude-haiku-4-5 call (`claim_check`
   run, cost logged in `runs` and `grounding_checks.llm_cost_usd`, cached 24 h per content and registry)
   lists claims the registry does not back; a finding counts only with a literal quote.
4. Blocked: a Czech reason back to the agent, nothing reaches the owner, `claim_ungrounded` audit line
   (improve-loop category `grounding`). Agents can dry-run with `reality_check`.

Escape hatch, the owner's only: POST `/api/reality/checks/{id}/override` (the button on the project page)
lets the same content through for 72 h. People are never gated. `POS_GROUNDING=0` switches the gate off.

## 3. Sequencing

An agent's marketing / outreach / content task (role growth, growth_sales, content, marketing_lead,
community; a promotion topic or title) in a project with a registry that names a capability that is not live
is created `waiting` with a note and a `reality_holds` row. Agents cannot claim or reopen it; it goes back to
`next` (agent woken) once every capability it names is live. The owner moving it releases it.

## 4. Done means delivered (pos.delivery)

The definition of done is split into criteria (lines, bullets, numbers, else `;`). An agent's hand-in or an
agent reviewer's accept needs evidence per criterion (`K1: …` lines or `criteria_evidence` on
complete_task; criteria without their own line share the result, which must then hold one piece of
evidence per criterion), verified by pos.evidence; a criterion about a URL, a form or being live needs a URL
that passes the outsider probe. Only work agents and people create is held to its criteria (source mcp,
api, ...); platform-made tasks (chat answers, deploy reviews, customer issues) keep their own rules. A parent with open steps, or steps archived without a
reason, cannot be handed in or accepted. Refusals record `delivery_incomplete`. `POS_DELIVERY_GATE=0`
switches it off.

## 5. Grounded blockers

A message, ask or approval to the owner that says something is blocked or missing is checked before
delivery: DNS (the host resolves → false), a URL "down" (it answers 2xx → false), a credential "missing" (it
is in the registry → false); any other blocker needs a verifiable reason (an error, a check, an HTTP code, a
task). Refusals record `blocker_unfounded`.

## Not covered (deliberately)

- The Kniha web is published by merging to its `production` branch (kniha-deployer), not through a
  PersonalOS tool, so the gate cannot stop a page; the landing page's probes show what it actually serves.
- GitHub comments, issues and PRs are developer collaboration and are not gated.
