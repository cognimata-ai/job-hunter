// Label mode's data (spec §15.6, §11.13; plan M4 Task 26, Ruling T26-a): the labelling queue and a label.
//
// - The queue is `GET /api/labelling/queue?limit=10` under `["labelling", "queue"]`, outside `JOBS_KEY`: a card's
//   write does not read it again. Nothing reads it again by itself either (no focus or reconnect refetch, a long
//   `staleTime`); a label does, for its progress. Label mode shows a snapshot of its items (`LabelMode`).
// - A label is `POST /api/jobs/{job_id}/feedback {kind: "label", label, comment?}` (201; 409 `conflict_state`
//   without a published score of the active tuple). It takes no `Idempotency-Key` (§14.1): a second label of the
//   job upserts its golden case. It reads the queue and the job's detail (Reveal score) again, from the
//   mutation's own callback, so it does even when Label mode has closed meanwhile.
import { useMutation, type UseMutationResult, useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { apiFetch, type ProblemError, toProblem } from "../api/client";
import type { components } from "../api/schema";
import { detailKey, type FeedbackIn, type FeedbackOut } from "./detailApi";

export type LabellingQueueOut = components["schemas"]["LabellingQueueOut"];
export type LabelItemOut = components["schemas"]["LabelItemOut"];
export type Label = FeedbackIn["label"];

export const LABELLING_QUEUE_KEY = ["labelling", "queue"] as const;
/** The ads of one batch. */
export const QUEUE_LIMIT = 10;
/** As long as the list's (`LIST_STALE_MS`): longer than the cache keeps an unshown answer, so only a label reads
 * the queue again while Label mode is shown. */
export const QUEUE_STALE_MS = 10 * 60_000;

export function useLabellingQueue(): UseQueryResult<LabellingQueueOut> {
  return useQuery({
    queryKey: LABELLING_QUEUE_KEY,
    queryFn: ({ signal }) =>
      apiFetch<LabellingQueueOut>(`/api/labelling/queue?${new URLSearchParams({ limit: String(QUEUE_LIMIT) }).toString()}`, { signal }),
    staleTime: QUEUE_STALE_MS,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

export interface LabelIn {
  label: Label;
  /** Trimmed and not empty, or absent. */
  comment?: string;
}

/** A label of the job (module comment); its failure is the card's to show. */
export function useRecordLabel(jobId: number): UseMutationResult<FeedbackOut, ProblemError, LabelIn> {
  const queryClient = useQueryClient();
  return useMutation<FeedbackOut, ProblemError, LabelIn>({
    mutationFn: async ({ label, comment }) => {
      const body: FeedbackIn = { kind: "label", label, ...(comment === undefined ? {} : { comment }) };
      try {
        return await apiFetch<FeedbackOut>(`/api/jobs/${String(jobId)}/feedback`, { method: "POST", body: JSON.stringify(body) });
      } catch (error) {
        throw toProblem(error);
      }
    },
    retry: false,
    meta: { ownToast: true }, // the card shows its failure
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: LABELLING_QUEUE_KEY });
      void queryClient.invalidateQueries({ queryKey: detailKey(jobId) });
    },
  });
}
