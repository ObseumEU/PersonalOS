import { ChevronDown, ChevronRight, Presentation, Square, X } from "lucide-react";
import { useCallback, useEffect, useState, type FormEvent } from "react";
import { api } from "../api";
import type { Channel, ChatMember } from "../chatApi";
import { confirmDialog, toast } from "../components/overlay";
import { ago, label, t } from "../i18n";
import { TaskLink } from "../taskSheet";

/* Porady (pos.meetings) in the desktop chat: start one in a group channel, see the one running and the past
 * ones, follow its turns, close it. The meeting itself happens in the thread of its agenda message. */

export type Meeting = {
  id: number;
  channel_id: number;
  /** The agenda message: the meeting's thread. */
  thread: number;
  topic: string;
  agenda: string;
  participants: string[];
  facilitator: string | null;
  rounds: number;
  status: "running" | "closed";
  now_speaking: string | null;
  turns: { seq: number; round: number; kind: "position" | "response" | "decision"; actor: string | null; status: string; message_id: number | null; note: string | null; task: string | null }[];
  decision: string | null;
  close_reason: string | null;
  spent_usd: number;
  budget_usd: number;
  started_at: string;
  deadline_at: string;
  closed_at: string | null;
};

export const meetingsApi = {
  list: (channelId: number) => api<Meeting[]>(`/api/chat/meetings?channel_id=${channelId}`),
  get: (id: number) => api<Meeting>(`/api/chat/meetings/${id}`),
  start: (body: { channel: number; topic: string; agenda: string; participants: number[]; rounds: number; facilitator?: number | null }) =>
    api<Meeting>("/api/chat/meetings", { method: "POST", body: JSON.stringify(body) }),
  close: (id: number) => api<Meeting>(`/api/chat/meetings/${id}/close`, { method: "POST" }),
};

const MAX_PARTICIPANTS = 8;
const MAX_ROUNDS = 3;
const field = "rounded border border-line bg-bg px-2 py-1 text-[13px] outline-none focus:border-accent";

/** Where the meeting is now: the round of the open turn (or the last one). */
function roundOf(m: Meeting): number {
  const open = m.turns.find((x) => x.status === "open") ?? m.turns[m.turns.length - 1];
  return Math.min(open?.round ?? 1, m.rounds);
}

export function meetingState(m: Pick<Meeting, "status" | "decision">): string {
  return m.status === "running" ? t("mt.status.running") : m.decision ? t("mt.status.decided") : t("mt.status.closed");
}

async function closeMeeting(m: Meeting): Promise<Meeting | null> {
  const ok = await confirmDialog({ title: t("mt.close_ask", { topic: m.topic }), body: t("mt.close_body"), confirm: t("mt.close"), danger: true });
  if (ok === null) return null;
  try {
    const out = await meetingsApi.close(m.id);
    toast(t("mt.closed"));
    return out;
  } catch (e) {
    toast(e instanceof Error ? e.message : String(e), { error: true });
    return null;
  }
}

function StartForm({ channel, members, onStarted, onClose }: { channel: Channel; members: ChatMember[]; onStarted: (m: Meeting) => void; onClose: () => void }) {
  const inChannel = new Set(channel.members.map((m) => m.id));
  // Agents only (a meeting gives the floor to agents); the channel's own first.
  const agents = members
    .filter((m) => m.kind !== "human" && !m.archived)
    .sort((a, b) => Number(inChannel.has(b.id)) - Number(inChannel.has(a.id)) || a.name.localeCompare(b.name, "cs"));
  const [topic, setTopic] = useState("");
  const [agenda, setAgenda] = useState("");
  const [picked, setPicked] = useState<number[]>([]);
  const [rounds, setRounds] = useState(2);
  const [facilitator, setFacilitator] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const m = await meetingsApi.start({ channel: channel.id, topic, agenda, participants: picked, rounds, facilitator: facilitator ? Number(facilitator) : null });
      toast(t("mt.started", { topic: m.topic }));
      onStarted(m);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 border-b border-line bg-raised/30 px-4 py-3">
      <span className="flex items-center text-xs font-medium text-ink-2">
        {t("mt.start_title", { channel: channel.name ?? channel.title })}
        <button type="button" aria-label={t("act.close")} onClick={onClose} className="ml-auto">
          <X size={13} />
        </button>
      </span>
      <label className="flex flex-col gap-1 text-xs text-ink-2">
        {t("mt.topic")}
        <input autoFocus required value={topic} onChange={(e) => setTopic(e.target.value)} placeholder={t("mt.topic_ph")} className={field} />
      </label>
      <label className="flex flex-col gap-1 text-xs text-ink-2">
        {t("mt.agenda")}
        <textarea rows={3} value={agenda} onChange={(e) => setAgenda(e.target.value)} placeholder={t("mt.agenda_ph")} className={`${field} resize-y`} />
      </label>
      <fieldset className="flex flex-col gap-1">
        <legend className="text-xs text-ink-2">
          {t("mt.participants")} · <span className="text-ink-3">{t("mt.participants_hint", { n: MAX_PARTICIPANTS })}</span>
        </legend>
        <div className="flex max-h-32 flex-wrap gap-x-4 gap-y-0.5 overflow-y-auto">
          {agents.map((m) => {
            const i = picked.indexOf(m.id);
            return (
              <label key={m.id} className="flex items-center gap-1.5 py-0.5 text-[13px] text-ink-2">
                <input
                  type="checkbox"
                  className="accent-accent"
                  checked={i >= 0}
                  disabled={i < 0 && picked.length >= MAX_PARTICIPANTS}
                  onChange={(e) => setPicked((p) => (e.target.checked ? [...p, m.id] : p.filter((x) => x !== m.id)))}
                />
                {i >= 0 && <span className="font-mono text-[11px] text-accent">{i + 1}.</span>}
                {m.name}
              </label>
            );
          })}
        </div>
      </fieldset>
      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-ink-2">
          {t("mt.rounds")}
          <select value={rounds} onChange={(e) => setRounds(Number(e.target.value))} className={field}>
            {Array.from({ length: MAX_ROUNDS }, (_, i) => i + 1).map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-xs text-ink-2">
          {t("mt.facilitator")}
          <select value={facilitator} onChange={(e) => setFacilitator(e.target.value)} className={field}>
            <option value="">{t("mt.facilitator_auto")}</option>
            {agents.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </select>
        </label>
        <button className="btn-accent ml-auto" disabled={busy || !topic.trim() || !picked.length}>
          <Presentation size={14} /> {t("mt.submit")}
        </button>
      </div>
      {error && <span className="text-xs break-words text-red-400">{error}</span>}
    </form>
  );
}

/** The meeting's turns and outcome: in the meeting thread's panel and in the channel's list. */
export function MeetingDetail({ id, onChanged }: { id: number; onChanged?: () => void }) {
  const [m, setM] = useState<Meeting | null>(null);
  const load = useCallback(() => {
    meetingsApi.get(id).then(setM, () => setM(null));
  }, [id]);
  useEffect(load, [load]);
  // The thread moves while the meeting runs: refresh with the chat's live events.
  useEffect(() => {
    if (m?.status !== "running") return;
    const h = setInterval(load, 20000);
    return () => clearInterval(h);
  }, [m?.status, load]);
  if (!m) return null;
  return (
    <div className="mx-4 my-2 flex flex-col gap-1.5 rounded-md border border-line px-3 py-2 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className={m.status === "running" ? "text-accent" : m.decision ? "text-emerald-300" : "text-ink-2"}>{meetingState(m)}</span>
        {m.status === "running" && <span className="text-ink-2">· {t("mt.round", { n: roundOf(m), of: m.rounds })}</span>}
        {m.status === "running" && <span className="text-ink-2">· {m.now_speaking ? t("mt.speaking", { name: m.now_speaking }) : t("mt.waiting")}</span>}
        <span className="text-ink-3">· {t("mt.spent", { spent: m.spent_usd.toFixed(2), budget: m.budget_usd.toFixed(2) })}</span>
        {m.status === "running" && (
          <button
            type="button"
            className="ml-auto flex items-center gap-1 text-red-300 hover:underline"
            onClick={async () => {
              const out = await closeMeeting(m);
              if (out) {
                setM(out);
                onChanged?.();
              }
            }}
          >
            <Square size={11} /> {t("mt.close")}
          </button>
        )}
      </div>
      <span className="text-ink-2">{t("mt.who", { participants: m.participants.join(", "), facilitator: m.facilitator ?? "—" })}</span>
      {m.decision && <span className="text-ink">{t("mt.decision", { text: m.decision })}</span>}
      {!m.decision && m.close_reason && <span className="text-ink-2">{t("mt.reason", { text: m.close_reason })}</span>}
      <details>
        <summary className="cursor-pointer text-ink-2 hover:text-ink">{t("mt.turns")}</summary>
        {m.turns.length === 0 ? (
          <p className="pt-1 text-ink-3">{t("mt.no_turns")}</p>
        ) : (
          <ol className="flex flex-col gap-0.5 pt-1">
            {m.turns.map((x) => (
              <li key={x.seq} className="flex flex-wrap items-baseline gap-x-2">
                <span className="w-5 text-right font-mono text-ink-3">{x.seq}.</span>
                <span className="text-ink">{x.actor ?? "—"}</span>
                <span className="text-ink-2">{label("mt.turn", x.kind)}</span>
                <span className={x.status === "open" ? "text-accent" : x.status === "skipped" ? "text-amber-300" : "text-ink-3"}>{label("mt.turn_status", x.status)}</span>
                {x.task && <TaskLink taskRef={x.task} className="font-mono text-ink-3 hover:text-accent">{x.task}</TaskLink>}
                {x.note && <span className="text-ink-3">· {x.note}</span>}
              </li>
            ))}
          </ol>
        )}
      </details>
    </div>
  );
}

/** Under a group channel's header: the running meeting (open its thread, close it), "Svolat poradu" and the history. */
export default function MeetingsBar({ channel, members, onOpen }: { channel: Channel; members: ChatMember[]; onOpen: (thread: number) => void }) {
  const [list, setList] = useState<Meeting[] | null>(null);
  const [starting, setStarting] = useState(false);
  const [history, setHistory] = useState(false);
  const load = useCallback(() => {
    meetingsApi.list(channel.id).then(setList, () => setList([]));
  }, [channel.id]);
  useEffect(() => {
    setStarting(false);
    setHistory(false);
    load();
  }, [load]);
  // A meeting moves on its own (turns, the decision): refresh while one runs.
  const running = list?.find((m) => m.status === "running") ?? null;
  useEffect(() => {
    if (!running) return;
    const h = setInterval(load, 20000);
    return () => clearInterval(h);
  }, [running?.id, load]);
  const past = (list ?? []).filter((m) => m.status !== "running");

  return (
    <div className="border-b border-line">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-4 py-1.5 text-xs">
        {running ? (
          <>
            <Presentation size={13} className="text-accent" />
            <span className="text-accent">{t("mt.running")}</span>
            <button type="button" onClick={() => onOpen(running.thread)} className="min-w-0 truncate text-ink hover:underline">
              {running.topic}
            </button>
            <span className="text-ink-2">
              {t("mt.round", { n: roundOf(running), of: running.rounds })} · {running.now_speaking ? t("mt.speaking", { name: running.now_speaking }) : t("mt.waiting")}
            </span>
            <span className="ml-auto flex items-center gap-3">
              <button type="button" onClick={() => onOpen(running.thread)} className="text-accent hover:underline">
                {t("mt.open")}
              </button>
              <button
                type="button"
                className="flex items-center gap-1 text-red-300 hover:underline"
                onClick={async () => {
                  if (await closeMeeting(running)) load();
                }}
              >
                <Square size={11} /> {t("mt.close")}
              </button>
            </span>
          </>
        ) : (
          <button type="button" onClick={() => setStarting((s) => !s)} aria-expanded={starting} className="flex items-center gap-1.5 text-ink-2 hover:text-accent">
            <Presentation size={13} /> {t("mt.start")}
          </button>
        )}
        {past.length > 0 && (
          <button type="button" onClick={() => setHistory((h) => !h)} aria-expanded={history} className={`flex items-center gap-1 text-ink-2 hover:text-ink ${running ? "" : "ml-auto"}`}>
            {history ? <ChevronDown size={12} /> : <ChevronRight size={12} />} {t("mt.history", { n: past.length })}
          </button>
        )}
      </div>
      {starting && !running && (
        <StartForm
          channel={channel}
          members={members}
          onClose={() => setStarting(false)}
          onStarted={(m) => {
            setStarting(false);
            load();
            onOpen(m.thread);
          }}
        />
      )}
      {history && (
        <ul className="max-h-48 overflow-y-auto border-t border-line">
          {past.map((m) => (
            <li key={m.id}>
              <button type="button" onClick={() => onOpen(m.thread)} className="grid w-full grid-cols-[minmax(0,1fr)_auto] gap-3 px-4 py-1.5 text-left text-xs hover:bg-raised">
                <span className="flex min-w-0 flex-col">
                  <span className="truncate text-ink">{m.topic}</span>
                  {m.decision && <span className="truncate text-ink-2">{m.decision}</span>}
                </span>
                <span className="text-ink-2 whitespace-nowrap">
                  {meetingState(m)} · {ago(m.closed_at ?? m.started_at)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
