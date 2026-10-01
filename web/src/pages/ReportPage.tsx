import { ArrowLeft, Printer } from "lucide-react";
import { Link, useLocation, useParams } from "react-router-dom";
import { ReportDetails, ReportTop, useReport } from "../components/tasks/Report";
import { label, t } from "../i18n";
import { taskHref } from "../taskSheet";

/**
 * The full report as a readable document (/report/T-123, /m/report/T-123 in the installed app): a wide
 * reading column, the takeaway, the decisions and the next step first, then everything else open
 * (the result itself with its tables, sources with quotes, changes, verification). Prints cleanly.
 */
export default function ReportPage() {
  const { ref = "" } = useParams();
  const taskRef = ref.toUpperCase();
  const loc = useLocation();
  const { report, error, loading, setReport } = useReport(taskRef, !!taskRef);
  const mobile = loc.pathname.startsWith("/m/");
  const back = mobile ? `/m/tasks?task=${taskRef}` : taskHref({ pathname: "/tasks", search: "" }, taskRef);
  return (
    <article id="report-doc" className="report-doc mx-auto flex w-full max-w-[860px] flex-col gap-8 px-4 py-6 sm:px-8 sm:py-10">
      <nav className="flex items-center gap-2 print:hidden">
        <Link to={back} className="btn h-9!">
          <ArrowLeft size={14} /> {t("rp.back")}
        </Link>
        <button type="button" className="btn ml-auto h-9!" onClick={() => window.print()}>
          <Printer size={14} /> {t("rp.print")}
        </button>
      </nav>
      {!report && loading && (
        <div className="flex flex-col gap-2" aria-busy="true">
          <p className="text-[15px]">{t("rp.loading")}</p>
          <p className="text-[13px] text-ink-2">{t("rp.loading_hint")}</p>
        </div>
      )}
      {error && <p className="text-sm text-amber-200">{t("rp.error", { error })}</p>}
      {report && !report.available && !loading && <p className="text-sm text-ink-2">{t("rp.none")}</p>}
      {report?.available && (
        <>
          <header className="flex flex-col gap-2 border-b border-line pb-5">
            <span className="font-mono text-xs text-ink-2">{report.task}</span>
            <h1 className="text-[26px] leading-tight font-normal tracking-[-0.01em] sm:text-[32px]">{report.title}</h1>
            <p className="text-[14px] text-ink-2">{t("rp.doc_meta", { who: report.assignee ?? "—", status: label("tk.status", report.status) })}</p>
          </header>
          <ReportTop r={report} onChange={setReport} />
          <div className="border-t border-line pt-6">
            <h2 className="pb-4 text-[20px] font-normal">{t("rp.details")}</h2>
            <ReportDetails r={report} full />
          </div>
        </>
      )}
    </article>
  );
}
