import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { t } from "../i18n";

/* Toasts (with an optional undo) and a confirmation dialog, rendered once by the Shell. */

type Toast = { id: number; text: string; undo?: () => unknown; error?: boolean };
type Ask = {
  title: string;
  body?: string;
  confirm: string;
  danger?: boolean;
  reason?: string; // a reason field with this label
  resolve: (v: string | null) => void;
};

let toastSink: ((t: Toast) => void) | null = null;
let askSink: ((a: Ask) => void) | null = null;
let nextId = 1;

/** A short notice at the bottom; with `undo`, a "Vrátit zpět" button for 8 s. */
export function toast(text: string, opts: { undo?: () => unknown; error?: boolean } = {}) {
  toastSink?.({ id: nextId++, text, ...opts });
}

/**
 * Ask before something that is hard to take back. Resolves to null when
 * cancelled, else to the reason typed (an empty string without a reason field).
 */
export function confirmDialog(opts: Omit<Ask, "resolve">): Promise<string | null> {
  return new Promise((resolve) => {
    if (!askSink) return resolve(window.confirm(opts.title) ? "" : null);
    askSink({ ...opts, resolve });
  });
}

function ToastItem({ t: item, onClose }: { t: Toast; onClose: () => void }) {
  useEffect(() => {
    const h = setTimeout(onClose, item.undo ? 8000 : 4000);
    return () => clearTimeout(h);
  }, [item, onClose]);
  return (
    <div
      role="status"
      className={`fade-in pointer-events-auto flex items-center gap-3 rounded-md border bg-raised px-4 py-2.5 text-sm shadow-lg ${
        item.error ? "border-red-400/60 text-red-300" : "border-line text-ink"
      }`}
    >
      <span className="min-w-0 flex-1">{item.text}</span>
      {item.undo && (
        <button
          className="shrink-0 text-sm font-medium text-accent hover:underline"
          onClick={() => {
            item.undo?.();
            onClose();
          }}
        >
          {t("act.undo")}
        </button>
      )}
      <button aria-label={t("act.close")} className="shrink-0 text-ink-2 hover:text-ink" onClick={onClose}>
        <X size={14} />
      </button>
    </div>
  );
}

function Dialog({ a, onDone }: { a: Ask; onDone: () => void }) {
  const [reason, setReason] = useState("");
  const ref = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!a.reason) ref.current?.focus();
    const esc = (e: KeyboardEvent) => e.key === "Escape" && close(null);
    window.addEventListener("keydown", esc);
    return () => window.removeEventListener("keydown", esc);
  });
  const close = (v: string | null) => {
    a.resolve(v);
    onDone();
  };
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-4" onClick={() => close(null)}>
      <form
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
        className="panel flex w-full max-w-md flex-col gap-3 p-5"
        onClick={(e) => e.stopPropagation()}
        onSubmit={(e) => {
          e.preventDefault();
          close(reason.trim());
        }}
      >
        <h2 id="confirm-title" className="text-base font-medium">
          {a.title}
        </h2>
        {a.body && <p className="text-sm leading-relaxed text-ink-2">{a.body}</p>}
        {a.reason && (
          <label className="flex flex-col gap-1">
            <span className="text-xs text-ink-2">{a.reason}</span>
            <input
              autoFocus
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="h-9 rounded border border-line bg-bg px-2 text-sm outline-none focus:border-accent"
            />
          </label>
        )}
        <div className="flex justify-end gap-2 pt-1">
          <button type="button" className="btn" onClick={() => close(null)}>
            {t("act.cancel")}
          </button>
          <button ref={ref} className={a.danger ? "btn border-amber-400/70! text-amber-300!" : "btn-accent"}>
            {a.confirm}
          </button>
        </div>
      </form>
    </div>
  );
}

export function OverlayHost() {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [asks, setAsks] = useState<Ask[]>([]);
  useEffect(() => {
    toastSink = (x) => setToasts((ts) => [...ts.slice(-3), x]);
    askSink = (x) => setAsks((as) => [...as, x]);
    return () => {
      toastSink = null;
      askSink = null;
    };
  }, []);
  return (
    <>
      <div className="pointer-events-none fixed inset-x-0 bottom-20 z-40 flex flex-col items-center gap-2 px-4 lg:bottom-6">
        {toasts.map((x) => (
          <ToastItem key={x.id} t={x} onClose={() => setToasts((ts) => ts.filter((y) => y.id !== x.id))} />
        ))}
      </div>
      {asks[0] && <Dialog key={asks.length} a={asks[0]} onDone={() => setAsks((as) => as.slice(1))} />}
    </>
  );
}
