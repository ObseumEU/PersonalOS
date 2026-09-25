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

## Napojení na jádro

- **Data:** `pos/hr/platform.py` čte agenty z `actors` (druh `ai` a `agent`),
  statistiky z úkolů (`returned_count`, `interventions`, historie akcí
  `return` a `intervene`), selhané běhy z `runs` a tokeny od Rozpočtáře
  (`budget_runs`). Pole z kap. 3.1, která `actors` zatím nemá (účel,
  životnost, zakladatel, `expires_at`, systémový agent), drží HR ve vlastní
  tabulce `hr_profiles` (verzované přes `pos.versioning`, stejně jako archivace agentů). Až je jádro přidá do `actors`, adaptér je bude
  číst odtud.
- **HR agent** je systémový člen „HR agent“ (vzniká při startu, u Rozpočtáře
  třída `system`). Úkoly na sloučení a úpravu instrukcí dostává on.
- **Limity:** `create_agent` v jádře má před založením zavolat
  `hr.service.admit_agent(conn, ctx, name=, purpose=, lifetime=)` a po založení
  `hr.service.register_agent(conn, actor_id, purpose=, lifetime=, created_by=)`.
  Do 10 aktivních agentů se počítají jen nesystémoví. Denní limit 2 se na
  majitele nevztahuje. `ask_owner` založí položku ve frontě schválení
  (`raise_agent_limit`).
- **API** `/api/hr`: `GET` přehled se skóre, `POST /review?apply=`,
  `POST /weekly`, `PUT /agents/{id}/profile`, `POST /agents/{id}/restore`,
  `POST /admit`.
- **MCP** (server `pos`): `hr_overview`, `hr_review`, `hr_admit_agent`,
  `hr_weekly_report`. Provést revizi nebo poslat přehled smí jen majitel nebo
  HR agent.
- **Plánovač:** dokud není Nexus (spec úkol 11), běží ve webové aplikaci:
  každých 30 minut zkontroluje, jestli je po 6:00 a dnešní revize ještě
  neproběhla, v pondělí navíc pošle týdenní přehled. Vypíná se
  `POS_HR_SCHEDULER=false`, pak jde použít cron
  `30 6 * * * python -m pos.hr review` a `0 7 * * 1 python -m pos.hr weekly`.
  `python -m pos.hr status` jen vypíše přehled.
