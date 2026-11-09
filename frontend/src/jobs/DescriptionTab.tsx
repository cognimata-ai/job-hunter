// The Description tab (spec §15.4; plan M4 Task 24, resolutions c and d, B14, B26): the ad's text with every
// evidence quote of the score marked, a criterion's and a fact's alike (Ruling T6-a), by its code-point span; a
// quote without a span is not marked. A partial description (a source's excerpt) says so, and a deferred member's
// description, which is its representative's, names that job.
import { useTranslation } from "react-i18next";
import type { Span } from "./codepoints";
import styles from "./DescriptionTab.module.css";
import type { DetailScoreOut, JobDetailOut } from "./detailApi";
import { EvidenceHighlighter } from "./EvidenceHighlighter";
import { JobRef } from "./JobRef";

/** The spans of the score's description evidence: its criteria's and its facts' rows with both offsets. */
export function descriptionSpans(score: DetailScoreOut | null): Span[] {
  if (score === null) {
    return [];
  }
  const rows = [...score.criteria.flatMap((criterion) => criterion.evidence), ...score.fact_evidence];
  return rows.flatMap((row) =>
    row.source_kind === "description" && row.span_start !== null && row.span_end !== null
      ? [{ start: row.span_start, end: row.span_end }]
      : [],
  );
}

export interface DescriptionTabProps {
  detail: JobDetailOut;
  onNavigate: (jobId: number) => void;
}

export function DescriptionTab({ detail, onNavigate }: DescriptionTabProps) {
  const { t } = useTranslation();
  const { description, job } = detail;
  return (
    <div className={styles.tab}>
      {description.from_job_id !== job.job_id && (
        <p className={styles.note}>
          {t("drawer.description_from")} <JobRef jobId={description.from_job_id} detail={detail} onNavigate={onNavigate} />
        </p>
      )}
      {description.completeness === "partial" && <p className={styles.note}>{t("drawer.partial")}</p>}
      {description.text === "" ? (
        <p className={styles.note}>{t("drawer.no_description")}</p>
      ) : (
        <EvidenceHighlighter text={description.text} lang={description.lang} spans={descriptionSpans(detail.score)} />
      )}
    </div>
  );
}
