import { Check, ChevronDown, ChevronUp, CornerDownLeft, Delete, Lock, RotateCcw, X, ZoomIn } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { type Handoff, type HandoffEvent, handoffApi, isOpen, keyName, minutesLeft, pointAt } from "../handoffApi";
import { register, t } from "../i18n/core";
import handoffDict from "../i18n/cs/handoff";
import { refreshNeedsMe } from "../needsMeApi";
import { toast } from "./overlay";

register(handoffDict);

/** The owner in an agent's live browser (pos.handoff): the page as the agent's browser shows it, his clicks,
 * taps, scrolling and typing go into that same page, then "Hotovo" and the agent continues. Desktop and /m. */
export default function HandoffView({ id, mobile = false, onClose }: { id: number; mobile?: boolean; onClose?: () => void }) {
  const [h, setH] = useState<Handoff | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [src, setSrc] = useState<string | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 1280, h: 800 });
  const [busy, setBusy] = useState(false);
  const [keep, setKeep] = useState(true);
  const [zoom, setZoom] = useState(false);
  const [, tick] = useState(0);
  const img = useRef<HTMLImageElement>(null);
  const box = useRef<HTMLDivElement>(null);
  // Desktop typing goes through a hidden text field: keys, IME, dictation, paste and autofill all arrive there.
  const kbd = useRef<HTMLTextAreaElement>(null);
  const queue = useRef<HandoffEvent[]>([]);
  const textBuf = useRef("");
  const timer = useRef<number | null>(null);
  const lastMove = useRef(0);
  const open = h ? isOpen(h.status) : false;

  // Load, and tell PersonalOS the owner opened it (audited: who, when, from which app).
  useEffect(() => {
    let alive = true;
    handoffApi
      .open(id, mobile ? "m" : "web")
      .then((x) => alive && setH(x))
      .catch((e) => alive && setErr(e instanceof Error ? e.message : String(e)));
    const poll = window.setInterval(() => {
      handoffApi.get(id).then((x) => alive && setH(x), () => undefined);
      tick((n) => n + 1);
    }, 10000);
    return () => {
      alive = false;
      window.clearInterval(poll);
    };
  }, [id, mobile]);

  // The frames: long-poll for a newer picture while the handoff is open (kept in memory on the server only).
  useEffect(() => {
    if (!open) return;
    let alive = true;
    let version = 0;
    let url: string | null = null;
    const loop = async () => {
      while (alive) {
        try {
          const r = await fetch(`/api/handoffs/${id}/frame?after=${version}&wait=8`, { credentials: "same-origin", cache: "no-store" });
          if (r.status === 200) {
            version = Number(r.headers.get("X-Frame-Version") || version);
            const w = Number(r.headers.get("X-Frame-Width") || 0);
            const hh = Number(r.headers.get("X-Frame-Height") || 0);
            if (w && hh) setSize({ w, h: hh });
            const next = URL.createObjectURL(await r.blob());
            if (url) URL.revokeObjectURL(url);
            url = next;
            if (alive) setSrc(next);
          } else if (r.status === 204) {
            const st = r.headers.get("X-Handoff-Status");
            if (st && st !== "waiting" && st !== "active") {
              handoffApi.get(id).then((x) => alive && setH(x), () => undefined);
              return;
            }
          } else {
            await new Promise((ok) => setTimeout(ok, 2000));
          }
        } catch {
          await new Promise((ok) => setTimeout(ok, 2000));
        }
      }
    };
    void loop();
    return () => {
      alive = false;
      if (url) URL.revokeObjectURL(url);
    };
  }, [id, open]);

  const flush = useCallback(() => {
    if (timer.current) window.clearTimeout(timer.current);
    timer.current = null;
    if (textBuf.current) {
      queue.current.push({ t: "text", text: textBuf.current });
      textBuf.current = "";
    }
    const events = queue.current;
    queue.current = [];
    if (events.length) handoffApi.input(id, events).catch((e) => toast(e instanceof Error ? e.message : String(e), { error: true }));
  }, [id]);

  const send = useCallback(
    (e: HandoffEvent, now = false) => {
      if (textBuf.current && e.t !== "text") {
        queue.current.push({ t: "text", text: textBuf.current });
        textBuf.current = "";
      }
      queue.current.push(e);
      if (now) flush();
      else if (!timer.current) timer.current = window.setTimeout(flush, 60);
    },
    [flush],
  );

  const typeText = useCallback(
    (s: string) => {
      textBuf.current += s;
      if (!timer.current) timer.current = window.setTimeout(flush, 80);
    },
    [flush],
  );

  // Touch scrolling on the picture scrolls the agent's page (not this one).
  useEffect(() => {
    const el = img.current;
    if (!el || !open) return;
    let y0: number | null = null;
    let x0 = 0;
    let acc = 0;
    const start = (e: TouchEvent) => {
      if (e.touches.length !== 1) return;
      y0 = e.touches[0].clientY;
      x0 = e.touches[0].clientX;
      acc = 0;
    };
    const move = (e: TouchEvent) => {
      if (y0 === null || e.touches.length !== 1) return;
      const dy = y0 - e.touches[0].clientY;
      const dx = x0 - e.touches[0].clientX;
      if (Math.abs(dy) < 6 && Math.abs(dx) < 6) return;
      e.preventDefault();
      const scale = size.h / (el.getBoundingClientRect().height || 1);
      acc += dy * scale;
      y0 = e.touches[0].clientY;
      x0 = e.touches[0].clientX;
      if (Math.abs(acc) > 40) {
        send({ t: "wheel", dx: 0, dy: Math.round(acc) });
        acc = 0;
      }
    };
    const end = () => (y0 = null);
    el.addEventListener("touchstart", start, { passive: true });
    el.addEventListener("touchmove", move, { passive: false });
    el.addEventListener("touchend", end);
    return () => {
      el.removeEventListener("touchstart", start);
      el.removeEventListener("touchmove", move);
      el.removeEventListener("touchend", end);
    };
  }, [open, send, size.h, src]);

  const at = (e: { clientX: number; clientY: number }) => pointAt(e.clientX, e.clientY, img.current!.getBoundingClientRect());

  const finish = async (how: "done" | "cancel" | "resume" | "dismiss") => {
    setBusy(true);
    flush();
    try {
      const x =
        how === "done" ? await handoffApi.done(id, keep) : how === "resume" ? await handoffApi.resume(id) : await handoffApi.cancel(id);
      setH(x);
      toast(t(how === "done" ? "ho.done_ok" : how === "resume" ? "ho.resume_ok" : "ho.cancel_ok"));
      refreshNeedsMe();
      if (how !== "done") onClose?.();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(false);
    }
  };

  if (err) return <p className="p-4 text-sm text-red-300">{err}</p>;
  if (!h) return <p className="p-4 text-sm text-ink-2">{t("ho.loading")}</p>;

  const btn = mobile
    ? "flex h-11 items-center justify-center gap-1.5 rounded-lg border px-4 text-[15px] disabled:opacity-40"
    : "btn";
  const primary = mobile ? `${btn} border-accent bg-accent/10 font-medium text-accent` : "btn-accent";
  const plain = mobile ? `${btn} border-line text-ink-2` : "btn";
  const ratio = `${size.w} / ${size.h}`;

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-1 px-4 pt-3">
        <span className="text-xs text-ink-2">
          {t("ho.from", { agent: h.agent.name })}
          {h.task_ref ? ` · ${h.task_ref}` : ""}
          {open ? ` · ${t("ho.left", { n: minutesLeft(h.expires_at) })}` : ""}
        </span>
        <h1 className="text-lg leading-snug font-medium break-words">{h.title}</h1>
        {h.reason && <p className="text-[13px] break-words text-ink-2">{h.reason}</p>}
      </div>

      {open ? (
        <>
          <div className="px-4 text-xs text-ink-2">
            {(h.page.url || h.url) && <span className="block truncate font-mono">{h.page.url || h.url}</span>}
            {t(mobile ? "ho.hint_mobile" : "ho.hint_desktop")}
          </div>
          <div className={zoom ? "overflow-auto" : ""}>
            <div
              ref={box}
              aria-label={t("ho.title")}
              className="relative mx-4 overflow-hidden rounded-lg border border-line bg-black focus-within:border-accent"
              style={{ aspectRatio: ratio, width: zoom ? "200%" : undefined }}
            >
              {!mobile && (
                <textarea
                  ref={kbd}
                  aria-label={t("ho.type")}
                  autoComplete="off"
                  autoCapitalize="none"
                  autoCorrect="off"
                  spellCheck={false}
                  className="absolute top-0 left-0 h-px w-px resize-none opacity-0"
                  onKeyDown={(e) => {
                    const k = keyName(e);
                    if (!k) return; // a modifier alone, or text: it arrives as input
                    e.preventDefault();
                    send({ t: "key", key: k });
                  }}
                  onInput={(e) => {
                    const el = e.currentTarget;
                    if (el.value) typeText(el.value);
                    el.value = "";
                  }}
                />
              )}
              {src ? (
                <img
                  ref={img}
                  src={src}
                  alt={h.page.title || h.title}
                  draggable={false}
                  className="h-full w-full cursor-pointer select-none"
                  style={{ touchAction: "none" }}
                  onClick={(e) => {
                    kbd.current?.focus({ preventScroll: true });
                    send({ t: "click", ...at(e), button: "left", n: Math.min(3, Math.max(1, e.detail || 1)) }, true);
                  }}
                  onContextMenu={(e) => {
                    e.preventDefault();
                    send({ t: "click", ...at(e), button: "right" }, true);
                  }}
                  onMouseMove={(e) => {
                    const now = Date.now();
                    if (now - lastMove.current < 120) return;
                    lastMove.current = now;
                    send({ t: "move", ...at(e) });
                  }}
                  onWheel={(e) => {
                    const scale = size.h / (img.current?.getBoundingClientRect().height || 1);
                    send({ t: "wheel", dx: Math.round(e.deltaX * scale), dy: Math.round(e.deltaY * scale), ...at(e) });
                  }}
                />
              ) : (
                <p className="p-4 text-sm text-ink-2">{t("ho.no_frame")}</p>
              )}
            </div>
          </div>
          {mobile && (
            <div className="flex flex-col gap-2 px-4">
              <label className="text-xs text-ink-2" htmlFor={`ho-type-${id}`}>
                {t("ho.type")}
              </label>
              <input
                id={`ho-type-${id}`}
                autoComplete="off"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                placeholder={t("ho.type_ph")}
                className="h-11 rounded-lg border border-line bg-bg px-3 text-[15px] outline-none focus:border-accent"
                onInput={(e) => {
                  const el = e.currentTarget;
                  if (el.value) typeText(el.value);
                  el.value = "";
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    send({ t: "key", key: "Enter" }, true);
                  } else if (e.key === "Backspace" && !e.currentTarget.value) {
                    e.preventDefault();
                    send({ t: "key", key: "Backspace" }, true);
                  }
                }}
              />
              <div className="flex flex-wrap gap-2">
                <button className={plain} onClick={() => send({ t: "key", key: "Backspace" }, true)} aria-label={t("ho.key.back")}>
                  <Delete size={16} />
                </button>
                <button className={plain} onClick={() => send({ t: "key", key: "Tab" }, true)}>
                  {t("ho.key.tab")}
                </button>
                <button className={plain} onClick={() => send({ t: "key", key: "Enter" }, true)}>
                  <CornerDownLeft size={16} /> {t("ho.key.enter")}
                </button>
                <button className={plain} onClick={() => send({ t: "wheel", dx: 0, dy: -400 }, true)} aria-label={t("ho.scroll_up")}>
                  <ChevronUp size={16} />
                </button>
                <button className={plain} onClick={() => send({ t: "wheel", dx: 0, dy: 400 }, true)} aria-label={t("ho.scroll_down")}>
                  <ChevronDown size={16} />
                </button>
                <button className={plain} onClick={() => setZoom(!zoom)} aria-pressed={zoom} aria-label={t("ho.zoom")}>
                  <ZoomIn size={16} />
                </button>
              </div>
            </div>
          )}
          <label className="flex items-center gap-2 px-4 text-[13px] text-ink-2">
            <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
            {t("ho.keep_login")}
          </label>
          <div className="flex flex-wrap gap-2 px-4">
            <button className={primary} disabled={busy} onClick={() => finish("done")}>
              <Check size={mobile ? 16 : 14} /> {t("ho.done")}
            </button>
            <button className={plain} disabled={busy} onClick={() => finish("cancel")}>
              <X size={mobile ? 16 : 14} /> {t("ho.cancel")}
            </button>
          </div>
          <p className="flex items-start gap-1.5 px-4 pb-4 text-xs text-ink-2">
            <Lock size={12} className="mt-0.5 shrink-0" /> {t("ho.secure")}
          </p>
        </>
      ) : (
        <div className="flex flex-col gap-3 px-4 pb-4">
          <p className="text-sm">{t(`ho.state.${h.status}`)}</p>
          {h.status === "expired" && !h.closed && (
            <div className="flex flex-wrap gap-2">
              <button className={primary} disabled={busy} onClick={() => finish("resume")}>
                <RotateCcw size={mobile ? 16 : 14} /> {t("ho.resume")}
              </button>
              <button className={plain} disabled={busy} onClick={() => finish("dismiss")}>
                {t("ho.dismiss")}
              </button>
            </div>
          )}
          {onClose && (
            <button className={plain} onClick={onClose}>
              {t("ho.back")}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
