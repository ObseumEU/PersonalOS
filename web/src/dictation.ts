// One dictation session with the browser's speech recognition (MicButton in components/compose.tsx),
// kept free of React so `npm test` checks it with a fake recogniser.

type SpeechAlt = { transcript: string };
type SpeechResult = { isFinal: boolean; length: number; [i: number]: SpeechAlt };
export type SpeechEvent = { resultIndex: number; results: { length: number; [i: number]: SpeechResult } };
export type Recognition = {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((e: SpeechEvent) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
  start: () => void;
  stop: () => void;
  abort: () => void;
};
export type RecognitionClass = new () => Recognition;

/** The browser's recogniser (Chrome and Safari prefix it), or null where there is none (Firefox). */
export function recognitionClass(w: unknown = typeof window === "undefined" ? undefined : window): RecognitionClass | null {
  const s = w as { SpeechRecognition?: RecognitionClass; webkitSpeechRecognition?: RecognitionClass } | undefined;
  return s?.SpeechRecognition ?? s?.webkitSpeechRecognition ?? null;
}

export type DictationHooks = {
  /** The box's text now: the words go after it. */
  base: () => string;
  /** What was there when the phrase started, and everything heard so far (interim words too). */
  onText: (base: string, said: string) => void;
  /** A real failure ("not-allowed" when the microphone is refused); silence and stopping are not. */
  onError: (error: string) => void;
  /** Listening ended (stopped, or the speech ended). */
  onStop: () => void;
};

/**
 * Czech, live, until stopped. Android's recogniser repeats itself in continuous mode, so there it
 * listens a phrase at a time and starts again; iOS may end after a phrase, which simply stops.
 */
export function createDictation(Cls: RecognitionClass, hooks: DictationHooks, android = false) {
  let want = false;
  let rec: Recognition | null = null;
  const end = () => {
    if (!want) return;
    want = false;
    hooks.onStop();
  };
  const listen = () => {
    const r = new Cls();
    r.lang = "cs-CZ";
    r.interimResults = true;
    r.continuous = !android;
    const base = hooks.base();
    r.onresult = (e) => {
      let said = "";
      for (let i = 0; i < e.results.length; i++) said += `${e.results[i][0]?.transcript ?? ""} `;
      hooks.onText(base, said);
    };
    r.onerror = (e) => {
      if (e.error === "no-speech" || e.error === "aborted") return;
      end();
      hooks.onError(e.error);
    };
    r.onend = () => {
      if (want && android) return listen(); // the next phrase, after what is in the box now
      end();
    };
    rec = r;
    try {
      r.start();
    } catch {
      end();
    }
  };
  return {
    start() {
      want = true;
      listen();
    },
    stop() {
      end();
      try {
        rec?.stop();
      } catch {
        /* already stopped */
      }
    },
    abort() {
      want = false;
      rec?.abort();
    },
  };
}
