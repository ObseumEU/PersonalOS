import { useSyncExternalStore, type ReactNode } from "react";
import { Link, useLocation } from "react-router-dom";

/*
 * The task panel opens over whatever page is showing (Home, Chat, Agents…):
 * `?task=T-123` on the current address, or `/tasks/T-123` on the task list.
 * `&focus=needs` scrolls to "Co potřebuju od tebe", `&part=talk` opens a tab; `?approval=12` opens an
 * approval that belongs to no task. Closing drops the parameter, so the page
 * underneath stays as it was and the browser's Back closes the panel too.
 */

let order: string[] = [];
const subs = new Set<() => void>();

/** The list the panel steps through with ← →; the task list and Home set it. */
export function setSheetOrder(refs: string[]) {
  if (refs.length === order.length && refs.every((r, i) => r === order[i])) return;
  order = refs;
  subs.forEach((f) => f());
}

export function useSheetOrder(): string[] {
  return useSyncExternalStore(
    (f) => {
      subs.add(f);
      return () => subs.delete(f);
    },
    () => order,
  );
}

const SHEET_KEYS = ["task", "focus", "approval", "part"];

/** The current search without the panel's own parameters. */
export function withoutSheet(search: string): URLSearchParams {
  const p = new URLSearchParams(search);
  SHEET_KEYS.forEach((k) => p.delete(k));
  return p;
}

/** Where a link to a task goes: the panel over the current page (or /tasks/T-1 on the list). */
export function taskHref(loc: { pathname: string; search: string }, ref: string, focus?: "needs"): string {
  const p = withoutSheet(loc.search);
  if (loc.pathname === "/tasks" || /^\/tasks\/T-\d+$/i.test(loc.pathname)) {
    if (focus) p.set("focus", focus);
    const q = p.toString();
    return `/tasks/${ref}${q ? `?${q}` : ""}`;
  }
  p.set("task", ref);
  if (focus) p.set("focus", focus);
  return `${loc.pathname}?${p.toString()}`;
}

export function approvalHref(loc: { pathname: string; search: string }, id: number): string {
  const p = withoutSheet(loc.search);
  p.set("approval", String(id));
  return `${loc.pathname}?${p.toString()}`;
}

/** A link that opens the task in the panel over the page you are on. */
export function TaskLink({
  taskRef,
  focus,
  className,
  title,
  children,
}: {
  taskRef: string;
  focus?: "needs";
  className?: string;
  title?: string;
  children: ReactNode;
}) {
  const loc = useLocation();
  return (
    <Link to={taskHref(loc, taskRef, focus)} className={className} title={title}>
      {children}
    </Link>
  );
}

/** Tell the pages a task changed in the panel (the list refreshes). */
export function taskChanged() {
  window.dispatchEvent(new Event("pos:tasks"));
  window.dispatchEvent(new Event("pos:needs-me"));
}
