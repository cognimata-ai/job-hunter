// The Jobs list (spec §15.3, §15.8; plan M4 Task 22): the first page of `GET /api/jobs` under the server's
// default query, as cards. While it loads, 8 skeleton cards; a failed first read, the problem with Retry (a
// failed refetch keeps the cards, and the toast is the query client's); no cards, the empty state. Task 23 adds
// the filters in the URL, Load more and "Ranking changed".
import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import pageStyles from "../app/Pages.module.css";
import { EmptyState } from "../components/EmptyState";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import { useJobsFirstPage } from "../jobs/api";
import { JobCard } from "../jobs/JobCard";
import styles from "./JobsPage.module.css";

const SKELETON_CARDS = 8;

// Task 24 opens the card's drawer here.
function openCard(): void {
  // nothing yet
}

export function JobsPage() {
  const { t } = useTranslation();
  const headingId = useId();
  const page = useJobsFirstPage();
  // The card in the tab order (roving focus): the first one until the list keyboard moves it (Task 23's
  // `useListKeyboard`), and the first one again when that card has left the list.
  const [focusedId] = useState<number | null>(null);
  const items = page.data?.items ?? [];
  const focused = items.some((card) => card.job_id === focusedId) ? focusedId : (items.at(0)?.job_id ?? null);
  return (
    <div className={styles.page}>
      <h1 id={headingId} className={pageStyles.title}>{t("jobs.title")}</h1>
      {page.data !== undefined ? (
        items.length === 0 ? (
          <EmptyState title={t("jobs.empty_title")}>{t("jobs.empty_body")}</EmptyState>
        ) : (
          <ol role="list" className={styles.list} aria-labelledby={headingId}>
            {items.map((card) => (
              <li key={card.job_id}>
                <JobCard card={card} focused={card.job_id === focused} onOpen={openCard} />
              </li>
            ))}
          </ol>
        )
      ) : page.isError ? (
        <ProblemBox error={page.error} onRetry={() => { void page.refetch(); }} />
      ) : (
        <Skeleton variant="card" count={SKELETON_CARDS} />
      )}
    </div>
  );
}
