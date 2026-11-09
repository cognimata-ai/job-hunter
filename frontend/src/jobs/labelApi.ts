// Label mode's data (spec §15.6, §11.13; plan M4 Task 26, Rulings T26-a, T26-d, T26-e and T26-f): the labelling
// queue, the shuffle of a batch, and a label.
//
// - The queue is `GET /api/labelling/queue?limit=10` under `LABELLING_QUEUE_KEY` (`["labelling", "queue"]`, kept
//   beside `detailKey` in `detailApi.ts`), outside `JOBS_KEY`: a card's write does not read it again. Nothing reads
//   it again by itself (no focus or reconnect refetch, a long `staleTime`), but while Label mode shows an empty
//   batch: then it is polled every 60 s in the background, and Check again reads it as the user's own read.
//   A label, a "wrong score" and a feedback's Delete read it again (`detailApi.ts`), for its progress.
// - `shuffle` puts a batch in a random order once, when Label mode takes it: the server orders it round-robin
//   over the score bands from the top one, so its order would tell each hidden score's band (Ruling T26-e).
// - A label is `POST /api/jobs/{job_id}/feedback {kind: "label", label, comment?}` (201; 409 `conflict_state`
//   without a published score of the active tuple) through the drawer's `useRecordFeedback`, so both read the
//   same things again. It takes no `Idempotency-Key` (§14.1): a second label of the job upserts its case.
import { type MutateOptions, useQuery, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";
import { useCallback, useRef } from "react";
import { apiFetch, type ProblemError } from "../api/client";
import type { components } from "../api/schema";
import { type FeedbackIn, type FeedbackOut, LABELLING_QUEUE_KEY, useRecordFeedback } from "./detailApi";

export type LabellingQueueOut = components["schemas"]["LabellingQueueOut"];
export type LabelItemOut = components["schemas"]["LabelItemOut"];
export type Label = FeedbackIn["label"];

/** The ads of one batch. */
export const QUEUE_LIMIT = 10;
/** As long as the list's (`LIST_STALE_MS`): longer than the cache keeps an unshown answer, so only a label reads
 * the queue again while Label mode shows a batch. */
export const QUEUE_STALE_MS = 10 * 60_000;
/** How often the queue is read again while the batch is empty (scoring may still be running). */
export const QUEUE_POLL_MS = 60_000;

/**
 * `items` in a uniformly random order (Fisher–Yates over `crypto.getRandomValues`), as a new array. A 32-bit
 * draw reduced modulo `i + 1` (at most 50 items) is uniform to within 1 part in 10^7.
 */
export function shuffle<T>(items: readonly T[]): T[] {
  const out = [...items];
  const draws = new Uint32Array(out.length);
  crypto.getRandomValues(draws);
  for (let i = out.length - 1; i > 0; i--) {
    const j = draws[i] % (i + 1);
    [out[i], out[j]] = [out[j], out[i]];
  }
  return out;
}

export interface LabellingQueue {
  query: UseQueryResult<LabellingQueueOut>;
  /** Reads the queue again now, as the user's own read (no `X-JH-Background`); settles when the read is over. */
  checkAgain: () => Promise<unknown>;
}

/** The queue; `poll` (the shown batch is empty) reads it again every 60 s in the background. */
export function useLabellingQueue(poll: boolean): LabellingQueue {
  // the next read is the user's own (Check again), not a poll
  const asked = useRef(false);
  const query = useQuery({
    queryKey: LABELLING_QUEUE_KEY,
    queryFn: ({ signal }) => {
      const background = poll && !asked.current;
      asked.current = false;
      const search = new URLSearchParams({ limit: String(QUEUE_LIMIT) }).toString();
      return apiFetch<LabellingQueueOut>(`/api/labelling/queue?${search}`, { signal, background });
    },
    staleTime: QUEUE_STALE_MS,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: poll ? QUEUE_POLL_MS : false,
  });
  const { refetch } = query;
  const checkAgain = useCallback(() => {
    asked.current = true;
    return refetch();
  }, [refetch]);
  return { query, checkAgain };
}

type FeedbackOptions = MutateOptions<FeedbackOut, ProblemError, FeedbackIn>;

export interface RecordLabel {
  /** The feedback POST the label goes through: its pending state, its error and its last body (for Retry). */
  mutation: UseMutationResult<FeedbackOut, ProblemError, FeedbackIn>;
  /** Posts `{kind: "label", label, comment?}`, the comment trimmed, and absent when it is empty. */
  save: (label: Label, comment: string, options?: FeedbackOptions) => void;
}

/** A label of the job: the drawer's `useRecordFeedback` with a label's body. */
export function useRecordLabel(jobId: number): RecordLabel {
  const mutation = useRecordFeedback(jobId);
  const { mutate } = mutation;
  const save = useCallback(
    (label: Label, comment: string, options?: FeedbackOptions) => {
      const text = comment.trim();
      mutate(text === "" ? { kind: "label", label } : { kind: "label", label, comment: text }, options);
    },
    [mutate],
  );
  return { mutation, save };
}
