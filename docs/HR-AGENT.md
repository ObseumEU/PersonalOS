# HR agent

Kód: `backend/src/pos/hr/`, testy `backend/tests/test_hr.py`. Specifikace:
[AGENTS-SPEC.md](AGENTS-SPEC.md), kap. 3 a 4.1.

Modul je čistá logika nad dvěma rozhraními v `pos/hr/interfaces.py`, takže
nesahá do tabulek jádra. Jádro (thread „Map PersonalOS assistant state“) je
implementuje a HR agenta spouští plánovač (spec úkol 11).

## Co dělá

| Funkce | Kdy | Co vrací nebo dělá |
|---|---|---|
| `run_daily_review` | denně | ohodnotí agenty, sám archivuje a mění životnost, na slučování a úpravu instrukcí založí úkol HR agentovi |
| `decide_over_limit` | když `create_agent` narazí na limit | `reuse` / `replace` / `defer` / `ask_owner` |
| `file_weekly_report` | týdně | úkol typu „k přečtení“ pro majitele s KPI z kap. 1 |

## Pravidla (výchozí hodnoty v `HRPolicy`)

- **Skóre efektivity** 0 až 1: 60 % kvalita (hotovo a nevráceno), 40 %
  samostatnost (bez zásahu majitele), minus postih za „musel jsem hlídat“.
  Počítá se až od 3 dokončených nebo selhaných úkolů za 14 dní.
- **Archivace** (vratná, provádí sama): vypršel `expires_at`; `one_shot`
  dokončil a den nic nedělá; bez práce 14 dní a nic otevřeného; skóre pod
  0,25 po 7denní ochranné lhůtě.
- **Změna životnosti:** `one_shot`, který dostal 3 a víc úkolů, se změní na
  `long_lived`.
- **Úprava instrukcí** (úkol): skóre pod 0,5 nebo vráceno přes 30 % práce.
- **Sloučení** (úkol): účel se shoduje aspoň na 60 % (slova bez diakritiky,
  česká ohýbání přes prefix). Zůstává agent s lepším skóre.
- **Nad limit:** stejný účel už někdo má → `reuse`; denní limit zakladatele →
  `defer`; jinak archivuje nejslabšího nečinného agenta bez otevřené práce →
  `replace`; když všichni pracují → `ask_owner` (limity mění jen majitel).
- Systémové agenty (`system=True`) HR hodnotí, ale nikdy nearchivuje ani
  neslučuje.

## Co potřebuje od jádra

`HRDataSource`:
- `list_agents()` s poli z kap. 3.1 plus `status`, `last_active_at`, `system`.
- `agent_stats(agent_id, since, until)`: dokončené úkoly, z toho bez zásahu
  majitele, vrácené, selhané, otevřené, zásahy majitele a tokeny (tokeny
  dodá Rozpočtář z logu `codex exec`).

`HRActions`: `archive_agent`, `set_lifetime`, `create_task(kind="read")`.

Aby šlo „vráceno“ a „zásah majitele“ počítat, potřebuje úkol v jádře událost
nebo pole pro vrácení majitelem a pro zásah. `create_agent` nad limitem má
místo zamítnutí zavolat `decide_over_limit` (nebo založit úkol HR agentovi).
