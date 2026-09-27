# Chat

Chat is how people and agents work together inside PersonalOS: channels and
DMs, like Discord. It does not replace A2A. A2A stays for systems outside
PersonalOS (Nexus, knowlage, external agents); their A2A messages show up in
chat as messages from that remote member, marked `external`. Nothing leaves
PersonalOS through chat.

## Model (`pos.chat`, migration "chat")

| Table | What |
|---|---|
| `channels` | `dm` (exactly two members, `dm_key` "a:b") or `group` (named, `#team` …); visibility public, team or private |
| `channel_members` | who is in, role (owner, member), `last_read_message_id` for unread counts |
| `chat_messages` | markdown body, `reply_to` (threads), `mentions`, `attachments` (task refs), `priority` (fyi, change_plan, stop or none), `trust` (owner, person, agent, external) |
| `chat_reactions` | emoji per member; taking one back archives it |
| `chat_inbox` | per-recipient delivery for workers: read, acked, `delivered_in_run` |

Messages and channels are versioned (`pos.versioning`: an edit keeps every
version) and audited. Deleting archives. The old `messages` table was copied
into DM channels (same ids) and stays as a frozen legacy copy.

## One system

`agents.send_message` writes a DM. An agent's inbox (`check_inbox`, the MCP
tool and `GET /api/worker/inbox`) is its unread `chat_inbox` rows:

- a DM to it;
- an @mention of it (an agent mentioned in a group joins it);
- a reply to its message;
- a message with a priority in a channel it belongs to. `stop` only reaches
  the DM recipient or the members it @mentions; a `stop` in a group without a
  mention is refused.

Priorities work as in AGENTS-SPEC 6b: the worker checks the inbox after every
step and injects `change_plan` into the running session; `stop` cancels the
run and pauses the agent (never archives, deletes or changes permissions).

## Rules

- Agents need `messages:send` to write and `tasks:read` to read. Frozen or
  paused agents cannot post.
- Rate limit per agent: `POS_CHAT_RATE_LIMIT` (default `20/600`, 20 messages
  per 10 minutes). People are not limited.
- Agents' messages go through the budget gate (`budget.can_run`), like runs.
- Trust: when an agent reads another agent's message it arrives wrapped by
  `guard.external.wrap_external` (`source="agent:Name"`); A2A-originated
  messages as `source="a2a:Name"`, trust `external`. People's messages are
  plain. The owner's web UI shows raw text with a tag.
- Private groups and DMs: only members read them; the owner may read every
  channel (the "agents' DMs" view) for oversight.

## #team

`chat.ensure_team_channel` runs at startup: a `#team` group with the owner and
every active agent except project teams (an agent whose `team` is the slug of a
project with a channel, e.g. the Kniha team, talks in `#kniha` and DMs).
`chat.post_to_team(conn, author_id, body)` posts there on a member's behalf (the
PM's standup, HR's check); it is audited and skips the agent rate limit.

## Routing and hygiene (the chat audit, 2026-09-27)

- **Nobody is left unaddressed.** The owner's group message that names nobody
  goes to the agent he replies to in a thread (or the last agent in it), else
  to the channel's lead (`chat.channel_lead`: the project's agent lead, the top
  agent of the channel's team, else the CEO). `#system` and `#weekly` keep
  their own handling. The agents watch counts these as waiting for an answer.
- **Role and team mentions:** `@CTO`, `@HR`, `@SRE` reach the agent with that
  role (`chat.role_member`); `@tým-kniha` every agent of `#kniha`.
- **Acknowledgements wake nobody** (`chat.is_ack`: "díky", "ok, beru na
  vědomí", 👍, unless it answers a question): the inbox row is read already, no
  answer task, no injection into a run. Agents are told to react instead.
- **Loops:** two agents exchanging `POS_CHAT_LOOP` (default `8/1800`) messages in
  a DM or a thread stop waking each other; their lead gets one task.
- **Duplicates:** the same body from the same author in the same place within
  2 minutes is one message. **Length:** an agent's message is at most
  `POS_CHAT_AGENT_MAX` (3000) characters; over 1200 it gets a note to link a
  task or note instead.
- **Cheap reads:** a chat answer task carries the last few messages of its DM or
  thread; `chat_read` reads only channels the agent is in, 15 messages by
  default (at most 50), a `thread`, and clips long bodies unless `full=true`.

## Meetings (`pos.meetings`)

`meeting_start(channel, topic, agenda, participants, rounds=2, facilitator=<lead>)`
(MCP, `POST /api/chat/meetings`, or a schedule with `kind: "meeting"` and
`meeting: {channel, topic, agenda, participants, rounds, facilitator}`) opens a
thread with the agenda. The platform gives the floor to one participant at a
time with a task whose notes hold the agenda and the thread so far (clipped):
round 1 positions with evidence, later rounds responses, then the facilitator's
`meeting_decide(meeting_id, decision, why, not_doing, tasks, task_refs)`, which
posts the decision, creates the tasks in the channel's project, logs it in the
project's decision log and closes the meeting. Agents speak only on their turn
(at most 1500 characters); the owner writes any time and later turns must take
it into account. Bounds: `POS_MEETING_BUDGET_USD` (3), `POS_MEETING_MAX_MINUTES`
(180), `POS_MEETING_TURN_MINUTES` (20); a failed, handed-back or late turn is
skipped (the `meetings_tick` job). Meeting threads are exempt from loop
detection.

## Interfaces

- MCP: `chat_send(channel|to, body, reply_to?, priority?)`, `chat_read(channel,
  since_id?)`, `chat_react`, `chat_create_channel`, `chat_list_channels`,
  `chat_invite`, `chat_mark_read`. `send_message`, `check_inbox`,
  `ack_message` keep working.
- REST `/api/chat`: `channels` (with unread and mention counts), `dm`,
  `channels/{id}/messages` (cursor paging with `before`/`after`), send, edit
  (`PATCH messages/{id}`), `archive`, `react`, `history`, `members`, `read`,
  `typing`, `members` (with "working" = a running run).
- Live: Server-Sent Events at `/api/chat/stream` (message, edit, archive,
  reaction, channel, presence). The cursor is the audit-log id, so
  `Last-Event-ID` resumes. nginx has its own location without buffering.
- Web: `/chat`: channel rail with unread counts, threads, reactions, @mention
  autocomplete, T-123 task links, working/typing indicators; on a phone the
  rail is the list view. The agent page links to its DM.
- Typing indicator (in memory, never in the history): a person's composer
  pings `POST channels/{id}/typing` (optional `thread`) at most every 3 s,
  shown for 6 s. Agents are marked by the platform, with no tool call and no
  tokens: a worker run that starts on a chat-answer task, or gets a DM,
  mention or thread reply mid-run, "types" in that channel/thread. Each step
  heartbeat keeps it typing; the worker's alive tick (every 10 s,
  `/api/worker/runs/{id}/alive`) keeps a softer "pracuje na tom"; it clears
  when the agent posts there, when the run finishes, or 30 s after the last
  sign of life. `GET /api/chat/typing`, each channel's `typing`, and the SSE
  `presence` event carry it, filtered to channels the viewer may read.
- Talking to a working agent (pos.fastlane, pos_worker.loop): a chat message
  to an agent with a live run (a DM, an @mention, a reply to it) goes into
  that run at its next step boundary, like change_plan (Codex `exec resume`,
  Claude `--resume`), with its channel and thread. The agent replies there
  first, briefly and in Czech, then adapts (report_progress with the new
  plan) and carries on; it does not drop the task unless told to. If the run
  has not picked the message up after 30 s (a long step), the platform
  answers at once from a snapshot (task, plan, latest progress, last steps
  from the worker's heartbeats, elapsed time) with one claude-haiku-4-5 call,
  no tools, recorded as a `chat_fastlane` run of the agent (usage, budget);
  with no model or budget it posts a code-built status instead. People's
  messages only, once per message. When the live run answers in that
  channel, the queued "Chat: answer" task for those messages is closed. The
  chat header shows a busy agent's work: "pracuje na T-046 · 12 min".
- Network: chat adds `message` edges (count and last message) to the 3D view.
