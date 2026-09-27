# Invoice filing (CFO → Google Drive)

The owner's order (2026-09-27): invoices from both mailboxes go to his Google Drive, business ones into the
company's accounting folder, the rest into "Osobní". Only what relates to Obseum, even remotely, is business.

## Flow

1. Every 5 minutes the job `invoices_poll` (Automations) reads new mail with attachments in
   david.rosko@obseum.cz and rosko.dav@gmail.com (Gmail search, not knowlage's events: those skip the
   "Updates" category where most receipts land).
2. `pos.invoices.classify` decides whether it is an invoice (a PDF/ISDOC/XML document plus invoice words,
   an invoice label, or the body) and suggests business or personal with a confidence.
3. Sure → filed at once on the CFO's behalf. Unsure → a task "Zařadit fakturu: …" for the CFO, who calls
   `invoice_file(message_id, classification, reason, attachment, account)`.
4. Each filing: a row in `invoice_filings` (Drive link, classification, reason, source mail, sha256), an
   audit entry `invoice_file`, and one line in the note "Faktury na Disku <YYYY-MM>" (topic finance).

Folders (by the invoice's issue date, else the day it came):
- business: `Obseum Ucetnictvi/<year>/Obseum s.r.o./Doklady/<NN_Month>` (existing `09_Zari`; a missing
  month is created in the siblings' convention, e.g. `10_Říjen`);
- personal: `Osobni` (year/month subfolders only if they ever appear there).

File names: the original attachment name (the folder's convention; the accountant numbers files later),
or `YYYY-MM-DD Dodavatel Číslo.pdf` when the original is generic (`invoice.pdf`).

## Safety (in code)

- Create only: `pos.invoices.gapi` has no update/move/delete and refuses any HTTP method but GET and POST.
- Only inside the two roots: every upload and folder creation walks the target's Drive parents to the root
  id (`drive.guard`); a business file never lands inside `Osobni`, which lies inside the business root.
- One invoice, one file: the same bytes (sha256) are filed once (both mailboxes), and a file already in the
  folder with the same md5 or name (without the `2611-143-` prefix) is recorded as a duplicate.

## Access (tokens in the api's `.env`, never shown)

| Variable | Scope | Account |
|---|---|---|
| `POS_GOOGLE_CLIENT_ID`, `POS_GOOGLE_CLIENT_SECRET` | knowlage's Google OAuth client | |
| `POS_GMAIL_TOKEN_DAVID_ROSKO_OBSEUM_CZ`, `POS_GMAIL_TOKEN_ROSKO_DAV_GMAIL_COM` | `gmail.readonly` (knowlage's tokens) | each mailbox |
| `POS_GDRIVE_READ_TOKEN` | `drive.readonly` (knowlage's token) | david.rosko@obseum.cz |
| `POS_GDRIVE_FILE_TOKEN` | `drive.file` | david.rosko@obseum.cz |

Both folders belong to david.rosko@obseum.cz. `drive.file` can create files and folders inside his
existing folders (tested 2026-09-27) but cannot see or change anything it did not create; listing the
folders and checking duplicates uses the read-only token. The broad `drive` scope is not needed.

Operator commands (in the api container): `python -m pos.invoices status`,
`python -m pos.invoices backfill --since 2026-09-01 [--dry-run]`,
`python -m pos.invoices file <message_id> business|personal "<reason>" --account <mailbox> [--attachment <part>]`.
