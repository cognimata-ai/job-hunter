// The detail drawer's data (spec §14.2, §15.4; plan M4 Task 24, resolution b): the job's detail, its history
// page by page, and the drawer's own writes: `seen_on_open`, a feedback label and its deletion, and Unmerge. The
// toggles are the card's `useJobActions`.
//
// - The detail is `["jobs", "detail", <id>]` and the history `["jobs", "history", <id>]`, both under `JOBS_KEY`,
//   so every write of a card or of the drawer that invalidates `JOBS_KEY` reads an open drawer again.
// - `seen_on_open` changes no ranking (§7.1 L1255), so it reads nothing again: it sets `user` in the detail's
//   cache and in that job's card of every loaded list (`["jobs", "list", …]`), whose NEW marker goes (Ruling
//   T24-b). A failure costs a NEW marker until the next read of the list: it raises no toast.
// - A feedback label and its deletion invalidate the detail and Label mode's queue (`LABELLING_QUEUE_KEY`: a label
//   makes or updates the job's golden case, which the queue reads; Ruling T26-d); Unmerge invalidates `JOBS_KEY`
//   and the header's status (its needs-attention counts can change). None of these routes takes an
//   `Idempotency-Key` (§14.1).
import {
  type InfiniteData,
  useInfiniteQuery,
  useMutation,
  type UseMutationResult,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useCallback, useEffect, useMemo } from "react";
import { apiFetch, isProblem, type ProblemError, toProblem } from "../api/client";
import type { components } from "../api/schema";
import { STATUS_KEY } from "../system/api";
import { type ActionsOut, JOBS_KEY, type JobCardOut, type JobsPageOut } from "./api";

export type JobDetailOut = components["schemas"]["JobDetailOut"];
export type JobOut = components["schemas"]["JobOut"];
export type DetailScoreOut = components["schemas"]["DetailScoreOut"];
export type EvidenceOut = components["schemas"]["EvidenceOut"];
export type ContestedOut = components["schemas"]["ContestedOut"];
export type MergeEventOut = components["schemas"]["MergeEventOut"];
export type JobFeedbackOut = components["schemas"]["JobFeedbackOut"];
export type FeedbackIn = components["schemas"]["FeedbackIn"];
export type FeedbackOut = components["schemas"]["FeedbackOut"];
export type UnmergedOut = components["schemas"]["UnmergedOut"];
export type HistoryPageOut = components["schemas"]["HistoryPageOut"];
export type HistoryItemOut = components["schemas"]["HistoryItemOut"];
export type UserActionsOut = components["schemas"]["UserActionsOut"];
type CardUserOut = components["schemas"]["CardUserOut"];

export function detailKey(jobId: number): readonly [string, "detail", number] {
  return [...JOBS_KEY, "detail", jobId];
}

export function historyKey(jobId: number): readonly [string, "history", number] {
  return [...JOBS_KEY, "history", jobId];
}

/** Label mode's queue (`jobs/labelApi.ts`), outside `JOBS_KEY`: a card's write does not read it again, a label
 * does. */
export const LABELLING_QUEUE_KEY = ["labelling", "queue"] as const;

/** The prefix of every loaded list's key (`useJobsList`'s `listKey`). */
export const LISTS_KEY = [...JOBS_KEY, "list"] as const;

/** Why a failed detail read has no detail (B28): `{survivor}` for a merged job (a 404 with its survivor), `{}`
 * for one that no longer exists (a 404 without one), `null` for any other failure. */
export function goneOf(error: unknown): { survivor?: number } | null {
  if (!isProblem(error) || error.status !== 404) {
    return null;
  }
  const survivor = error.extra.merged_into_job_id;
  return typeof survivor === "number" ? { survivor } : {};
}

/** The job's detail (`GET /api/jobs/{job_id}`). A 404 is never retried (a 4xx): `goneOf` reads it. */
export function useJobDetail(jobId: number): UseQueryResult<JobDetailOut> {
  return useQuery({
    queryKey: detailKey(jobId),
    queryFn: ({ signal }) => apiFetch<JobDetailOut>(`/api/jobs/${String(jobId)}`, { signal }),
  });
}

/** A card's `user` from the job's actions after a write. */
function cardUser(row: UserActionsOut): CardUserOut {
  return {
    seen_at: row.seen_at,
    saved_at: row.saved_at,
    applied_at: row.applied_at,
    hidden_at: row.hidden_at,
    has_note: row.note !== null && row.note !== "", // the card's rule: `coalesce(u.note, '') <> ''`
  };
}

/** The pages with the job's card given `user`; the same object when no page lists the job. */
function withUser(data: InfiniteData<JobsPageOut>, jobId: number, user: CardUserOut): InfiniteData<JobsPageOut> {
  if (!data.pages.some((page) => page.items.some((card) => card.job_id === jobId))) {
    return data;
  }
  const patch = (card: JobCardOut): JobCardOut => (card.job_id === jobId ? { ...card, user } : card);
  return { ...data, pages: data.pages.map((page) => ({ ...page, items: page.items.map(patch) })) };
}

/** `PUT /api/jobs/{job_id}/actions {seen_on_open: true}` (module comment). */
export function useSeenOnOpen(): UseMutationResult<ActionsOut, unknown, number> {
  const queryClient = useQueryClient();
  return useMutation<ActionsOut, unknown, number>({
    mutationFn: (jobId) =>
      apiFetch<ActionsOut>(`/api/jobs/${String(jobId)}/actions`, { method: "PUT", body: JSON.stringify({ seen_on_open: true }) }),
    retry: false,
    meta: { ownToast: true }, // no toast at all: a failure leaves a NEW marker
    onSuccess: (out, jobId) => {
      queryClient.setQueryData<JobDetailOut>(detailKey(jobId), (detail) => (detail === undefined ? detail : { ...detail, user: out.job }));
      const user = cardUser(out.job);
      queryClient.setQueriesData<InfiniteData<JobsPageOut>>({ queryKey: LISTS_KEY }, (data) =>
        data === undefined ? data : withUser(data, jobId, user),
      );
    },
  });
}

export interface JobHistory {
  /** The loaded results, the detail's first page first. */
  items: HistoryItemOut[];
  hasNextPage: boolean;
  loadingMore: boolean;
  /** The last Load more failed; the loaded results stay. */
  loadMoreError: unknown;
  loadMore: () => void;
}

/**
 * The job's history: its first page is the detail's (`first`, so the tab reads nothing to open), later pages
 * `GET /api/jobs/{job_id}/history?cursor=…` from `next_cursor`. A refused cursor (400 `invalid_cursor`) starts
 * again from the first page, silently. The pages are dropped when the tab closes (`gcTime: 0`), so the tab opens
 * again on the detail's page of that moment; a write's invalidation reads the loaded pages again meanwhile.
 */
export function useJobHistory(jobId: number, first: HistoryPageOut): JobHistory {
  const queryClient = useQueryClient();
  const queryKey = useMemo(() => historyKey(jobId), [jobId]);
  const query = useInfiniteQuery({
    queryKey,
    queryFn: ({ pageParam, signal }) => {
      const search = pageParam === null ? "" : `?${new URLSearchParams({ cursor: pageParam }).toString()}`;
      return apiFetch<HistoryPageOut>(`/api/jobs/${String(jobId)}/history${search}`, { signal });
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    initialData: { pages: [first], pageParams: [null] },
    staleTime: Infinity,
    gcTime: 0,
  });
  const cursorRefused = query.isError && isProblem(query.error, "invalid_cursor");
  useEffect(() => {
    if (cursorRefused) {
      void queryClient.resetQueries({ queryKey, exact: true });
    }
  }, [cursorRefused, queryClient, queryKey]);
  const { fetchNextPage, isFetchingNextPage } = query;
  const loadMore = useCallback(() => {
    if (!isFetchingNextPage) {
      void fetchNextPage();
    }
  }, [fetchNextPage, isFetchingNextPage]);
  const items = useMemo(() => query.data.pages.flatMap((page) => page.items), [query.data]);
  return {
    items,
    hasNextPage: query.hasNextPage,
    loadingMore: isFetchingNextPage || cursorRefused,
    loadMoreError: query.isFetchNextPageError && !cursorRefused ? query.error : null,
    loadMore,
  };
}

/** A label of the job's score (`POST /api/jobs/{job_id}/feedback`, 201), a "wrong score" or a Label mode label:
 * it reads the detail and the labelling queue again. */
export function useRecordFeedback(jobId: number): UseMutationResult<FeedbackOut, ProblemError, FeedbackIn> {
  const queryClient = useQueryClient();
  return useMutation<FeedbackOut, ProblemError, FeedbackIn>({
    mutationFn: async (body) => {
      try {
        return await apiFetch<FeedbackOut>(`/api/jobs/${String(jobId)}/feedback`, { method: "POST", body: JSON.stringify(body) });
      } catch (error) {
        throw toProblem(error);
      }
    },
    retry: false,
    meta: { ownToast: true }, // its failure is shown where it was given
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: detailKey(jobId) });
      void queryClient.invalidateQueries({ queryKey: LABELLING_QUEUE_KEY });
    },
  });
}

/** Deletes a label of the job (`DELETE /api/feedback/{feedback_id}`, 204; the golden case stays): it reads the
 * detail and the labelling queue again. */
export function useDeleteFeedback(jobId: number): UseMutationResult<null, ProblemError, number> {
  const queryClient = useQueryClient();
  return useMutation<null, ProblemError, number>({
    mutationFn: async (feedbackId) => {
      try {
        await apiFetch<undefined>(`/api/feedback/${String(feedbackId)}`, { method: "DELETE" });
        return null; // a 204
      } catch (error) {
        throw toProblem(error);
      }
    },
    retry: false,
    meta: { ownToast: true }, // its failure is shown under the list
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: detailKey(jobId) });
      void queryClient.invalidateQueries({ queryKey: LABELLING_QUEUE_KEY });
    },
  });
}

/** Reverses a merge into the job (`POST /api/jobs/{job_id}/unmerge {merge_event_id}`, 202): it reads the jobs
 * and the header's status again. */
export function useUnmerge(jobId: number): UseMutationResult<UnmergedOut, ProblemError, number> {
  const queryClient = useQueryClient();
  return useMutation<UnmergedOut, ProblemError, number>({
    mutationFn: async (mergeEventId) => {
      try {
        return await apiFetch<UnmergedOut>(`/api/jobs/${String(jobId)}/unmerge`, {
          method: "POST",
          body: JSON.stringify({ merge_event_id: mergeEventId }),
        });
      } catch (error) {
        throw toProblem(error);
      }
    },
    retry: false,
    meta: { ownToast: true }, // its failure is shown under the merge events
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: JOBS_KEY });
      void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
    },
  });
}
