/*
 * @mention suggestions in a composer (the desktop chat and the phone app): what is being typed
 * after "@", who fits (members of the channel first; in a group anyone, since mentioning an agent
 * invites it), and the text with the chosen name put in. Pure functions (tests/mentions.test.ts).
 */

type Named = { id: number; name: string };

/** The part typed after "@" right before the caret, or null when the caret is not in a mention. */
export function mentionQuery(value: string, caret: number = value.length): string | null {
  const m = /(^|\s)@([^\s@]{0,24})$/.exec(value.slice(0, caret));
  return m ? m[2] : null;
}

/** Who fits the query: the channel's members first, then names that start with it, at most `limit`. */
export function mentionCandidates<M extends Named>(
  query: string | null,
  members: M[],
  channel: { kind: string; members: { id: number }[] },
  limit = 6,
): M[] {
  if (query === null) return [];
  const q = query.toLowerCase();
  const inChannel = new Set(channel.members.map((m) => m.id));
  return members
    .filter((m) => m.name.toLowerCase().includes(q) && (channel.kind === "group" || inChannel.has(m.id)))
    .sort(
      (a, b) =>
        Number(inChannel.has(b.id)) - Number(inChannel.has(a.id)) ||
        Number(!b.name.toLowerCase().startsWith(q)) - Number(!a.name.toLowerCase().startsWith(q)),
    )
    .slice(0, limit);
}

/** The text with the mention before the caret completed to "@Name ", and where the caret goes. */
export function completeMention(value: string, caret: number, name: string): { value: string; caret: number } {
  const before = value.slice(0, caret).replace(/@([^\s@]{0,24})$/, `@${name} `);
  return { value: before + value.slice(caret), caret: before.length };
}
