# Rozpočtář (tokeny)

Implementace kap. 4.2 z [AGENTS-SPEC.md](AGENTS-SPEC.md). Kód: `backend/src/pos/budget/`.

## Co se vlastně měří

Předplatné ChatGPT nemá měsíční počítadlo tokenů. Codex hlásí dvě klouzavá okna
limitů, každé jako „procent využito“ a čas obnovy:

- krátké okno (5 hodin),
- dlouhé okno (týden).

„Tokeny vydrží do konce období“ tedy v praxi znamená, že žádné z oken nedojde
před svou obnovou. Měsíc (nebo den obnovy předplatného) zůstává jako období
pro přehled spotřeby a pro volitelný strop, který si majitel může nastavit sám.

Zdroje dat, oba z Codexu:

| Zdroj | Co obsahuje |
|---|---|
| `codex exec --json` (stdout) | `thread.started` s id vlákna, `turn.completed` s tokeny |
| `~/.codex/sessions/…/rollout-*.jsonl` | události `token_count`: tokeny celkem a `rate_limits` (okna, % využito, obnova) |

Počítají se „účtované“ tokeny: necachovaný vstup plus výstup (stejně jako je
sčítá Codex). Rozpočtář čte i sezení, která nespustila PersonalOS (majitelův
vlastní Codex na stejném stroji), protože berou ze stejného předplatného.

## Hodinová kontrola

1. Načte nové záznamy sezení.
2. Pro každé okno spočítá tempo (průměr posledních ~15 % okna a průměru od
   začátku okna) a odhad využití v okamžiku obnovy.
3. Určí stav:

| Stav | Kdy | Co se děje |
|---|---|---|
| ok | odhad pod 85 % | nic |
| watch | odhad 85–100 % | stropy agentů se sníží na 75 % |
| throttle | odhad nad 100 % | agenti třídy `low` stojí, ostatní mají poloviční stropy, úkol pro majitele |
| pause | využito ≥ 95 % nebo Codex hlásí vyčerpaný limit | běží jen `system` agenti, úkol pro majitele |

4. Přidělí agentům denní stropy: co zbývá v týdenním okně, rozloženo do dnů
   do obnovy, bez rezervy pro majitele (30 %), rozděleno podle třídy
   (system 3 : normal 2 : low 1). Přepočet procent na tokeny se odhaduje
   z historie; dokud chybí, stropy nejsou a platí jen stavy.

Úkol pro majitele vzniká jen při zhoršení stavu, ne při každé kontrole.

## Nastavení (proměnné prostředí)

| Proměnná | Výchozí | Význam |
|---|---|---|
| `POS_BUDGET_RENEWAL_DAY` | 1 | den v měsíci, kdy se obnovuje předplatné |
| `POS_BUDGET_MONTHLY_TOKENS` | – | volitelný měsíční strop účtovaných tokenů |
| `POS_BUDGET_OWNER_RESERVE` | 0.3 | podíl nechaný pro majitelův vlastní Codex |
| `POS_BUDGET_CODEX_HOME` | `$CODEX_HOME` nebo `~/.codex` | kde Codex zapisuje sezení |
| `POS_BUDGET_CHECK_MINUTES` | 60 | interval automatické kontroly, 0 = vypnuto |

## Rozhraní

- API: `GET /api/budget`, `POST /api/budget/check`, `GET /api/budget/gate/{agent}`,
  `PUT /api/budget/agents/{agent}` (`budget_class`: system / normal / low).
- CLI: `python -m pos.budget check | status | gate AGENT | record --agent A [--task T] < exec.jsonl`.

## Zapojení do jádra (`backend/src/pos/integrations.py`)

- Runner před každým `codex exec` volá bránu `can_run` a po běhu zapisuje
  spotřebu přes `record_exec`.
- Asistent je veden jako systémový agent.
- Web aplikace spouští kontrolu každých `POS_BUDGET_CHECK_MINUTES` minut
  (výchozí 60, 0 = vypnuto). Změnu stavu zapíše do audit logu, a když se stav
  zhorší na throttle nebo pause, vytvoří úkol pro majitele (téma `rozpocet`, P1).
- Až bude plánovač v Nexusu, stačí, když bude volat `integrations.budget_check`.
- Tabulky `budget_*` si modul zakládá sám (`CREATE TABLE IF NOT EXISTS`).
