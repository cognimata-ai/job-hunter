// The Jobs page's search in the URL (plan M4 Task 23, Ruling e): the page and the filter panel read it from the
// router and write it back through it, so Back and Forward step through the filters.
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useCallback } from "react";
import { type JobsSearch, parseJobsSearch } from "./search";

/** A change of the search: the fields to set (`undefined` removes an optional one), or the next search. */
export type SearchChange = Partial<JobsSearch> | ((current: JobsSearch) => JobsSearch);

export interface WriteOptions {
  /** Replace the current history entry (the debounced search text, a slider) instead of pushing one. */
  replace?: boolean;
}

export type WriteSearch = (change: SearchChange, options?: WriteOptions) => void;

/** The current search, and a function that writes a change of it onto the latest one. A write keeps the
 * scroll position: the control that made it, low in the filter panel, stays in view. */
export function useJobsSearch(): { search: JobsSearch; write: WriteSearch } {
  const search = useSearch({ from: "/" });
  const navigate = useNavigate({ from: "/" });
  const write = useCallback<WriteSearch>(
    (change, { replace = false } = {}) => {
      void navigate({
        to: "/",
        // the latest search (another write may have landed since this render), read as the page reads it
        search: (latest) => {
          const current = parseJobsSearch({ ...latest });
          return typeof change === "function" ? change(current) : { ...current, ...change };
        },
        replace,
        resetScroll: false,
      });
    },
    [navigate],
  );
  return { search, write };
}
