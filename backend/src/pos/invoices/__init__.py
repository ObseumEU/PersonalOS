"""Invoice filing for the CFO: invoices from both Gmail mailboxes go to the owner's Google Drive.

- `classify`: pure rules (is it an invoice, business or personal, the date, the file name);
- `drive`: the folder conventions (year / Obseum s.r.o. / Doklady / month) and the path resolution;
- `gapi`: the only Google client (Gmail read-only, Drive read + drive.file create; no update, no delete);
- `service`: `file_invoice`, the ledger, the poll job and the MCP tools.

docs/INVOICES.md describes the flow and the access.
"""
