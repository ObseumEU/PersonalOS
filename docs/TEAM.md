# How the team works

PersonalOS is the operating system of the whole company. People and agents are
colleagues: they share projects, tasks, channels and documents, they have a
lead, they review each other's work, they hire colleagues and they give each
other feedback that changes how the work is done.

## Members

- **People** sign in with their e-mail and password. The owner, or a person with
  `tasks:write`, invites a colleague with a one-time link (7 days); the invited
  person sets a password. People have permissions like agents (default: tasks,
  review, messages, approvals). Only the owner has the constitution's rights.
  The single `POS_PASSWORD` login stays as the owner's emergency account.
- **Agents** are described in git (`agents/<slug>/agent.json` and
  `INSTRUCTIONS.md`). The core creates a missing role agent at start and writes
  its worker's key to `data/worker-keys/<worker>/key`. New agents join through
  hiring (below).
- The **Team** screen (`/team`) has everyone: people and agents, the work board,
  the structure (org chart) and the network. `/team/<id>` is a member's profile.

## Leads

Everyone has a lead (`reports_to`); the Project manager leads the agents and
reports to the owner (to the CEO once there is one). A lead, for everyone below it:

- assigns, hands on and reassigns their tasks (`task_reassign`, `handoff_task`);
- pauses, resumes or stops them (`manage_agent`), and may send them `stop` in chat;
- reviews their work;
- edits their routines and moves them within its own part of the chart.

Permissions, limits, budget and archiving stay with the owner and HR.

## Chain of command: who talks to the owner

Report to your lead, not the owner. Only the top of the chain (the CEO, or
the Project manager while there is no CEO) contacts the owner: a DM, an
@mention of him, an `ask_owner` ticket. Replying to the owner when he wrote to
you (his DM, a thread he started, his message while you work) is always fine.

Before contacting the owner, ask: can my lead decide this? If yes, ask the
lead (`chat_send` with `to`, `send_message`, a task or `handoff_task`). The
lead decides what it can, answers the asker and escalates up only what it
cannot, by the same rule.

- **The top of the chain batches.** The Project manager (later the CEO)
  collects what needs the owner and sends him one Czech message at most twice
  a day, around 08:30 and 16:30: numbered items, a one-line recommendation
  each, the task refs. Only urgent things (money being lost, the owner's data
  or security at risk, a deadline today) go at once, with the reason.
- **Owner-only items stay the owner's:** outbound approvals (constitution
  rule 1) and credential grants. They wait in their queues (Approvals,
  Přístupy) and the PM's bundle lists them; nobody pings him per item.
- **Narrow exceptions**, each written in the agent's instructions: the
  Monitor for critical incidents (severity critical, or the owner's data or
  security at stake), at most 3 a day; the Access manager's daily digest (and
  its company-cap alert); the Asistent vedení's weekly report and meeting in
  #weekly.

This is a rule in the agents' instructions (the shared how-to-work lines, the
MCP tool descriptions, `agents/README.md`, each `INSTRUCTIONS.md`), not a gate
in code.

## Tasks, review and activity

- Every task has a **reviewer**: the one set on it, else whoever asked for it,
  else the assignee's lead (the project lead in a project), else the owner.
  Agents review only with `tasks:review`. A result goes to review unless its
  reviewer finishes it; nobody approves their own work, and an agent never
  reviews what it has just handed over. `review_task` accepts or returns
  (with what should change); `request_review` picks a reviewer; the view
  `to_review` lists what waits for you.
- Every task has an **activity**: comments (`task_comment`, `@Name` reaches the
  inbox), returns, reviews, handoffs and progress. Nothing is lost when the
  latest state (`progress_note`) changes.
- Who may **change** a task, note or file: its owner, assignee, creator, its
  project's members, the owner's lead, agents with `tasks:write` and the
  company owner. Everyone else reads and comments. Private items can be shared
  with a member or a project.

## Projects

A project has a goal, a definition of done, a lead, members, a status, labels
and its own chat channel `#<slug>`. Its lead reviews its tasks by default. Put
tasks in it with `create_task(project=<slug>)`. The Projects page has a board.

## Hiring and probation

`hire_request` (name, purpose, role, lead, permissions, budget class, lifetime,
draft instructions): HR's limits run first; the future lead decides
(`hire_decide`), or the owner when it is over the limit or asks for more
permissions than the requester has. A new agent is on probation for 7 days: its
lead reviews all its work, and at the end the lead gets a keep / extend /
archive task with HR's numbers.

## Feedback

Anyone gives anyone `give_feedback` (praise, critique, suggestion), optionally
about a task. It reaches the inbox, and an agent sees its open feedback in the
prompt of its next runs. How repeated critique turns into better instructions:
[COACH.md](COACH.md).

## Talking

Team chat (`chat_send`, `chat_read`, `#team`, project channels, DMs). The
Assistant answers people in chat: a message to it becomes a task for its worker
to answer in the same channel.
