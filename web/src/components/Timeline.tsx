import { useEffect, useState } from "react";
export type AgendaEvent = { start: number; end: number; title: string; meta: string };

const START = 8;
const END = 18;

const hhmm = (t: number) => `${String(Math.floor(t)).padStart(2, "0")}:${String(Math.round((t % 1) * 60)).padStart(2, "0")}`;

/** Day agenda on a measured time axis (quarter-hour ticks) with a live "now" line. */
export default function Timeline({ events }: { events: AgendaEvent[] }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(t);
  }, []);
  const pct = (t: number) => ((t - START) / (END - START)) * 100;
  const nowH = now.getHours() + now.getMinutes() / 60;
  const ticks = Array.from({ length: (END - START) * 4 + 1 }, (_, i) => START + i / 4);

  return (
    <div className="overflow-x-auto">
      <div className="relative mr-6 ml-1 h-[112px] min-w-[720px]">
        <div className="absolute inset-x-0 top-0 h-6">
          {ticks.map((t) => {
            const major = t % 1 === 0;
            return (
              <div key={t} className="absolute top-0" style={{ left: `${pct(t)}%` }}>
                <div className={`w-px bg-ink-3 ${major ? "h-2" : "h-1"}`} />
                {major && <span className="cap absolute top-2.5 -translate-x-1/2 text-[10px]">{hhmm(t)}</span>}
              </div>
            );
          })}
        </div>
        {events.map((e, i) => {
          const past = e.end < nowH;
          return (
            <div
              key={e.title}
              className="absolute flex flex-col gap-1"
              style={{
                left: `${pct(e.start)}%`,
                width: `${pct(e.end) - pct(e.start)}%`,
                top: 40 + (i % 2) * 34,
                opacity: past ? 0.45 : 1,
              }}
            >
              <span className={`block h-1 w-full rounded-[1px] ${i === 2 ? "bg-accent" : "bg-ink"}`} />
              <span className="text-xs whitespace-nowrap">
                {e.title} <span className="cap">{hhmm(e.start)}</span>
              </span>
            </div>
          );
        })}
        {nowH >= START && nowH <= END && (
          <>
            <div className="breathe absolute top-0 bottom-0 w-px bg-accent" style={{ left: `${pct(nowH)}%` }} />
            <span className="cap absolute top-[26px] text-accent!" style={{ left: `calc(${pct(nowH)}% + 6px)` }}>
              now {hhmm(nowH)}
            </span>
          </>
        )}
      </div>
    </div>
  );
}
