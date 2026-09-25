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
every active agent. `chat.post_to_team(conn, author_id, body)` posts there on a
member's behalf (the PM's standup, HR's check); it is audited and skips the
agent rate limit.

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
- Network: chat adds `message` edges (count and last message) to the 3D view.
