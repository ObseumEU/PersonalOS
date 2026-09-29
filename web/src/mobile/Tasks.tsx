import { Plus } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { toast } from "../components/overlay";
import { t } from "../i18n/core";
import { type Task, type View, dueLabel, tasksApi } from "../tasksApi";
import { TopBar } from "./ui";

const VIEWS: View[] = ["today", "next", "agents", "waiting", "review"];

function Row({ task, view }: { task: Task; view: View }) {
  const due = dueLabel(task);
  return (
    <Link to={`?view=${view}&task=${task.ref}`} className="flex min-h-14 items-center gap-3 border-b border-line px-4 py-2.5 active:bg-raised">
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-[15px] leading-snug break-words">{task.title}</span>
        <span className="flex flex-wrap gap-x-2 text-[12px] text-ink-2">
          <span className="font-mono">{task.ref}</span>
          {task.assignee_name && <span>{task.assignee_name}</span>}
          {task.progress != null && task.progress > 0 && <span>{task.progress} %</span>}
          {due.text && <span className={due.urgent ? "text-amber-300" : ""}>{due.text}</span>}
        </span>
      </span>
    </Link>
  );
}

/** Úkoly: a few views, one line per task; a tap opens the full task panel (loaded on demand). */
export default function Tasks() {
  const [params, setParams] = useSearchParams();
  const view = (params.get("view") as View) || "today";
  const [tasks, setTasks] = useState<Task[] | null>(null);
  const [text, setText] = useState("");
  const load = useCallback(() => {
    setTasks(null);
    tasksApi.list(view === "review" ? "to_review" : view).then(setTasks, () => setTasks([]));
  }, [view]);
  useEffect(load, [load]);
  useEffect(() => {
    window.addEventListener("pos:tasks", load);
    return () => window.removeEventListener("pos:tasks", load);
  }, [load]);

  const capture = async () => {
    const v = text.trim();
    if (!v) return;
    try {
      const task = await tasksApi.capture(v);
      setText("");
      toast(t("m.tasks.added", { ref: task.ref }));
      load();
    } catch (e) {
      toast(e instanceof Error ? e.message : String(e), { error: true });
    }
  };

  return (
    <div className="flex flex-col">
      <TopBar title={t("m.tasks.title")} />
      <div role="tablist" className="flex gap-1.5 overflow-x-auto border-b border-line px-3 py-2 [scrollbar-width:none]">
        {VIEWS.map((v) => (
          <button
            key={v}
            role="tab"
            aria-selected={v === view}
            onClick={() => setParams({ view: v })}
            className={`h-9 shrink-0 rounded-full border px-3.5 text-[14px] ${v === view ? "border-accent bg-accent/10 text-accent" : "border-line text-ink-2"}`}
          >
            {t(`m.tasks.view.${v}`)}
          </button>
        ))}
      </div>
      <form
        className="flex gap-2 border-b border-line px-3 py-2"
        onSubmit={(e) => {
          e.preventDefault();
          capture();
        }}
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={t("m.tasks.capture")}
          aria-label={t("m.tasks.capture_aria")}
          className="h-11 min-w-0 flex-1 rounded-lg border border-line bg-surface px-3 text-[16px] outline-none focus:border-accent"
        />
        <button aria-label={t("m.tasks.capture_aria")} disabled={!text.trim()} className="grid h-11 w-11 place-items-center rounded-lg border border-accent bg-accent/10 text-accent disabled:opacity-40">
          <Plus size={20} />
        </button>
      </form>
      {tasks === null && <p className="px-4 py-6 text-sm text-ink-2">{t("act.loading")}</p>}
      {tasks?.length === 0 && <p className="px-4 py-8 text-center text-sm text-ink-2">{t("m.tasks.empty")}</p>}
      {tasks?.map((x) => <Row key={x.id} task={x} view={view} />)}
    </div>
  );
}
