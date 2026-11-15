// The System section's list helpers (plan M4 Task 31): how long a run or a score cycle took, and a keyset-paged
// list's Load more (`systemApi.PagedList`), which the runs, the cycles, the alerts, the golden set and Privacy &
// data's deletions share (the deletions show a failure by its code alone: `PrivacyProblem`).
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import type { ProblemError } from "../api/client";
import { ProblemBox } from "../components/ProblemBox";
import fields from "../settings/fields.module.css";
import parts from "./SystemSection.module.css";
import type { PagedList } from "./systemApi";
import { useUnits } from "./units";

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

/** A list's Load more, with its last failure above it (the button reads the page again), shown by `problem` when
 * given. */
export function LoadMore<T>({ list, problem }: { list: PagedList<T>; problem?: (error: ProblemError) => ReactNode }) {
  const { t } = useTranslation();
  if (!list.hasMore && list.loadMoreError === null) {
    return null;
  }
  const failure = list.loadMoreError;
  return (
    <div className={parts.more}>
      {failure !== null && (problem === undefined ? <ProblemBox error={failure} /> : problem(failure))}
      <button type="button" className={fields.button} disabled={list.loadingMore} aria-busy={list.loadingMore || undefined}
        onClick={list.loadMore}>
        {t("jobs.load_more")}
      </button>
    </div>
  );
}
