import { useSyncExternalStore } from "react";
import type { RefKind } from "./refs";

/** Which reference preview is open (components/RefPreview.tsx). Tiny, so the /m entry can carry it. */
let current: { kind: RefKind; id: string } | null = null;
const subs = new Set<() => void>();

export function openRef(kind: RefKind, id: string) {
  current = { kind, id };
  subs.forEach((f) => f());
}

export function closeRef() {
  current = null;
  subs.forEach((f) => f());
}

export function useCurrentRef() {
  return useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => current,
  );
}
