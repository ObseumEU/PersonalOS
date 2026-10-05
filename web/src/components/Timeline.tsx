import { useEffect, useState } from "react";
import { t } from "../i18n";
export type AgendaEvent = { start: number; end: number; title: string; meta: string };

const START = 8;
const END = 18;

const hhmm = (x: number) => `${String(Math.floor(x)).padStart(2, "0")}:${String(Math.round((x % 1) * 60)).padStart(2, "0")}`;

/** Day agenda on a measured time axis (quarter-hour ticks) with a live "now" line. */
export default function Timeline({ events }: { events: AgendaEvent[] }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const h = setInterval(() => setNow(new Date()), 30_000);
    return () => clearInterval(h);
  }, []);
  const pct = (x: number) => ((x - START) / (END - START)) * 100;
  const nowH = now.getHours() + now.getMinutes() / 60;
  const ticks = Array.from({ length: (END - START) * 4 + 1 }, (_, i) => START + i / 4);

  return (
    <div className="overflow-x-auto">
      <div className="relative mr-6 ml-1 h-[112px] min-w-[720px]">
        <div className="absolute inset-x-0 top-0 h-6">
          {ticks.map((tick) => {
            const major = tick % 1 === 0;
            return (
              <div key={tick} className="absolute top-0" style={{ left: `${pct(tick)}%` }}>
                <div className={`w-px bg-ink-3 ${major ? "h-2" : "h-1"}`} />
                {/* The edge labels align inward: centred, "08:00" was cut to ":00" at the left edge. */}
                {major && (
                  <span
                    className={`absolute top-2.5 text-xs whitespace-nowrap text-ink-2 tabular-nums ${tick === START ? "" : tick === END ? "-translate-x-full" : "-translate-x-1/2"}`}
                  >
                    {hhmm(tick)}
                  </span>
                )}
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
                {e.title} <span className="text-ink-2 tabular-nums">{hhmm(e.start)}</span>
              </span>
            </div>
          );
        })}
        {nowH >= START && nowH <= END && (
          <>
            <div className="breathe absolute top-0 bottom-0 w-px bg-accent" style={{ left: `${pct(nowH)}%` }} />
            <span className="absolute top-[26px] text-xs whitespace-nowrap text-accent" style={{ left: `calc(${pct(nowH)}% + 6px)` }}>
              {t("work.timeline.now", { time: hhmm(nowH) })}
            </span>
          </>
        )}
      </div>
    </div>
  );
}
