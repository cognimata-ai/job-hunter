// The Jobs page (spec §15.3, §15.8, §15.9; plan M4 Tasks 22 and 23): the filter panel (the page's `aside`), and
// the list under the URL's search (`useJobsList`): "<total> jobs · <unscored> unscored" ("10,000+" once the
// count is capped), the sort, "⟳ Ranking changed — refresh" when a later page says so, the cards and Load more.
//
// - While the first page is read: 8 skeleton cards. Its failure: the problem with Retry; a failed refetch keeps
//   the cards (its toast is the query client's). A failed Load more: the problem below the cards, which stay,
//   with focus on its Retry (Load more has left with the failure).
// - No cards: "No jobs match these filters" (Ruling T23-d). Under the default filters it says what they leave
//   out, since they are filters too; under others it offers Reset filters, after which focus is on the heading.
// - The Ranking changed region is rendered, empty, from the start, so its banner is announced: a live region
//   has to be in the accessibility tree before its content changes.
// - Without an active profile, "Upload your CV to start ranking" (a link to the CV section) above the cards,
//   which are still listed, unscored (§15.8).
// - The list's keyboard is `useListKeyboard`'s; Enter on a card, or a click on its title (a link to the same
//   URL, Ruling T24-f), pushes `job=<id>`. Every card is rendered through `GroupCard` (Task 25): a duplicate
//   group's card (`kind: "group"`) is framed by its group, one card under its representative's id, and any other
//   is the plain `JobCard`, so a card that stops being a group card keeps its article and the focus in it.
// - The detail drawer (Task 24, resolution e) is open while the search has `job`. Closing it goes Back when this
//   page pushed that entry (so Esc and Back agree), else (a pasted URL) replaces the URL without `job`; showing
//   another job in it (a survivor, a representative) replaces `job`. The drawer gives focus back to what held it
//   before (the card it was opened from); when that is nothing (a pasted URL), focus goes to the job's card if
//   the list shows it, else to the heading.
import { Link, useRouter } from "@tanstack/react-router";
import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import pageStyles from "../app/Pages.module.css";
import { EmptyState } from "../components/EmptyState";
import { ProblemBox } from "../components/ProblemBox";
import { Skeleton } from "../components/Skeleton";
import type { TranslationKey } from "../i18n/index";
import { DetailDrawer } from "../jobs/DetailDrawer";
import { FilterPanel } from "../jobs/FilterPanel";
import { GroupCard } from "../jobs/GroupCard";
import { DEFAULT_SEARCH, type JobSort, resetSearch, SORTS, toApiParams } from "../jobs/search";
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
  const router = useRouter();
  const list = useJobsList(search);
  const profile = useProfile();
  const ids = useMemo(() => list.items.map((card) => card.job_id), [list.items]);
  // whether this page pushed the drawer's history entry (a card opened it), so closing goes Back
  const pushedJob = useRef(false);
  const open = useCallback((jobId: number) => {
    pushedJob.current = true;
    write({ job: jobId });
  }, [write]);
  const closeDrawer = useCallback(() => {
    if (pushedJob.current) {
      pushedJob.current = false;
      router.history.back();
    } else {
      write({ job: undefined }, { replace: true });
    }
  }, [router, write]);
  const showJob = useCallback((jobId: number) => { write({ job: jobId }, { replace: true }); }, [write]);
  // a card's title links to its drawer: this search with `job=<id>`, as the router writes it
  const jobHref = useCallback(
    (jobId: number) => router.buildLocation({ to: "/", search: { ...search, job: jobId } }).href,
    [router, search],
  );
  const keyboard = useListKeyboard(ids, open);
  const { focusCard, headingRef } = keyboard;

  // after the drawer closed (by any way, Back included): when it gave focus back to nothing, the job's card or
  // the heading takes it. The drawer gives focus back in a timer of its own after it unmounts, so this runs after.
  const shownIds = useRef(ids);
  useLayoutEffect(() => {
    shownIds.current = ids;
  }, [ids]);
  const openJob = search.job;
  const drawerJob = useRef<number | undefined>(undefined);
  useEffect(() => {
    const closed = drawerJob.current;
    drawerJob.current = openJob;
    if (openJob !== undefined || closed === undefined) {
      return undefined;
    }
    pushedJob.current = false; // closed by Back: the entry is gone
    const timer = setTimeout(() => {
      if (!focusDropped()) {
        return;
      }
      if (shownIds.current.includes(closed)) {
        focusCard(closed);
      } else {
        headingRef.current?.focus();
      }
    }, 0);
    return () => { clearTimeout(timer); };
  }, [openJob, focusCard, headingRef]);
  const resetFilters = () => {
    write(resetSearch);
    headingRef.current?.focus(); // the button leaves with the empty state
  };

  // Load more's button leaves with the last page, or with a failure: focus then goes to the first card the page
  // brought, or to the failure's Retry
  const firstNew = useRef<number | null>(null);
  const moreFailed = useRef<HTMLDivElement>(null);
  const loadMore = () => {
    firstNew.current = ids.length;
    list.loadMore();
  };
  const params = toApiParams(search).toString();
  useLayoutEffect(() => {
    firstNew.current = null; // another list: no page of the old one is awaited
  }, [params]);
  useLayoutEffect(() => {
    const at = firstNew.current;
    if (at !== null && ids.length > at) {
      firstNew.current = null;
      if (focusDropped()) {
        focusCard(ids[at]);
      }
    }
  }, [ids, focusCard]);
  const failedMore = list.loadMoreError !== null;
  useLayoutEffect(() => {
    if (failedMore && focusDropped()) {
      moreFailed.current?.querySelector<HTMLElement>("button")?.focus();
    }
  }, [failedMore]);

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
      <EmptyState title={t("jobs.no_match")}>{t("jobs.defaults_hide")}</EmptyState>
    );
  } else {
    content = (
      <>
        <ol role="list" className={styles.list} aria-labelledby={headingId}>
          {list.items.map((card) => (
            <li key={card.job_id} {...keyboard.itemProps(card.job_id)}>
              <GroupCard card={card} {...keyboard.cardProps(card.job_id)} href={jobHref(card.job_id)} />
            </li>
          ))}
        </ol>
        {failedMore && (
          <div ref={moreFailed}>
            <ProblemBox error={list.loadMoreError} onRetry={loadMore} />
          </div>
        )}
        {list.hasNextPage && !failedMore && (
          <div className={styles.more}>
            <button type="button" className={styles.button} aria-disabled={list.loadingMore}
              onClick={() => {
                if (!list.loadingMore) {
                  loadMore();
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
          <div className={styles.head}>
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
          </div>
          {noProfile && (
            <p className={styles.notice}>
              <Link to="/settings" hash="cv">{t("jobs.no_profile")}</Link>
            </p>
          )}
          {content}
        </div>
      </div>
      {openJob !== undefined && <DetailDrawer jobId={openJob} onClose={closeDrawer} onNavigate={showJob} />}
    </div>
  );
}
