// The History tab (spec §15.4, §11.11 L4591; plan M4 Task 24, resolution f, B25): the job's results newest first,
// as served (published, profile version, rubric, tuple and status), each with its instances (the scope version,
// M, L and U, the coverage, the confidence and when it was computed, the current one marked). A verified
// not-a-job-ad instance has no scores: they read "—". The first page is the detail's; Load more reads the next
// while there is one, and its failure is shown with Retry while the loaded results stay.
import { useId } from "react";
import { useTranslation } from "react-i18next";
import { EmptyState } from "../components/EmptyState";
import { ProblemBox } from "../components/ProblemBox";
import { useServerText } from "../i18n/index";
import { useFormat } from "../system/format";
import { type HistoryItemOut, type HistoryPageOut, useJobHistory } from "./detailApi";
import { percentInt, scoreInt } from "./format";
import styles from "./HistoryTab.module.css";

type InstanceOut = HistoryItemOut["instances"][number];

function Instance({ instance }: { instance: InstanceOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const none = t("drawer.no_value");
  const score = (value: string | null) => (value === null ? none : String(scoreInt(value)));
  return (
    <li className={styles.instance}>
      {t("history.instance", {
        scope: String(instance.scope_version_id),
        mid: score(instance.mid),
        low: score(instance.low),
        high: score(instance.high),
        coverage: instance.coverage === null ? none : t("history.percent", { percent: String(percentInt(instance.coverage)) }),
        confidence: instance.confidence === null ? none : serverText("confidence", instance.confidence),
        date: format.dateTimeMinutes(instance.computed_at),
      })}
      {instance.is_current && (
        <>
          {" · "}
          <span className={styles.current}>{t("history.current")}</span>
        </>
      )}
    </li>
  );
}

function Result({ item }: { item: HistoryItemOut }) {
  const { t } = useTranslation();
  const serverText = useServerText();
  const format = useFormat();
  const id = useId();
  return (
    <article aria-labelledby={id} className={styles.result}>
      <h4 id={id} className={styles.heading}>
        {t("history.result", {
          date: format.dateTimeMinutes(item.published_at),
          profile: String(item.profile_version_no),
          rubric: item.rubric_version,
          tuple: String(item.llm_tuple_id),
          status: serverText("result_status", item.status),
        })}
      </h4>
      <ul role="list" className={styles.instances}>
        {item.instances.map((instance) => <Instance key={instance.det_result_id} instance={instance} />)}
      </ul>
    </article>
  );
}

export function HistoryTab({ jobId, first }: { jobId: number; first: HistoryPageOut }) {
  const { t } = useTranslation();
  const history = useJobHistory(jobId, first);
  if (history.items.length === 0) {
    return <EmptyState title={t("history.none")} />;
  }
  const failed = history.loadMoreError !== null;
  return (
    <div className={styles.tab}>
      <ol role="list" className={styles.results} aria-label={t("history.title")}>
        {history.items.map((item) => (
          <li key={item.result_id}>
            <Result item={item} />
          </li>
        ))}
      </ol>
      {failed && <ProblemBox error={history.loadMoreError} onRetry={history.loadMore} />}
      {history.hasNextPage && !failed && (
        <button type="button" className={styles.more} aria-disabled={history.loadingMore} onClick={history.loadMore}>
          {history.loadingMore ? t("jobs.loading_more") : t("jobs.load_more")}
        </button>
      )}
    </div>
  );
}
