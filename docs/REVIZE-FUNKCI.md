# PersonalOS: revize všech funkcí

Stav kódu: `ObseumEU/PersonalOS` main `0e43a5b` (2026-09-25 21:00).
Zadání majitele (2026-09-25 21:05): doimplementovat všechny funkce a u každé
rozhodnout, jestli ji nechat, upravit, předělat, nebo odstranit.

## 0. Vize, podle které soudím

PersonalOS je operační systém celé firmy. Lidé i agenti v něm jsou kolegové,
kteří:

1. **sdílejí** projekty, úkoly, kanály a dokumenty;
2. **mají vedoucího**, který jim přiděluje práci a smí je řídit;
3. **si navzájem kontrolují práci** (review není jen výsada majitele);
4. **nabírají** nové kolegy (člověk pozve člověka, kdokoli s právem založí agenta);
5. **dávají si zpětnou vazbu**, která se propíše do toho, jak kolega pracuje;
6. **komunikují** stejně jako lidé (chat, vlákna, zmínky, předávky).

Test pro každou funkci: *Dělá z agenta kolegu, nebo z něj dělá nástroj majitele?*

### Hlavní zjištění

Datový model už je na tým připravený (tabulka `actors` pro lidi i agenty,
`owner_id`, `created_by`, `assignee_id` u záznamů, org chart, chat). Ale
**vrstva oprávnění a web jsou postavené na jednom člověku**:

- přihlášení je jedno heslo a každý požadavek z webu je „majitel“
  (`auth.py:36`, `api_tasks.py:28-30`); druhého člověka nejde vytvořit;
- review, zásah, pozastavení, změna org chartu a schvalování smí jen
  „člověk“ nebo „majitel“, ne vedoucí; `reports_to` nedává žádnou autoritu;
- agenti si práci nekontrolují (`tasks.py:306` „jen lidé hodnotí práci agentů“);
- zpětná vazba nemá kam jít: úkol nemá komentáře, poznámka k vrácení se
  přepíše dalším `report_progress` (`tasks.py:372`);
- část agentů nemůže dělat, co jim instrukce říkají (allowlisty v
  docker-compose blokují chat, Dev agent pod Claude nemá shell, Windows cesty).

Proto návrh níže nejdřív opravuje rozbité věci (vlna 0), pak přidává
„kolegiální“ jádro (vlna 1: komentáře, review mezi kolegy, autorita vedoucího,
zpětná vazba, nábor, projekty, agenti jako kód), pak více lidí a sjednocení
obrazovek (vlna 2) a nakonec úklid (vlna 3).

## 1. Verdikty v jedné tabulce

| # | Funkce / obrazovka | Verdikt | Proč, v jedné větě |
|---|---|---|---|
| 1 | Přihlášení (Login, `auth.py`) | **Předělat** | Jedno heslo = jeden člověk; firma potřebuje účty a pozvánky. |
| 2 | Agents (adresář) + detail | **Upravit** | Z „agentů“ udělat adresář **Tým** pro lidi i agenty; opravit statistiky a drahé GET. |
| 3 | Založení agenta, limity (`create_agent`, HR admit) | **Upravit** | Zaobalit do náboru se schválením vedoucím a zkušební dobou; opravit schválení limitu. |
| 4 | Archivace / obnova agenta | **Upravit** | Dvě kopie logiky, HR obnova vrací staré klíče, otevřená práce zůstane viset. |
| 5 | Org + Project manager | **Upravit** | Nechat, ale `reports_to` musí dávat autoritu; Org stránka editovatelná. |
| 6 | `handoff_task` | **Nechat** | Funguje, jen smazat mrtvý kód. |
| 7 | Daily standup | **Nechat + opravit** | Dobrý rituál; agenti na něj nemohou odpovědět kvůli allowlistům. |
| 8 | Network 3D | **Upravit** | Hezké, ale vedlejší; přesunout jako záložku do Tým/Org, skrýt archivované. |
| 9 | Board | **Předělat** | Jen čtecí a mimo navigaci; stát se záložkou „Práce“ v Tým s akcemi. |
| 10 | Approvals | **Upravit** | Sjednotit s publikací nástrojů, doplnit chybějící handlery. |
| 11 | Chat | **Nechat + dotáhnout** | Přesně podle vize; odstranit duplicitu s „Inject“ panelem, kanál pro projekty. |
| 12 | HR agent | **Upravit** | Metriky dobré; nesmí archivovat role agenty, GET nesmí spouštět revizi, UI chybí. |
| 13 | Agent coach | **Dokončit** | Není založený, nemá práva, nikdo nečte jeho data; stane se nositelem zpětné vazby. |
| 14 | Rozpočtář | **Upravit** | Počítá jen Codex; sjednotit s Claude, přidat přehled na System. |
| 15 | Knihovna nástrojů | **Nechat + dotáhnout** | Sdílení nástrojů mezi kolegy sedí; HR má číst `tool_usage`, schvalování do Approvals. |
| 16 | Rutiny (`schedules`) + systémové joby (`scheduler`) | **Upravit** | Dva plánovače na jedné stránce, duplicitní budget smyčka, bug v `level`. |
| 17 | Worker runtime | **Upravit** | Allowlist nástrojů musí vycházet z oprávnění agenta, ne z compose. |
| 18 | Tasks (jádro, pohledy, capture, inbox) | **Upravit** | Silné jádro; chybí komentáře, „moje“, reviewer, kroky v Today; obcházení review. |
| 19 | Review práce (stav `review`) | **Předělat** | Rozšířit na review kýmkoli určeným (i agentem), s historií a stavem „žádám změny“. |
| 20 | Projekty a kroky | **Předělat** | Z „úkolu s kroky“ udělat sdílený projekt: vedoucí, členové, kanál, DoD, stav. |
| 21 | Topics | **Upravit** | Podle rozhodnutí o knowlage jsou to **štítky**, ne sila; stránka tématu zůstává jako filtr. |
| 22 | Notes | **Nechat** | Funguje; doplnit MCP `note_list/get`. |
| 23 | Files | **Upravit** | Hledání se stěhuje do knowlage (jádro už dělá); opravit „restore“, MCP upload/list. |
| 24 | Calendar | **Nechat** | Čtecí ICS stačí; kalendář na člena později. Kapacita dne z kalendáře místo mocku. |
| 25 | Today | **Upravit** | Stát se „Můj den“ přihlášeného člena; zaškrtnutí nesmí obejít review. |
| 26 | Assistant (stránka) | **Předělat** | Je to jen proxy do knowlage bez paměti; asistent má být kolega v chatu. |
| 27 | Knowledge graf (FIG. 1/3/4) | **Nechat** | Hotové a skutečné. |
| 28 | Viditelnost (3 vrstvy) | **Dokončit** | Chybí oprávnění k zápisu, sdílení je mrtvý kód, tři endpointy bez kontroly. |
| 29 | Verzování + audit | **Upravit** | Restore smí přepsat `owner_id`/`visibility` bez kontroly. |
| 30 | Connectors + směrování | **Dokončit** | Gmail události nikdo neposílá; pravidla nejdou editovat; `calendar/nexus/web` jsou prázdné. |
| 31 | Mail agent | **Dokončit** | Závisí na událostech z knowlage (háček níže). |
| 32 | Dev agent | **Opravit** | Windows cesty v testech, bez shellu pod Claude. |
| 33 | Community agent | **Odložit (vypnout)** | Bez Discord MCP nic nedělá; nechat instrukce, vyřadit z výchozí compose. |
| 34 | Pravidlo „faktura → Nexus“ | **Vypnout do napojení** | Nexus A2A URL je prázdná, úkoly by visely navždy. |
| 35 | Odchozí akce (`outbound`) | **Upravit** | Guard zná akce bez providerů; `register_outbound_action` mrtvý. |
| 36 | A2A server/klient | **Nechat + opravit** | Úkol se zasekne ve „working“, karta se stahuje při každém volání. |
| 37 | MCP server `pos` | **Upravit** | Doplnit nástroje pro review, komentáře, zpětnou vazbu, nábor, pravidla; zrušit duplicity. |
| 38 | Self-deploy | **Opravit** | Promote mode nasazuje rovnou do produkce bez návratu; ignoruje kill switch. |
| 39 | Kill switch | **Nechat** | Doplnit respektování v deployeru. |
| 40 | Guard (ústava) | **Dokončit** | `check-command` nikdo nevolá; napojit přes Claude hook. |
| 41 | Browser use | **Dokončit** | Worker image nemá Chromium; chybí `POS_TASK_ID`. |
| 42 | System stránka | **Upravit** | Verze natvrdo, přidat rozpočet; „API online“ udělat skutečné. |
| 43 | ComingSoon | **Odstranit** | Žádná sekce na něj nevede. |
| 44 | Týdenní revize (GTD) | **Upravit** | Jen MCP prompt; přidat jednoduchou webovou revizi pro každého člena. |

## 2. Vlna 0: opravy rozbitého (malé, hned)

Každá položka je samostatná změna s testem.

| ID | Co | Kde | Hotovo když |
|---|---|---|---|
| F1 | Zaškrtnutí/Complete u úkolu ve stavu `review` nesmí dát `done`; jde přes Accept | `Tasks.tsx:150`, `Today.tsx:~81`, `TaskDetail.tsx`, `tasks.complete` | `complete` na `review` úkolu od člověka = `review(accept)`; test |
| F2 | HR obnova agenta musí vydat nový klíč a staré nechat zrušené; jedna implementace archivace | `hr/platform.py:113,134` → volat `agents.archive/restore` | HR restore volá `agents.restore`; test na revokovaný klíč |
| F3 | Archivace agenta zruší běžící běhy a jeho otevřené úkoly předá vedoucímu (`reports_to`) s poznámkou | `agents.archive` | Po archivaci žádný úkol nevisí na archivovaném |
| F4 | Handler pro schválení `raise_agent_limit`: zapíše nový limit do tabulky `settings` (verzovaně), `HRPolicy` čte odtud | `approvals._on_approved`, `hr/policy.py` | Schválení zvedne limit, test |
| F5 | HR REPLACE nesmí archivovat dřív, než založení projde (transakce) | `hr/limits.py`, `agents.create_agent` | Při chybě založení nikdo archivovaný |
| F6 | HR nearchivuje agenty definované v `agents/` (role agenti), jen navrhne úkol vedoucímu | `hr/review.py` | Flag `seeded=True` = jako system pro archivaci |
| F7 | GET `/api/agents` a `/{id}` nespouští HR dry-run ani commit; skóre se čte z posledního uloženého výsledku revize | `api_agents.py:_hr` | GET je čistě čtecí |
| F8 | „Tento týden“ statistiky počítat jen za týden | `agents.py:488-492` | Test |
| F9 | Tokeny všude = Codex + Claude (`budget_store` + `engine_usage`) jednou funkcí | `agents.py`, `network.py`, `hr/platform.py` | Stejné číslo na Agents, Network a HR |
| F10 | Allowlist nástrojů workeru se odvodí z oprávnění agenta (`/api/worker/me` vrátí seznam), compose proměnné jen zužují | `worker/pos_worker/__main__.py`, `docker-compose.yml` | Dev agent i coach umí `chat_send`/`check_inbox`, standup dostane odpovědi |
| F11 | Dev agent: Linux cesty v instrukcích (`backend/.venv/bin/python` nebo `python -m pytest`); pod Claude povolit `Bash` (s hookem z F16) | `agents/dev-agent/INSTRUCTIONS.md`, `claude.py` | Dev agent na Claude projde „fetch, test, commit“ |
| F12 | A2A: odpověď bez tasku i zprávy = úkol vrátit do fronty, ne `working` bez `remote_task_id`; cache karty 10 min; audit commit | `a2a.py:118,165,250-264,306` | Test |
| F13 | Scheduler: `report["level"]` místo `getattr`; smazat `integrations.budget_loop` (job `budget_check` stačí); popisek výjimek kill switche | `scheduler.py:143,258`, `integrations.py:133`, `Automations.tsx:45` | Jeden budget běh za hodinu |
| F14 | Self-deploy promote: před `up` otagovat běžící image, při selhání deploy/health vrátit tag a znovu `up`; deployer respektuje kill switch | `selfdeploy.py:201-263` | Test simulovaného selhání health |
| F15 | Endpointy `/api/audit`, `/api/actors`, `/api/runs` s ctx a filtrem viditelnosti; restore a `rollback_run` kontrolují změnu viditelnosti; `owner_id` a `visibility` mezi zmrazenými sloupci při restore, pokud je nemění vlastník | `api_tasks.py:198-211`, `versioning.py:28` | Testy |
| F16 | Guard příkazů skutečně napojit: Claude worker dostane PreToolUse hook na `Bash`, který volá `/api/worker/check-command`; „needs owner“ = úkol a odmítnutí | `worker/pos_worker/claude.py` | Test hooku |
| F17 | Browser: Playwright Chromium do worker image, `POS_TASK_ID` a `BROWSER_APPROVAL_WAIT` do env MCP | `worker/Dockerfile`, `__main__.py:80,111` | Headless běh v kontejneru |
| F18 | Connectors UI zobrazí `mail_prefilter` počty | `Connectors.tsx:31` | Vidět přeskočené maily |
| F19 | Pravidlo „faktura → Nexus“ výchozí vypnuté, dokud `POS_NEXUS_A2A_URL` není nastavené (a samo se zapne, když je) | `routing.py:32` | Žádné visící úkoly |
| F20 | Prod compose: logging i pro agent-coach a project-manager | `deploy/prod/docker-compose.prod.yml` | |
| F21 | Mrtvý kód pryč: `ComingSoon` a `BUILT`, nevyužitý `run` v `org.handoff`, `register_outbound_action` (nebo ho použít v F35) | `App.tsx:28,75`, `org.py:265`, `outbound.py` | |

### Stav vlny 0

- F1: hotovo, 3aa337b
- F2, F3: hotovo, 028c1f6
- F4: hotovo, db81463
- F5, F6: hotovo, 7cd6491
- F7, F8, F9: hotovo, 04db74e (+ testy a42b09b)
- F10: hotovo, 50c7b53
- F11, F16: hotovo, 53b3253
- F12: hotovo, e31cad2
- F13: hotovo, d86c3d8
- F14: hotovo, 44d83fe (návrat na poslední dobrou verzi přestavbou z main; tagování image nebylo potřeba)
- F15: hotovo (tento commit)
- F17: hotovo, 884e033 (headless Chromium ověřen v kontejneru)
- F18: hotovo, 97c02c4
- F19: hotovo, f2dd491
- F20, F21: hotovo, 30e7284 (`register_outbound_action` se používá: outbound registruje své akce)

## 3. Vlna 1: kolegiální jádro

### 3.1 Aktivita a komentáře u úkolu (základ zpětné vazby)

- Tabulka `task_comments` (id, task_id, author_id, body markdown, kind
  `comment|return|review|handoff|system`, created_at, archived_at), verzovaná.
- Vrácení, review, předávka a `report_progress` zapisují záznam do aktivity;
  `progress_note` zůstává jen jako „poslední stav“, nic se neztratí.
- Každý úkol má svoje vlákno v chatu? Ne: komentáře u úkolu stačí, chat
  zobrazí odkaz `T-123` (existuje). Jeden zdroj pravdy = aktivita úkolu.
- Zmínka `@jméno` v komentáři doručí `chat_inbox` položku (jako v chatu).
- API: `GET/POST /api/tasks/{id}/comments`; MCP: `task_comment(task, body)`,
  `get_task` vrací posledních 20 záznamů aktivity.
- UI: TaskDetail má pod poli časovou osu aktivity a pole komentáře; vrácení
  přes dialog místo `window.prompt`.

Stav: hotovo, b775fca.

### 3.2 Review mezi kolegy

- Pole úkolu `reviewer_id` (volitelné). Výchozí reviewer podle pořadí:
  zadaný při založení → tvůrce úkolu → vedoucí řešitele (`reports_to`) → majitel.
- `complete` od kohokoli, kdo není reviewer, dá stav `review` (i od člověka,
  když má úkol reviewera jiného než sebe).
- `review(task, verdict=accept|changes, comment)` smí reviewer, vedoucí
  řešitele a majitel; agent potřebuje právo `tasks:review`.
- `changes` = stav `next`, `returned_count += 1`, komentář typu `return`.
- Ochrana: nikdo neschvaluje vlastní práci; agent nesmí reviewovat úkol, který
  vznikl z jeho vlastní předávky v posledním kroku (proti kruhu).
- MCP: `review_task`, `request_review(task, reviewer)`; `list_tasks` s pohledem
  `to_review` (čeká na mě jako reviewera).
- UI: Today a Tým mají „Čeká na tvé review“; TaskDetail tlačítka Schválit /
  Vrátit s komentářem.
- HR metriky: kvalita = podíl přijatých review od kohokoli, ne jen majitele.

Stav: hotovo (commit „Wave 1 / 3.2“). Reviewer = zadaný → kdo úkol zadal → vedoucí
řešitele → majitel; agent jen s `tasks:review` (PM ho má). UI: pole REVIEWER a
Accept/Return jen pro toho, kdo smí.

### 3.3 Autorita vedoucího (org chart dává práva)

- Nová funkce `org.manages(conn, manager_id, member_id)` (tranzitivně po
  `reports_to`).
- Vedoucí (člověk i agent) smí u svých podřízených: přidělit a předat úkol,
  pozastavit a obnovit, review, poslat `stop`, upravit rutiny, navrhnout
  změnu instrukcí. Nesmí: oprávnění, limity, rozpočet, archivaci (ty zůstávají
  majiteli / HR podle ústavy).
- `stop` v chatu smí jen vedoucí příjemce, majitel, nebo člověk (oprava
  současné díry, kdy ho pošle kdokoli s `messages:send`).
- Změnu `reports_to`, role a týmu smí majitel a vedoucí nad oběma (přesun
  v rámci svého podstromu).
- Org stránka: přetahování v hierarchii pro toho, kdo smí.

### 3.4 Zpětná vazba a kritika, která mění práci

- Tabulka `feedback` (id, from_id, to_id, task_id?, kind
  `praise|critique|suggestion`, body, rating 1–5?, status
  `open|applied|dismissed`, applied_ref), verzovaná a auditovaná.
- Dát zpětnou vazbu smí každý člen komukoli (to je ta „kritika“ z vize);
  MCP `give_feedback(to, body, task?, kind)`, UI tlačítko u úkolu i na profilu.
- Příjemce ji dostane do inboxu jako `fyi`; agent-worker ji má v promptu při
  dalším běhu („Poslední zpětná vazba pro tebe“, max 5 otevřených).
- **Agent coach** je vlastník procesu: denně seskupí otevřenou kritiku po
  agentech; opakující se vzor (≥ 2) promění v návrh změny instrukcí
  (dnes úkol pro Dev agenta), zbytek označí `applied/dismissed` s důvodem.
- Instrukce agenta jdou upravit i z UI (AgentDetail): editor uloží commit do
  `agents/<slug>/INSTRUCTIONS.md` přes stejnou cestu jako Dev agent
  (merge do main + deployer kontrola). Kdo smí: majitel, vedoucí agenta, coach.
- Profil člena ukazuje „Zpětná vazba“ (přijatá a daná) a co se z ní změnilo.

Stav: hotovo (commit „Wave 1 / 3.4“). Úprava instrukcí z UI jde jako úkol pro Dev agenta
(commit na agent/dev → deployer), ne přímým zápisem z API.

### 3.5 Nábor (hiring) a zkušební doba

- Jeden tok pro založení kolegy: `hire_request` (kdo žádá, role, účel,
  vedoucí, oprávnění, rozpočtová třída, životnost, návrh instrukcí).
- Schvaluje vedoucí budoucího kolegy (když žádá agent a vedoucím je PM, schválí
  PM; nad limit nebo s oprávněním nad žadatelem jde do Approvals majiteli).
  HR `admit` běží před schválením a jeho verdikt (`reuse/defer/...`) se ukáže
  v žádosti.
- Po schválení se agent založí (`create_agent`), dostane vedoucího, klíč,
  složku `agents/<slug>/` s instrukcemi a spustí se jeho worker (viz 3.7).
- **Zkušební doba** 7 dní: všechna jeho práce má reviewera = vedoucí; HR na
  konci doporučí „ponechat / prodloužit / archivovat“ jako úkol vedoucímu.
- Lidé: stejný tok s `kind=human` = pozvánka (vlna 2, 4.1).
- MCP: `hire_request`, `hire_decide`; UI: tlačítko „Nabrat kolegu“ v Tým.

Stav: hotovo (commit „Wave 1 / 3.5“) pro agenty; lidé přes pozvánky ve vlně 2.
Spuštění workeru nového agenta přijde s 3.7.

### 3.6 Projekty jako sdílená práce

- Tabulka `projects` (id, slug, name, goal, definition_of_done, lead_id,
  status `active|paused|done|archived`, visibility, labels, channel_id,
  due), členové v `project_members` (člověk i agent, role `lead|member`).
- Úkol má `project_id`; dnešní „projekt = úkol s kroky“ se zmigruje
  (kořenové úkoly s kroky → projekt, kroky → úkoly projektu).
- Založení projektu vytvoří chat kanál `#<slug>` s členy.
- Stránka projektu: cíl a DoD, členové, board (fronta / pracuje / review /
  hotovo) s přetahováním, aktivita, dokumenty (odkazy do Files/knowlage).
- PM přiděluje v rámci projektu podle rolí; vedoucí projektu dělá review
  výchozím způsobem.
- MCP: `project_list`, `project_get`, `project_create`, `project_add_member`.
- Topics zůstávají štítky napříč (viz 21), projekt může mít více štítků.

Stav: hotovo (commit „Wave 1 / 3.6“). Kroky zůstávají (parent_id) a patří do projektu
svého úkolu; stránka Projekty s boardem a přetahováním.

### 3.7 Agenti jako kód (samo-založení role agentů)

- Každý `agents/<slug>/` dostane `agent.json`: name, role, team, reports_to,
  permissions, budget_class, runtime, lifetime, výchozí rutiny.
- Jádro při startu zajistí (idempotentně) aktéry podle těchto souborů:
  Dev agent, Mail agent, Agent coach, Project manager, (Community vypnutý).
  Tím zmizí „coach musí založit majitel ručně“.
- Klíče pro workery generuje jádro samo a zapíše do `.env` na serveru (podle
  pravidla „vše dělají thready samy“).
- HR a Rozpočtář dostanou `agent.json` s `worker: none` (běží v jádře), ať je
  seznam úplný. HR úkoly bez coache jdou coachovi (který teď vždy existuje).

## 4. Vlna 2: víc lidí a sjednocené obrazovky

### 4.1 Účty pro lidi

- Tabulka `human_accounts` (actor_id, email, password_hash argon2, disabled),
  session nese `actor_id` místo „owner“; `get_ctx` vrací přihlášeného.
- Pozvánka: majitel nebo vedoucí vytvoří odkaz (jednorázový token, 7 dní);
  pozvaný si nastaví heslo. Budoucí SSO (Google Workspace) je rozšíření.
- Majitel zůstává jediný s právy ústavy (`is_owner`). Ostatní lidé mají
  oprávnění jako agenti (`permissions`), výchozí `tasks:*`, `messages:send`,
  `approvals:request`.
- Všude, kde se píše „jen člověk“, platí nově „člověk s právem“ nebo
  „vedoucí“ (3.3). `@me` = přihlášený, ne majitel.
- Úkoly založené agentem: `owner_id` = kdo práci zadal (vedoucí / tvůrce
  nadřazeného úkolu), ne automaticky majitel.
- Výchozí přihlášení s jedním heslem zůstane jako nouzový účet majitele.

### 4.2 Obrazovka Tým (sloučení Agents + Board + Org + Network)

- `/team` se záložkami: **Lidé a agenti** (adresář, karta člena: role,
  vedoucí, stav, aktuální úkol, skóre), **Práce** (dnešní Board s akcemi:
  přidělit, předat, pozastavit), **Struktura** (Org, editovatelná),
  **Síť** (3D, bez archivovaných).
- Profil člena (dnešní AgentDetail) pro lidi i agenty: fronta, review,
  zpětná vazba, rutiny, výkon; agentům navíc běh, instrukce (editor), engine,
  klíče, paměť.
- Staré cesty `/agents`, `/board`, `/org`, `/network` přesměrovat.
- Duplicitní panel „Inject/Messages“ na profilu nahradit odkazem na DM
  a polem priority zprávy, které píše do chatu (jeden systém).

### 4.3 Asistent jako kolega

- Stránka Assistant zanikne; ask box na Today zůstane (rychlý dotaz do
  knowlage s citacemi).
- „Assistant“ je agent s workerem (existuje v compose), mluví se s ním v DM
  v chatu, má MCP `pos` + knowlage (`ask_agent` Knowledge agent), takže umí
  odpovídat i o úkolech, projektech a souborech a zakládat úkoly. Konverzace
  se neztrácí (je to chat).

### 4.4 Můj den (Today) a osobní pohledy

- Today je pro přihlášeného člena: moje úkoly (včetně kroků s dnešním
  `do_date`), čeká na moje review, moje schválení, můj kalendář, zmínky.
- Pohledy úkolů dostanou filtr „Moje / Můj tým / Vše“.
- Kapacita dne = pracovní doba minus události z kalendáře (odpadne mock 6 h).
- Webová týdenní revize: jedna stránka s kroky GTD (inbox na nulu, waiting,
  someday, projekty bez dalšího kroku); agenti ji mají dál jako prompt.

### 4.5 Rozpočet a rutiny v UI

- System: panel Rozpočet (obě předplatná, stav ok/watch/throttle/pause,
  stropy tříd, změna třídy agenta).
- Automations: jeden seznam „Rutiny“ (systémové joby a rutiny členů se
  sloupcem Vlastník), stejné ovládání.
- `/api/system` verze z balíčku, „API online“ z živého health checku.

### 4.6 Konektory a pošta

- **Háček od knowlage (vlastní thread „Knowlage: server a konektory“):** po
  načtení nového e-mailu pošle knowlage `POST /api/events` (source `gmail`,
  `ref` = message id, `meta.headers`, label, odkaz na dokument v knowlage).
  PersonalOS pak spustí prefilter a směrování na Mail agenta.
- Pravidla směrování editovatelná v UI (všechna pole), MCP `route_update`
  pro HR/coach/PM (verzované, auditované).
- Zdroje `calendar`, `nexus`, `web` v UI skrýt, dokud je nic neposílá.
- Community agent vyřadit z výchozí compose profilu `agents` (profil
  `community`), dokud nebude Discord MCP.
- Odchozí akce: guard zná `github.issue/review`, `web.post`, `payment`;
  doplnit provider `github.issue` a `github.review` (GitHub token existuje),
  `payment` a `web.post` vždy jen jako úkol majiteli.

### 4.7 Soubory a viditelnost

- Files: hledání přes knowlage dělá jádro (probíhá). Doplnit: `restore`
  přejmenovat na `unarchive` a přidat skutečný restore verze; MCP
  `file_list`, `file_upload` (agenti sdílí dokumenty jako kolegové).
- Sdílení: API a UI pro `visibility.share` (sdílet soukromou položku
  s konkrétním členem nebo projektem).
- Zápis: upravit/archivovat smí vlastník, řešitel, členové projektu,
  vedoucí vlastníka a majitel; ostatní jen číst (a komentovat).
- Témata: `topics` dostanou viditelnost; přejmenování a sloučení štítku.

## 5. Vlna 3: úklid a drobnosti

- Smazat ComingSoon, `BUILT` v `App.tsx`, mrtvé `share()` jen pokud 4.7
  neproběhne.
- MCP duplicity: `send_message/check_inbox/ack_message` nechat jako aliasy
  chatu, v instrukcích a promptu workeru uvádět jen `chat_*`.
- HR texty do stejného jazyka jako UI (UI anglicky, obsah úkolů česky
  zůstává).
- `runs.tool_calls` a `turns` zobrazit v profilu agenta (coach je používá).
- HR čte `tool_usage` (které sdílené nástroje pomáhají).
- Dokument `docs/COACH.md` a `docs/TEAM.md` (jak funguje tým: role,
  vedoucí, review, zpětná vazba, nábor).

## 6. Co zůstává beze změny

Kill switch, ústava a guard (kromě F16), verzování a audit (kromě F15),
chat, handoff, knihovna nástrojů, znalostní graf, notes, kalendář (čtecí),
MCP i A2A jako protokoly. Tyto části vizi splňují.

## 7. Výchozí volby, které jsem zvolil

| Otázka | Volba |
|---|---|
| Kdo je výchozí reviewer | zadaný → tvůrce → vedoucí řešitele → majitel |
| Zkušební doba nového agenta | 7 dní, review vedoucím |
| Kdo smí dát kritiku | kdokoli komukoli, zpracovává coach |
| Kdo smí měnit instrukce z UI | majitel, vedoucí agenta, coach (přes git a deployer) |
| Autorita vedoucího | úkoly, pauza, stop, review, rutiny; ne oprávnění, limity, rozpočet |
| Přihlášení lidí | e-mail + heslo s pozvánkou; SSO později |
| Assistant | kolega v chatu; samostatná stránka zaniká |
| Topics | štítky (shodně s knowlage), projekty jsou samostatné |
| Community agent | vypnutý do napojení Discordu |
