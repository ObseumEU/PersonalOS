/**
 * The settings pages (/automations, /connectors, /system, /tools) show names the platform keeps in
 * English for its own code: routine names ("Morning brief"), routing rules ("Invoice e-mail → CFO"),
 * schedules ("weekly fri 15:00"), result keys ("pushed: 0"). The owner reads them in Czech; the raw
 * key stays in the tooltip. Unknown names are returned as they are.
 */

/** Platform routines by their action key. */
export const JOB_CS: Record<string, string> = {
  morning_brief: "Ranní přehled",
  follow_ups: "Připomenout, na co se čeká",
  weekly_review: "Příprava týdenní revize",
  nightly_retrospective: "Noční retrospektiva",
  budget_check: "Kontrola rozpočtu",
  a2a_sync: "Předávání úkolů vzdáleným agentům (A2A)",
  reap_runs: "Uvolnit běhy workerů, které zmlkly",
  member_schedules: "Plány lidí a agentů",
  claude_selfcheck: "Kontrola, že Claude odpovídá správným modelem",
  knowlage_files: "Nahrát soubory do znalostní báze (znovu, co selhalo)",
  feedback_digest: "Opakovaná kritika agenta → Performance Coach",
  probation_review: "Konec zkušební doby → rozhodne vedoucí",
  access_expire: "Přístupy: ukončit dočasná oprávnění a navýšení",
  access_watch: "Přístupy: skoky v útratě a firemní strop",
  access_digest: "Přístupy: denní souhrn pro tebe",
  access_weekly: "Přístupy: týdenní revize rozpočtů (Access manager)",
  weekly_report: "Týdenní report firmy a porada (Chief of Staff)",
  weekly_meeting_timeouts: "Týdenní porada: uzavřít po 24 h bez odpovědi",
  sentinel_watch: "Sentinel: upozornit, když přestane hlásit",
  sentinel_digest: "Sentinel: denní souhrn zdraví do #team",
  grafana_watch: "Grafana: upozornit, když přestane odpovídat",
  agents_watch: "Agenti: běžící workery a odpovědi na tvoje zprávy",
  routines_overdue: "Rutiny: upozornit na zpoždění přes hodinu",
  review_sla: "Kontroly: po 24 h vedoucímu kontrolora, tvoje CEO",
  idle_agents: "Agenti bez práce 7 dní → CEO",
  github_triage: "GitHub: nové issues a PR ve firemních repozitářích → roztřídit",
  weekly_publish_overdue: "Týdenní report: zveřejnit koncept, který nikdo nezveřejnil",
  outbound_digest: "Odchozí: denní kontrola všeho odeslaného (CEO)",
  invoices_poll: "Faktury: nové faktury z pošty na Google Drive (CFO)",
  meetings_tick: "Porady: časové limity a rozpočet",
  projects_weekly: "Projekty: týdenní stav, milníky a zaseklé projekty (COO)",
  support_intake: "Zákazníci: nová pošta → požadavky, koncepty odpovědí, připomínky",
  learning_weekly: "Poučení týdne → Performance Coach",
  memories_ensure: "Každý agent má poznámku s pamětí",
  ceo_business_focus: "CEO: byznysové zaměření týdne (náklady, nevytížení agenti)",
  heads_sweep: "Vedoucí: zaseklá práce týmu (ráno)",
  heads_sweep_afternoon: "Vedoucí: zaseklá práce týmu (odpoledne)",
  heads_second_line: "COO: zaseklá práce napříč týmy",
  owner_decision_defaults: "Tvoje rozhodnutí: po lhůtě platí doporučení",
  promises_tick: "Sliby CEO: zachytit a hlídat termíny",
  outbound_drafts: "Odchozí: koncepty, které jsi odeslal nebo zahodil",
  outbound_replies: "Odchozí: odpovědi na to, co jsme poslali",
  scorecard_daily: "Přehled firmy: měřené cíle a denní snímek",
};

export const jobName = (action: string, name: string) => JOB_CS[action] ?? name;

const RULES: [RegExp, string][] = [
  [/^GitHub issue labelled (\S+) → (.+)$/, "Issue na GitHubu se štítkem $1 → $2"],
  [/^GitHub review request → (.+)$/, "Žádost o revizi na GitHubu → $1"],
  [/^GitHub issue or pull request \(company repos\) → (.+?)( triage)?$/, "Issue nebo pull request ve firemních repozitářích → $1"],
  [/^Invoice e-mail → payment task for (.+)$/, "E-mail s fakturou → platba v $1"],
  [/^Invoice e-mail → (.+)$/, "E-mail s fakturou → $1"],
  [/^Lead or opportunity e-mail → (.+)$/, "Poptávka nebo obchodní příležitost → $1"],
  [/^New e-mail → (.+?)( triage)?$/, "Nový e-mail → $1"],
  [/^Discord mention or question → (.+)$/, "Zmínka nebo dotaz na Discordu → $1"],
  [/^Sentinel incident → (.+)$/, "Incident ze Sentinelu → $1"],
  [/^Grafana alert → (.+)$/, "Upozornění z Grafany → $1"],
];

/** A routing rule's name in Czech ("Invoice e-mail → CFO" -> "E-mail s fakturou → CFO"). */
export function ruleName(name: string): string {
  for (const [re, out] of RULES) if (re.test(name)) return name.replace(re, out);
  return name;
}

const DAYS: Record<string, string> = { mon: "pondělí", tue: "úterý", wed: "středu", thu: "čtvrtek", fri: "pátek", sat: "sobotu", sun: "neděli" };

/** "weekdays 07:00" -> "pracovní dny 07:00", "every 60m" -> "každých 60 min", "weekly fri 15:00" -> "každý pátek 15:00". */
export function scheduleCs(spec: string): string {
  const s = (spec ?? "").trim();
  let m = s.match(/^every (\d+)\s*m$/);
  if (m) return m[1] === "1" ? "každou minutu" : `každých ${m[1]} min`;
  m = s.match(/^every (\d+)\s*h$/);
  if (m) return m[1] === "1" ? "každou hodinu" : `každých ${m[1]} h`;
  m = s.match(/^daily (\d\d:\d\d)$/);
  if (m) return `denně ${m[1]}`;
  m = s.match(/^weekdays (\d\d:\d\d)$/);
  if (m) return `pracovní dny ${m[1]}`;
  m = s.match(/^weekly (\w{3}) (\d\d:\d\d)$/);
  if (m && DAYS[m[1]]) return `každ${m[1] === "wed" || m[1] === "sat" || m[1] === "sun" ? "ou" : "ý"} ${DAYS[m[1]]} ${m[2]}`;
  return s;
}

/** Result keys of routines ("pushed: 0 · failed: 0"). */
export const RESULT_KEYS: Record<string, string> = {
  level: "stav",
  pushed: "nahráno",
  failed: "selhalo",
  agents: "agenti",
  ended: "skončilo",
  expired: "vypršelo",
  sent: "odesláno",
  changes: "změny",
  seen: "viděno",
  checked: "zkontrolováno",
  items: "položek",
  to: "příjemců",
  runs: "běhy",
  runs_failed: "selhané běhy",
  returned: "vráceno",
  interventions: "zásahy",
  events_unrouted: "nezařazené události",
  tasks_done: "hotové úkoly",
  owner_edits_after_ai: "tvoje úpravy po AI",
  goals_updated: "aktualizované cíle",
  day: "den",
  projects: "projekty",
  summaries: "shrnutí",
  milestones: "milníky",
  feedback: "zpětná vazba",
  repeated: "opakované",
  business_share: "podíl byznysu",
  meeting: "porada",
  ok: "v pořádku",
};

/** Environment variables are said in words; the name itself goes to the tooltip. */
export const ENV_CS: Record<string, string> = {
  POS_GITHUB_TOKEN: "přístup ke GitHubu",
  POS_DISCORD_WEBHOOK_URL: "webhook Discordu",
  POS_GITHUB_WEBHOOK_SECRET: "tajný klíč webhooku GitHubu",
  POS_EVENTS_TOKENS: "klíče pro strojové události",
  POS_SMTP_HOST: "poštovní server (SMTP)",
};

export function envWords(envs: string): string {
  const names = envs.split(/,\s*/).filter(Boolean);
  if (!names.length) return "";
  if (names.some((n) => n.startsWith("POS_SMTP"))) return "poštovní server (SMTP)";
  return names.map((n) => ENV_CS[n] ?? n.replace(/^POS_/, "").toLowerCase().replace(/_/g, " ")).join(", ");
}
