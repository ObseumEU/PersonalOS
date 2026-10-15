# Datová schránka (ISDS), read only

T-638, design note #42. Nothing from the authorities should slip past the owner: every received data message
becomes a task for him with the message and its attachments.

## Flow

1. Every 30 minutes the job `isds_poll` (Automations, `pos.isds.poll`) lists the received messages of the
   last 30 days (`GetListOfReceivedMessages`, `https://ws1.mojedatovaschranka.cz/DS/dx`).
2. A message not yet in `isds_messages` is downloaded (`SignedMessageDownload` + `MessageDownload`,
   `/DS/dz`): the signed message `DZ_<dmID>.zfo` and each attachment become **private** files (never pushed
   into knowlage, Ú6), tag `datovka`.
3. A private task "Datovka: <věc> (<odesílatel>)" for the owner, priority 1, with links to the files, and a
   DM from PersonalOS, which reaches his phone (pos.push).
4. A download that fails 3 times still gets its task, without files and with the reason (for example a
   message "do vlastních rukou" the reading user may not open).

It never sends, deletes or marks anything. Sending a data message is a legal act: later, and only through
`request_outbound` kind=commitment with the owner's approval.

## Delivery by login

Logging in delivers a message (doručení přihlášením), for the delegated user just as for the owner. The
connector therefore delivers every message at most 30 minutes after it arrives, and deadlines run from then.

## Access (owner, once)

1. In the data box (mojedatovaschranka.cz → Nastavení → Oprávněné osoby) add a delegated user (pověřená
   osoba) for PersonalOS with only: read received messages (without "do vlastních rukou"), lists and
   delivery receipts. No sending, deleting or administration. Login by name and password.
2. Store the login on svr03 (the value never leaves the server and is never printed):

   ```
   printf %s "$HESLO" | docker exec -i personalos-api-1 python -m pos.credentials put datovka-read \
       --basic-user <uživatelské jméno>
   ```

3. Register `datovka-read` (Settings → Credentials): reference `pos://datovka-read/authorization`, header
   `Authorization: Basic {value}`, host `ws1.mojedatovaschranka.cz`, tool `http`. No agent grant is needed:
   only the platform uses it (pos.credentials.platform_header, each use logged without the value).

Until the credential is registered the job answers `skipped` and calls nothing.
