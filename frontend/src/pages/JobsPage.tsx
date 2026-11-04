// The Jobs page (spec §15.3, §15.8, §15.9; plan M4 Tasks 22 and 23): the filter panel (the page's `aside`), and
// the list under the URL's search (`useJobsList`): "<total> jobs · <unscored> unscored" ("10,000+" once the
// count is capped), the sort, "⟳ Ranking changed — refresh" when a later page says so, the cards and Load more.
//
// - While the first page is read: 8 skeleton cards. Its failure: the problem with Retry; a failed refetch keeps
//   the cards (its toast is the query client's). A failed Load more: the problem below the cards, which stay.
// - No cards: under the default filters, that nothing has been collected yet; under others, "No jobs match these
//   filters" with Reset filters.
// - Without an active profile, "Upload your CV to start ranking" (a link to the CV section) above the cards,
//   which are still listed, unscored (§15.8).
// - The list's keyboard is `useListKeyboard`'s; Enter on a card writes `job=<id>` (the drawer's, Task 24).
import { Link } from "@tanstack/react-router";
import { useCallback, useId, useLayoutEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import pageStyles from "../app/Pages.module.css";
import { EmptyState } from "../components/EmptyState";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import type { TranslationKey } from "../i18n/index";
import { FilterPanel } from "../jobs/FilterPanel";
import { JobCard } from "../jobs/JobCard";
import { DEFAULT_SEARCH, type JobSort, SORTS, toApiParams } from "../jobs/search";
import { type CountsOut, useJobsList } from "../jobs/useJobsList";
import { useJobsSearch } from "../jobs/useJobsSearch";
import { focusDropped, useListKeyboard } from "../jobs/useListKeyboard";
import { useProfile } from "../profile/api";
import { useFormat } from "../system/format";
import styles from "./JobsPage.module.css";

const SKELETON_CARDS = 8;

const SORT_LABELS = {
  score_mid: "jobs.sort.score_mid",
  score_low: "jobs.sort.score_low",
  posted: "jobs.sort.posted",
  first_seen: "jobs.sort.first_seen",
} as const satisfies Record<JobSort, TranslationKey>;

/** "1,284 jobs · 212 unscored"; a capped count reads "10,000+". */
function CountsLine({ counts }: { counts: CountsOut }) {
  const { t } = useTranslation();
  const format = useFormat();
  const { total_estimate: total, unscored, capped } = counts;
  const shown = (n: number, atCap: boolean) => (atCap ? t("jobs.capped", { number: format.number(n) }) : format.number(n));
  return (
    <p className={styles.counts}>
      {t("jobs.counts", { count: total, total: shown(total, capped), unscored: shown(unscored, capped && unscored >= total) })}
    </p>
  );
}

export function JobsPage() {
  const { t } = useTranslation();
  const headingId = useId();
  const sortId = useId();
  const { search, write } = useJobsSearch();
  const list = useJobsList(search);
  const profile = useProfile();
  const ids = useMemo(() => list.items.map((card) => card.job_id), [list.items]);
  const open = useCallback((jobId: number) => { write({ job: jobId }); }, [write]);
  const keyboard = useListKeyboard(ids, open);
  const resetFilters = () => { write((current) => ({ ...DEFAULT_SEARCH, ...(current.label === 1 ? { label: 1 } : {}) })); };

  // Load more's button leaves with the last page: focus then goes to the first card it brought
  const firstNew = useRef<number | null>(null);
  const { focusCard, headingRef } = keyboard;
  useLayoutEffect(() => {
    const at = firstNew.current;
    if (at !== null && ids.length > at) {
      firstNew.current = null;
      if (focusDropped()) {
        focusCard(ids[at]);
      }
    }
  }, [ids, focusCard]);

  const filtered = toApiParams({ ...search, sort: DEFAULT_SEARCH.sort }).toString() !== "";
  const noProfile = profile.data !== undefined && profile.data.active === null;
  let content;
  if (list.pending) {
    content = <Skeleton variant="card" count={SKELETON_CARDS} />;
  } else if (list.error !== null) {
    content = <ProblemBox error={list.error} onRetry={list.retry} />;
  } else if (list.items.length === 0) {
    content = filtered ? (
      <EmptyState title={t("jobs.no_match")}
        action={<button type="button" className={styles.button} onClick={resetFilters}>{t("jobs.reset_filters")}</button>} />
    ) : (
      <EmptyState title={t("jobs.empty_title")}>{t("jobs.empty_body")}</EmptyState>
    );
  } else {
    content = (
      <>
        <ol role="list" className={styles.list} aria-labelledby={headingId}>
          {list.items.map((card) => (
            <li key={card.job_id} {...keyboard.itemProps(card.job_id)}>
              <JobCard card={card} {...keyboard.cardProps(card.job_id)} />
            </li>
          ))}
        </ol>
        {list.loadMoreError !== null && <ProblemBox error={list.loadMoreError} onRetry={list.loadMore} />}
        {list.hasNextPage && list.loadMoreError === null && (
          <div className={styles.more}>
            <button type="button" className={styles.button} aria-disabled={list.loadingMore}
              onClick={() => {
                if (!list.loadingMore) {
                  firstNew.current = ids.length;
                  list.loadMore();
                }
              }}>
              {list.loadingMore ? t("jobs.loading_more") : t("jobs.load_more")}
            </button>
          </div>
        )}
      </>
    );
  }
  return (
    <div className={styles.page}>
      <h1 id={headingId} ref={headingRef} tabIndex={-1} className={pageStyles.title}>{t("jobs.title")}</h1>
      <div className={styles.layout}>
        <FilterPanel />
        <div className={styles.results}>
          <div className={styles.toolbar}>
            {list.counts !== null && <CountsLine counts={list.counts} />}
            <div className={styles.sort}>
              <label htmlFor={sortId}>{t("jobs.sort")}</label>
              <select id={sortId} className={styles.select} value={search.sort}
                onChange={(event) => {
                  const sort = SORTS.find((value) => value === event.target.value);
                  if (sort !== undefined) {
                    write({ sort });
                  }
                }}>
                {SORTS.map((value) => <option key={value} value={value}>{t(SORT_LABELS[value])}</option>)}
              </select>
            </div>
          </div>
          <div role="status" aria-label={t("jobs.ranking_status")} className={styles.status}>
            {list.rankingChanged && (
              <button type="button" className={styles.ranking}
                onClick={() => {
                  list.refresh();
                  headingRef.current?.focus(); // the button leaves with the banner
                }}>
                {t("jobs.ranking_changed")}
              </button>
            )}
          </div>
          {noProfile && (
            <p className={styles.notice}>
              <Link to="/settings" hash="cv">{t("jobs.no_profile")}</Link>
            </p>
          )}
          {content}
        </div>
      </div>
    </div>
  );
}
