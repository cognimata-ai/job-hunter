// System → Runs (spec §14.2 L5034-5035, §15.7, §17.3; plan M4 Task 31, resolution e): the run history, newest
// first, keyset-paged with Load more (`GET /api/runs`). Each row shows the run's status (with its reason code),
// its trigger (and whether it took the paid sources), when it started and how long it took. A run row carries no
// counts: they are its report's, which the row's [Report] opens below it, read through `useRun` and shown by
// `RunReport`. One report is open at a time; [Report] again closes it.
import { Fragment, useState } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useServerText } from "../i18n/index";
import fields from "../settings/fields.module.css";
import tables from "../settings/tables.module.css";
import { type RunOut, useRun } from "./api";
import { useFormat } from "./format";
import { RunReport } from "./RunReport";
import styles from "./RunsList.module.css";
import parts from "./SystemSection.module.css";
import { type PagedList, useRuns } from "./systemApi";
import { useUnits } from "./units";

const COLUMNS = 6;

/** The open report: the run read again with its report (polled while it runs). */
function OpenReport({ runId }: { runId: number }) {
  const run = useRun(runId);
  if (run.isPending) {
    return <Skeleton variant="line" count={3} />;
  }
  if (run.isError) {
    return <ProblemBox error={run.error} onRetry={() => { void run.refetch(); }} />;
  }
  return <RunReport run={run.data.run} report={run.data.report} partialCauses={run.data.partial_causes} />;
}

/** How long a run or a cycle took: `null` before it started, `still` while it runs. */
export function useElapsed(): (started: string | null, finished: string | null, still: string) => string {
  const { t } = useTranslation();
  const units = useUnits();
  return (started, finished, still) => {
    if (started === null) {
      return t("value.none");
    }
    if (finished === null) {
      return still;
    }
    return units.duration((Date.parse(finished) - Date.parse(started)) / 1000);
  };
}

/** A list's Load more, or its failure with Retry. */
export function LoadMore<T>({ list }: { list: PagedList<T> }) {
  const { t } = useTranslation();
  if (!list.hasMore && list.loadMoreError === null) {
    return null;
  }
  return (
    <div className={parts.more}>
      {list.loadMoreError !== null && <ProblemBox error={list.loadMoreError} />}
      <button type="button" className={fields.button} disabled={list.loadingMore} aria-busy={list.loadingMore || undefined}
        onClick={list.loadMore}>
        {t("jobs.load_more")}
      </button>
    </div>
  );
}

function RunRow({ run, open, onToggle }: { run: RunOut; open: boolean; onToggle: () => void }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const elapsed = useElapsed();
  const reportId = `run-report-${String(run.id)}`;
  const trigger = serverText("run_trigger", run.trigger);
  return (
    <Fragment>
      <tr>
        <th scope="row" className={cx(tables.nowrap, styles.id)}>{t("runs.id", { id: String(run.id) })}</th>
        <td data-label={t("runs.col_status")}>
          {serverText("run_status", run.status)}
          {run.status_reason !== null && <code className={styles.reason}>{run.status_reason}</code>}
        </td>
        <td data-label={t("runs.col_trigger")}>{run.include_paid ? `${trigger} · ${t("runs.paid")}` : trigger}</td>
        <td data-label={t("runs.col_started")} className={tables.nowrap}>
          {run.started_at === null ? t("runs.not_started") : format.dateTimeMinutes(run.started_at)}
        </td>
        <td data-label={t("runs.col_duration")} className={tables.nowrap}>{elapsed(run.started_at, run.finished_at, t("runs.no_duration"))}</td>
        <td>
          <button type="button" className={cx(tables.action, styles.toggle)} aria-expanded={open} aria-controls={open ? reportId : undefined}
            onClick={onToggle}>
            {t("runs.report")}<span className="visually-hidden"> {t("runs.report_of", { id: String(run.id) })}</span>
          </button>
        </td>
      </tr>
      {open && (
        <tr className={tables.detail}>
          <td id={reportId} colSpan={COLUMNS} className={tables.wide}><OpenReport runId={run.id} /></td>
        </tr>
      )}
    </Fragment>
  );
}

export function RunsList() {
  const { t } = useTranslation();
  const runs = useRuns();
  const [open, setOpen] = useState<number | null>(null);
  let body;
  if (runs.items === undefined) {
    body = runs.error === null ? <Skeleton variant="line" count={3} /> : <ProblemBox error={runs.error} onRetry={runs.retry} />;
  } else if (runs.items.length === 0) {
    body = <p className={parts.empty}>{t("runs.empty")}</p>;
  } else {
    body = (
      <>
        <div className={tables.scroll}>
          <table className={tables.table} aria-labelledby="runs-heading">
            <thead>
              <tr>
                <th scope="col">{t("runs.col_run")}</th>
                <th scope="col">{t("runs.col_status")}</th>
                <th scope="col">{t("runs.col_trigger")}</th>
                <th scope="col">{t("runs.col_started")}</th>
                <th scope="col">{t("runs.col_duration")}</th>
                <th scope="col"><span className="visually-hidden">{t("runs.col_report")}</span></th>
              </tr>
            </thead>
            <tbody>
              {runs.items.map((run) => (
                <RunRow key={run.id} run={run} open={open === run.id}
                  onToggle={() => { setOpen((current) => (current === run.id ? null : run.id)); }} />
              ))}
            </tbody>
          </table>
        </div>
        <LoadMore list={runs} />
      </>
    );
  }
  return (
    <section id="runs" aria-labelledby="runs-heading" className={parts.part}>
      <h3 id="runs-heading" className={parts.title}>{t("settings_system.runs")}</h3>
      {body}
    </section>
  );
}
