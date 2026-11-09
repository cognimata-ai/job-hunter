// The Jobs list, page by page (spec §14.3, §15.3; plan M4 Task 23, Rulings d and T23-b): `GET /api/jobs` under
// the search's filters (`toApiParams`), 50 cards a page, each later page from the page before's `next_cursor`.
//
// - Its key is `["jobs", "list", <params>]`, under `JOBS_KEY`: the user's own writes invalidate it, and the
//   loaded pages are read again in a chain, each from the fresh page before's cursor, so an own write never
//   raises "Ranking changed".
// - Nothing else reads it again: not focus, not a reconnect, and not age while it is shown. A silent reorder
//   under the reader is what the banner is for: a later page whose cursor's ranking generation has passed says
//   `ranking_changed`, and `refresh()` starts again from page 1.
// - A cursor the api refuses (400 `invalid_cursor`: a restart with a new key, say) starts again from page 1
//   too, silently: no problem box, no toast (a 4xx is never the query client's toast).
// - The cards are the pages' in order, a job listed twice (it moved down between two pages) once.
import { type InfiniteData, useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo } from "react";
import { apiFetch, isProblem } from "../api/client";
import type { components } from "../api/schema";
import { JOBS_KEY, type JobCardOut, type JobsPageOut } from "./api";
import { type JobsSearch, toApiParams } from "./search";

export type CountsOut = components["schemas"]["CountsOut"];

export const PAGE_LIMIT = 50;
/** How long a read list stays fresh: longer than the query cache keeps an unshown one (5 min), so a list is
 * read again only by a write, a refresh or a change of filters. */
export const LIST_STALE_MS = 10 * 60_000;

/** The list's query key for these API params. */
export function listKey(params: string): readonly [string, "list", string] {
  return [...JOBS_KEY, "list", params];
}

export interface JobsList {
  /** The loaded cards. */
  items: JobCardOut[];
  /** The first page's counts; `null` until it is read. */
  counts: CountsOut | null;
  /** Whether a page after the first says the ranking has moved since that first page. */
  rankingChanged: boolean;
  /** The first page is being read (nothing to show yet). */
  pending: boolean;
  /** The first page failed (with nothing to show). */
  error: unknown;
  hasNextPage: boolean;
  loadingMore: boolean;
  /** The last Load more failed; the loaded cards stay. */
  loadMoreError: unknown;
  loadMore: () => void;
  /** Reads the first page again after its failure. */
  retry: () => void;
  /** Starts again from page 1 (the Ranking changed banner). */
  refresh: () => void;
}

function cards(data: InfiniteData<JobsPageOut> | undefined): JobCardOut[] {
  const seen = new Set<number>();
  const items: JobCardOut[] = [];
  for (const card of data?.pages.flatMap((page) => page.items) ?? []) {
    if (!seen.has(card.job_id)) {
      seen.add(card.job_id);
      items.push(card);
    }
  }
  return items;
}

export function useJobsList(s: JobsSearch): JobsList {
  const queryClient = useQueryClient();
  const params = toApiParams(s).toString();
  const queryKey = useMemo(() => listKey(params), [params]);
  const query = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam, signal }) => {
      const search = new URLSearchParams(params);
      if (pageParam !== null) {
        search.set("cursor", pageParam);
      }
      search.set("limit", String(PAGE_LIMIT));
      return apiFetch<JobsPageOut>(`/api/jobs?${search.toString()}`, { signal });
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    staleTime: LIST_STALE_MS,
  });
  // a refused cursor needs pages already read: the first page sends none
  const cursorRefused = query.isError && query.data !== undefined && isProblem(query.error, "invalid_cursor");
  useEffect(() => {
    if (cursorRefused) {
      void queryClient.resetQueries({ queryKey, exact: true });
    }
  }, [cursorRefused, queryClient, queryKey]);
  const refresh = useCallback(() => {
    void queryClient.resetQueries({ queryKey, exact: true });
  }, [queryClient, queryKey]);
  const { fetchNextPage, isFetchingNextPage, refetch } = query;
  const loadMore = useCallback(() => {
    if (!isFetchingNextPage) {
      void fetchNextPage();
    }
  }, [fetchNextPage, isFetchingNextPage]);
  const retry = useCallback(() => {
    void refetch();
  }, [refetch]);
  const data = query.data;
  const items = useMemo(() => cards(data), [data]);
  return {
    items,
    counts: data?.pages.at(0)?.counts ?? null,
    rankingChanged: data?.pages.some((page) => page.ranking_changed) ?? false,
    pending: data === undefined && !query.isError,
    error: data === undefined && query.isError ? query.error : null,
    hasNextPage: query.hasNextPage,
    loadingMore: isFetchingNextPage || cursorRefused,
    loadMoreError: query.isFetchNextPageError && !cursorRefused ? query.error : null,
    loadMore,
    retry,
    refresh,
  };
}
