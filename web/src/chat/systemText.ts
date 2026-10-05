/**
 * The platform's notices to agents are in English (agents read them), but the owner reads the same
 * messages in chat: "Owner resolved your ask T-737 '…': … Continue T-662." Shown to him, the known
 * notices are said in Czech. Unknown text is returned as it is.
 */
const who = (w: string) => (w === "Owner" ? "Majitel" : w);

const RULES: [RegExp, (m: RegExpMatchArray) => string][] = [
  [
    /^(.+?) resolved your ask (T-\d+) '([^']*)'(?::\s*([\s\S]*?))?(?:\s*Continue (T-\d+)\.)?\s*$/,
    (m) => {
      const answer = (m[4] ?? "").replace(/\s+[—–-]\s+I'?m$/, "").trim();
      const said = answer ? `: ${answer}${/[.!?…]$/.test(answer) ? "" : "."}` : ".";
      return `${who(m[1])} odpověděl na dotaz ${m[2]} „${m[3]}“${said}${m[5] ? ` Pokračuj na ${m[5]}.` : ""}`;
    },
  ],
  [
    /^(.+?) handed in (T-\d+) '([^']*)' for your review\.?[\s\S]*$/,
    (m) => `${who(m[1])} předal ${m[2]} „${m[3]}“ ke kontrole.`,
  ],
  [
    /^Review digest: (\d+) result\(s\) wait for your review\.[\s\S]*$/,
    (m) => `Souhrn ke kontrole: ${m[1]} ${Number(m[1]) === 1 ? "výsledek čeká" : Number(m[1]) < 5 ? "výsledky čekají" : "výsledků čeká"} na tvou kontrolu (úkoly „Review: T-…“ ve frontě).`,
  ],
  [
    /^These tasks were with the owner; agents gave them to him\. They are yours now[^:]*:\s*([\s\S]*)$/,
    (m) => `Tyhle úkoly skončily u majitele, dali mu je agenti. Teď jsou tvoje (přečti si jeho komentáře):\n${m[1]}`,
  ],
];

export function systemCs(body: string | null | undefined): string {
  const text = body ?? "";
  for (const [re, fn] of RULES) {
    const m = text.match(re);
    if (m) return fn(m);
  }
  return text;
}
