# Tool library

Agents write small tools for themselves and share the good ones with the
team. Tools live in git, so every change is a commit the deployer checks.

| Where | Visibility | Who changes it |
|---|---|---|
| `agents/<agent-slug>/tools/<name>/` | personal | the agent, by commits on its branch |
| `shared/tools/<name>/` | team | a published copy, through the deployer and the owner |

## Kinds

- **script**: a small program the agent runs (`python <entry>`).
- **mcp**: a small stdio MCP server; the worker mounts it as an extra MCP
  server (Claude: `--mcp-config`, Codex: `-c mcp_servers.<name>.command=...`).
- **skill**: `SKILL.md` instructions, added to the agent's system prompt
  (Claude) or task prompt (Codex).

Examples in `shared/tools/`: `pr-description` (skill), `summarize-diff`
(script), `task-links` (mcp, dependency-free).

## Manifest (`tool.json`)

```json
{
  "name": "summarize-diff", "kind": "script", "description": "...",
  "owner": "Dev agent", "visibility": "team", "version": "1.0.0",
  "entry": "summarize_diff.py", "tests": "test_summarize_diff.py",
  "permissions_needed": [], "outbound": false
}
```

`name` matches the folder; `version` is semver; `entry` is a file in the
folder (`SKILL.md` for a skill); `tests` is a test file in the folder or a
command. A personal tool's `owner` is the agent whose folder it is.

## Guard review (`pos.tools.review`)

- **secrets**: private keys, cloud, GitHub, Slack, Discord and PersonalOS keys,
  hard-coded passwords and tokens;
- **outbound**: HTTP and mail clients, network commands, webhook and SMTP
  addresses, and the outbound commands of `pos.guard.commands`, unless the
  manifest says `"outbound": true`. Such a tool is never mounted as an MCP
  server, and each use needs an approved `request_approval`
  (`tools_record_use` refuses without the approval id);
- **permission escalation**: `permissions_needed` beyond what the owner has,
  platform credentials (deployer key, owner token), signing keys, `sudo`;
- **bypasses**: anything `pos.guard.commands` calls a bypass (e.g. `--no-verify`).

## Publishing

1. The agent calls `tools_publish(name)`. PersonalOS runs the review and records
   a row in `tool_publications`: `rejected` with the findings, or `pending`
   with the exact copy to make.
2. The agent copies the folder to `shared/tools/<name>/` (visibility `team`)
   and commits on its branch with `Tool-Publication: <id>`.
3. The deployer runs the constitution check **and the tools guard review on
   every commit that touches `shared/tools/`** (`pos.tools.check_range`), then
   the tests (backend tests run every shared tool's own tests), build and
   health. Any finding refuses the range.
4. The owner approves or rejects on the **Tools** page. An approved version
   found in `shared/tools/` becomes `published`; a rejected one stays listed
   but is not mounted.

## Mounting and usage

The worker asks `GET /api/worker/tools` before each run: the agent's own
personal tools plus the shared tools whose `permissions_needed` it has, minus
anything with review findings. Tool files are found under `WORKER_TOOLS_DIR`
(a PersonalOS checkout), else the work folder; a missing file is skipped.

MCP: `tools_list(scope)`, `tools_get(name)` (tasks:read), `tools_publish(name)`,
`tools_record_use(name, ok)` (tasks:claim). Usage lands in `tool_usage`;
`pos.tools.tools_usage(conn)` gives HR and the retrospective the counts per tool.

API: `GET /api/tools`, `GET /api/tools/{name}`,
`POST /api/tools/publications/{id}/decide` (owner). The API reads the repo at
`POS_TOOLS_REPO_DIR` (compose mounts `agents/` and `shared/` there).
