import { ArchiveRestore } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { ago, t } from "../../i18n";
import { taskChanged } from "../../taskSheet";
import { type Task, tasksApi } from "../../tasksApi";
import { toast } from "../overlay";
import { PageHeader } from "../ui";
import { Avatar } from "./bits";

/** Úkoly → Archiv (/tasks?view=archived): archived tasks, each with "Obnovit" (POST unarchive). */
export default function ArchivedTasks() {
  const [items, setItems] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(() => {
    tasksApi.archived().then(
      (x) => {
        setItems(x);
        setError(null);
      },
      (e) => setError(e instanceof Error ? e.message : String(e)),
    );
  }, []);
  useEffect(load, [load]);
  useEffect(() => {
    window.addEventListener("pos:tasks", load);
    return () => window.removeEventListener("pos:tasks", load);
  }, [load]);

  const restore = async (task: Task) => {
    setBusy(task.ref);
    try {
      await tasksApi.unarchive(task.ref);
      setItems((xs) => (xs ?? []).filter((x) => x.ref !== task.ref));
      toast(t("tk.archive.restored", { title: task.title }));
      taskChanged();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-5">
      <PageHeader kicker={t("tk.list.kicker")} title={t("tk.archive.title")} sub={t("tk.archive.sub")} />
      <section className="panel flex min-w-0 flex-col">
        <div className="border-b border-line px-4 py-2.5">
          <Link to="/tasks" className="text-[13px] text-accent hover:underline">
            {t("tk.archive.back")}
          </Link>
        </div>
        {!items && !error && <p className="p-6 text-sm text-ink-2">{t("act.loading")}</p>}
        {items && items.length === 0 && <p className="p-8 text-center text-sm text-ink-2">{t("tk.archive.none")}</p>}
        <ul>
          {(items ?? []).map((task) => (
            <li key={task.id} className="grid grid-cols-[32px_minmax(0,1fr)_auto] items-center gap-3 border-b border-line px-4 py-3">
              <Avatar type={task.assignee_type} name={task.assignee_name} size={30} />
              <span className="flex min-w-0 flex-col gap-0.5">
                <span className="truncate text-[14px] text-ink-2">
                  <span className="mr-2 font-mono text-xs text-ink-3">{task.ref}</span>
                  {task.title}
                </span>
                <span className="truncate text-xs text-ink-3">
                  {[task.assignee_name, task.archived_at ? t("tk.archive.at", { when: ago(task.archived_at) }) : null].filter(Boolean).join(" · ")}
                </span>
              </span>
              <button type="button" className="btn h-8!" disabled={busy === task.ref} onClick={() => restore(task)}>
                <ArchiveRestore size={14} /> {t("act.restore")}
              </button>
            </li>
          ))}
        </ul>
        {error && <p className="p-4 text-xs break-words text-red-400">{error}</p>}
      </section>
    </div>
  );
}
