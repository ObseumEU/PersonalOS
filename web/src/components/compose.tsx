/**
 * What every box the owner writes to agents in shares (chat on the web and in /m, the answer on
 * "Čeká na tebe", task comments):
 *
 * - useAttachments: a picture pasted from the clipboard (Ctrl+V, a screenshot) or picked is
 *   uploaded at once (POST /api/files) and goes with the message as an attachment; the server
 *   writes it into the text as "📎 name (soubor #id)" and the agent's worker hands the picture
 *   to the model (pos_worker.images).
 * - MicButton: dictation with the browser's speech recognition (Web Speech API, cs-CZ). Tap to
 *   start, tap to stop; what is heard goes live into the box (interim results too) so it can be
 *   edited before sending. The stack has no speech-to-text service of its own, so a browser
 *   without it (Firefox, some iOS home-screen apps) shows no button: the keyboard's microphone
 *   still works there.
 */
import { Mic, Square, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ClipboardEvent } from "react";
import { filesApi } from "../filesApi";
import { t } from "../i18n/core";
import { IMAGE_EXT, joinDictation, pastedName, splitFileLines } from "../composeText";
import { createDictation, recognitionClass } from "../dictation";
import { toast } from "./overlay";

export { joinDictation, splitFileLines };

export type Attached = { type: "file"; id: number; name: string; mime?: string | null; preview?: string };

/** Image files on the clipboard of a paste event (none for plain text: the paste goes on as usual). */
export function clipboardImages(e: ClipboardEvent | globalThis.ClipboardEvent): File[] {
  const data = e.clipboardData;
  if (!data) return [];
  const out: File[] = [];
  for (const item of Array.from(data.items ?? [])) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const f = item.getAsFile();
      if (f) out.push(f);
    }
  }
  if (!out.length) for (const f of Array.from(data.files ?? [])) if (f.type.startsWith("image/")) out.push(f);
  return out;
}

export function useAttachments() {
  const [files, setFiles] = useState<Attached[]>([]);
  const [uploading, setUploading] = useState<string | null>(null);
  const add = useCallback(async (list: File[] | FileList | null, pasted = false) => {
    for (const raw of Array.from(list ?? [])) {
      const f = pasted || raw.name === "image.png" ? new File([raw], pastedName(raw.type), { type: raw.type }) : raw;
      setUploading(f.name);
      try {
        const up = await filesApi.upload(f);
        setFiles((fs) => (fs.some((x) => x.id === up.id) ? fs : [...fs, { type: "file", id: up.id, name: up.name, mime: up.mime, preview: up.preview }]));
      } catch (e) {
        toast(e instanceof Error ? e.message : String(e), { error: true });
      }
    }
    setUploading(null);
  }, []);
  /** onPaste for a textarea: pictures are attached, text pastes as usual. */
  const onPaste = useCallback(
    (e: ClipboardEvent<HTMLElement>) => {
      const imgs = clipboardImages(e);
      if (!imgs.length) return;
      e.preventDefault();
      void add(imgs, true);
    },
    [add],
  );
  const remove = useCallback((id: number) => setFiles((fs) => fs.filter((f) => f.id !== id)), []);
  const clear = useCallback(() => setFiles([]), []);
  return { files, uploading, add, onPaste, remove, clear, ids: files.map((f) => f.id) };
}

/** The attachments waiting to be sent: a thumbnail for pictures, the name otherwise, ✕ to drop one. */
export function AttachChips({ files, uploading, onRemove, className = "" }: { files: Attached[]; uploading: string | null; onRemove: (id: number) => void; className?: string }) {
  if (!files.length && !uploading) return null;
  return (
    <div className={`flex flex-wrap items-center gap-1.5 ${className}`}>
      {files.map((f) => (
        <span key={f.id} className="flex h-9 max-w-full items-center gap-1.5 rounded-full border border-line bg-surface pr-1 pl-1.5 text-[13px]">
          {f.preview === "image" ? (
            <img src={filesApi.contentUrl(f.id)} alt="" className="h-7 w-7 shrink-0 rounded-full object-cover" />
          ) : null}
          <span className="max-w-[40vw] truncate sm:max-w-56">{f.name}</span>
          <button type="button" aria-label={t("m.chat.remove_attachment")} onClick={() => onRemove(f.id)} className="grid h-7 w-7 shrink-0 place-items-center text-ink-2 hover:text-ink">
            <X size={14} />
          </button>
        </span>
      ))}
      {uploading && <span className="text-[13px] text-ink-2">{t("m.chat.uploading", { name: uploading })}</span>}
    </div>
  );
}

/* ------------------------------------------------------------------ dictation */

export const canDictate = () => recognitionClass() !== null;

/**
 * Tap to dictate into a box; tap again to stop. `value`/`onChange` are the box's own state: the
 * words go in live (the not-yet-final ones too) after what was there when dictation started.
 * A browser without speech recognition (Firefox) gets no button.
 */
export function MicButton({ value, onChange, className = "", size = 18 }: { value: string; onChange: (v: string) => void; className?: string; size?: number }) {
  const [on, setOn] = useState(false);
  const session = useRef<ReturnType<typeof createDictation> | null>(null);
  const latest = useRef(value);
  latest.current = value;
  const change = useRef(onChange);
  change.current = onChange;
  const Cls = recognitionClass();

  useEffect(() => () => session.current?.abort(), []);

  const toggle = useCallback(() => {
    if (on) return session.current?.stop();
    if (!Cls) return;
    const android = typeof navigator !== "undefined" && /Android/i.test(navigator.userAgent);
    session.current = createDictation(
      Cls,
      {
        base: () => latest.current,
        onText: (base, said) => change.current(joinDictation(base, said)),
        onError: (error) => toast(error === "not-allowed" || error === "service-not-allowed" ? t("compose.mic_denied") : t("compose.mic_error", { error }), { error: true }),
        onStop: () => setOn(false),
      },
      android,
    );
    setOn(true);
    session.current.start();
  }, [on, Cls]);

  if (!Cls) return null;
  return (
    <button
      type="button"
      onClick={toggle}
      onPointerDown={(e) => e.preventDefault() /* the box keeps its focus (the phone's keyboard stays) */}
      aria-pressed={on}
      aria-label={on ? t("compose.mic_stop") : t("compose.mic_start")}
      title={on ? t("compose.mic_stop") : t("compose.mic_start")}
      data-mic
      className={`relative grid shrink-0 place-items-center rounded-full transition ${on ? "bg-red-500 text-white" : "text-ink-2 hover:text-ink active:bg-raised"} ${className}`}
    >
      {on && <span aria-hidden className="absolute inset-0 animate-ping rounded-full bg-red-500/40" />}
      {on ? <Square size={size - 4} fill="currentColor" className="relative" /> : <Mic size={size} />}
      {on && <span className="sr-only" role="status">{t("compose.mic_listening")}</span>}
    </button>
  );
}

/* ------------------------------------------------------------------ attached files in a text */

/** Those files: pictures as thumbnails (open full size), others as a link. */
export function FileLinks({ files }: { files: { id: number; name: string }[] }) {
  if (!files.length) return null;
  return (
    <span className="flex flex-wrap gap-1.5 pt-1">
      {files.map((f) =>
        IMAGE_EXT.test(f.name) ? (
          <a key={f.id} href={filesApi.contentUrl(f.id)} target="_blank" rel="noreferrer" title={f.name}>
            <img src={filesApi.contentUrl(f.id)} alt={f.name} loading="lazy" className="max-h-48 max-w-full rounded-md border border-line object-contain" />
          </a>
        ) : (
          <a key={f.id} href={filesApi.contentUrl(f.id)} target="_blank" rel="noreferrer" className="text-[13px] text-accent hover:underline">
            📎 {f.name}
          </a>
        ),
      )}
    </span>
  );
}
