// The System section's data (spec §14.2, §15.7, §17.5; plan M4 Task 31, resolutions d and e): Needs attention and
// its actions, the run history, the score cycles, the alerts and their acknowledgement, the golden set and its
// flags, and the detailed health.
//
// - **The lists** are keyset-paged (`{items, next_cursor}`, §14.3): the first page is read when the list shows,
//   Load more reads the next one. A refused cursor (400 `invalid_cursor`, a cursor of an older key) starts the list
//   again from its first page, silently.
// - **The actions of Needs attention** (`observability/attention.py`, `api/routes/system.py`, `groups.py`,
//   `targets.py`, `alerts.py`) each read Needs attention and the header's status again once they are answered
//   (and the health, whose figures they move); a refusal is the caller's to show at its item. Of these routes the
//   batch release and the target retry take an `Idempotency-Key` (§14.1; `api/routes/targets.py`: "Each route
//   needs an `Idempotency-Key`"), each keyed per action (`useKeyedMutation`); the replay, the resolve, the closure,
//   the acknowledgement and the retry-apply are not keyed.
// - **The golden set's writes** read the set again, and the golden runs' gate, which counts its active cases.
//
// Each write tells its own failure (`meta.ownToast`): its item shows the problem, so no transient toast is raised
// beside it.
import {
  type InfiniteData,
  type QueryKey,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useCallback, useEffect, useMemo } from "react";
import { apiSend, isProblem, type ProblemError } from "../api/client";
import { useKeyedMutation } from "../api/idempotency";
import type { components } from "../api/schema";
import { JOBS_KEY, SPEND_KEY } from "../jobs/api";
import { GOLDEN_RUNS_KEY } from "../settings/EvalStatus";
import { type RunOut, STATUS_KEY } from "./api";

export type AttentionOut = components["schemas"]["AttentionOut"];
export type ScoreCycleOut = components["schemas"]["ScoreCycleOut"];
export type AlertOut = components["schemas"]["AlertOut"];
export type AlertStatus = AlertOut["status"];
export type GoldenCaseOut = components["schemas"]["GoldenCaseOut"];
export type SystemHealthOut = components["schemas"]["SystemHealthOut"];
type ReplayQueuedOut = components["schemas"]["ReplayQueuedOut"];
type ConflictResolvedOut = components["schemas"]["ConflictResolvedOut"];
type TargetMovedOut = components["schemas"]["TargetMovedOut"];
type ReleasedOut = components["schemas"]["ReleasedOut"];
type SnapshotAcknowledgedOut = components["schemas"]["SnapshotAcknowledgedOut"];
type AcknowledgedOut = components["schemas"]["AcknowledgedOut"];
export type Decision = components["schemas"]["ResolveIn"]["decision"];
export type GoldenFlags = components["schemas"]["FlagsIn"];

export const ATTENTION_KEY = ["system", "needs-attention"] as const;
export const RUNS_KEY = ["system", "runs"] as const;
export const CYCLES_KEY = ["system", "score-cycles"] as const;
export const ALERTS_KEY = ["system", "alerts"] as const;
export const GOLDEN_KEY = ["system", "golden-cases"] as const;
export const HEALTH_KEY = ["system", "health"] as const;

/** How often Needs attention is read again while it shows. */
export const ATTENTION_POLL_MS = 60_000;
/** The rows a page of the run, cycle and alert lists asks for. */
export const PAGE_LIMIT = 20;
/** The golden cases a page asks for (the route's default). */
export const GOLDEN_PAGE_LIMIT = 50;

interface Page<T> {
  items: T[];
  next_cursor: string | null;
}

/** A keyset-paged list as a section shows it. */
export interface PagedList<T> {
  /** The loaded rows, `undefined` until the first page is in. */
  items: T[] | undefined;
  /** The first page's failure (with its Retry). */
  error: ProblemError | null;
  retry: () => void;
  hasMore: boolean;
  loadingMore: boolean;
  /** The last Load more failed; the loaded rows stay. */
  loadMoreError: ProblemError | null;
  loadMore: () => void;
}

function usePaged<T>(queryKey: QueryKey, path: string, params: Record<string, string>): PagedList<T> {
  const queryClient = useQueryClient();
  const query = useInfiniteQuery<Page<T>, ProblemError, InfiniteData<Page<T>>, QueryKey, string | null>({
    queryKey,
    queryFn: ({ pageParam, signal }) => {
      const search = new URLSearchParams(params);
      if (pageParam !== null) {
        search.set("cursor", pageParam);
      }
      return apiSend<Page<T>>(`${path}?${search.toString()}`, { signal });
    },
    initialPageParam: null,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });
  const cursorRefused = query.isFetchNextPageError && isProblem(query.error, "invalid_cursor");
  useEffect(() => {
    if (cursorRefused) {
      void queryClient.resetQueries({ queryKey, exact: true });
    }
  }, [cursorRefused, queryClient, queryKey]);
  const { fetchNextPage, isFetchingNextPage, refetch } = query;
  const loadMore = useCallback(() => {
    if (!isFetchingNextPage) {
      void fetchNextPage();
    }
  }, [fetchNextPage, isFetchingNextPage]);
  const items = useMemo(() => query.data?.pages.flatMap((page) => page.items), [query.data]);
  return {
    items,
    error: query.isError && query.data === undefined ? query.error : null,
    retry: () => { void refetch(); },
    hasMore: query.hasNextPage,
    loadingMore: isFetchingNextPage || cursorRefused,
    loadMoreError: query.isFetchNextPageError && !cursorRefused ? query.error : null,
    loadMore,
  };
}

// --- the reads ---

/** Needs attention (`GET /api/system/needs-attention`), read again every 60 s in the background while it shows
 * (`X-JH-Background: 1`: a poll never extends the session), so a time it shows ("can be released from") moves on. */
export function useAttention(): UseQueryResult<AttentionOut, ProblemError> {
  return useQuery({
    queryKey: ATTENTION_KEY,
    queryFn: ({ signal }) => apiSend<AttentionOut>("/api/system/needs-attention", { signal, background: true }),
    refetchInterval: ATTENTION_POLL_MS,
  });
}

/** The runs, newest first (`GET /api/runs`). */
export function useRuns(): PagedList<RunOut> {
  return usePaged<RunOut>(RUNS_KEY, "/api/runs", { limit: String(PAGE_LIMIT) });
}

/** The score cycles, newest first (`GET /api/score-cycles`). */
export function useCycles(): PagedList<ScoreCycleOut> {
  return usePaged<ScoreCycleOut>(CYCLES_KEY, "/api/score-cycles", { limit: String(PAGE_LIMIT) });
}

/** The alerts of `status`, newest first (`GET /api/alerts?status=…`). */
export function useAlerts(status: AlertStatus): PagedList<AlertOut> {
  const key = useMemo(() => [...ALERTS_KEY, status], [status]);
  return usePaged<AlertOut>(key, "/api/alerts", { status, limit: String(PAGE_LIMIT) });
}

/** The golden cases by id (`GET /api/golden-cases`). */
export function useGoldenCases(): PagedList<GoldenCaseOut> {
  return usePaged<GoldenCaseOut>(GOLDEN_KEY, "/api/golden-cases", { limit: String(GOLDEN_PAGE_LIMIT) });
}

/** The detailed health (`GET /api/system/health`, §17.5). */
export function useHealth(): UseQueryResult<SystemHealthOut, ProblemError> {
  return useQuery({
    queryKey: HEALTH_KEY,
    queryFn: ({ signal }) => apiSend<SystemHealthOut>("/api/system/health", { signal }),
  });
}

// --- Needs attention's actions ---

/** What an answered action reads again: Needs attention, the header's status and the health, and `also`. */
function useRefresh(...also: QueryKey[]): () => void {
  const queryClient = useQueryClient();
  return () => {
    for (const queryKey of [ATTENTION_KEY, STATUS_KEY, HEALTH_KEY, ...also]) {
      void queryClient.invalidateQueries({ queryKey });
    }
  };
}

const OWN = { ownToast: true } as const;

/** Replay a stored response's rejected items (`POST /api/system/raw-items/replay {raw_response_id}`, 202). */
export function useReplay(): UseMutationResult<ReplayQueuedOut, ProblemError, number> {
  const refresh = useRefresh();
  return useMutation({
    mutationFn: (rawResponseId: number) =>
      apiSend<ReplayQueuedOut>("/api/system/raw-items/replay", { method: "POST", body: JSON.stringify({ raw_response_id: rawResponseId }) }),
    meta: OWN,
    onSuccess: refresh,
  });
}

export interface ResolveVars {
  conflictId: number;
  decision: Decision;
}

/** Settle an identity conflict (`POST /api/identity-conflicts/{id}/resolve {decision}`, 202). */
export function useResolveConflict(): UseMutationResult<ConflictResolvedOut, ProblemError, ResolveVars> {
  const refresh = useRefresh(JOBS_KEY);
  return useMutation({
    mutationFn: ({ conflictId, decision }: ResolveVars) =>
      apiSend<ConflictResolvedOut>(`/api/identity-conflicts/${String(conflictId)}/resolve`, { method: "POST", body: JSON.stringify({ decision }) }),
    meta: OWN,
    onSuccess: refresh,
  });
}

/** Retry a refused or failed target (`POST /api/targets/{id}/retry`, keyed, 202). */
export function useRetryTarget(): UseMutationResult<TargetMovedOut, ProblemError, number> {
  const refresh = useRefresh(JOBS_KEY);
  return useKeyedMutation(
    (targetId: number, key) => apiSend<TargetMovedOut>(`/api/targets/${String(targetId)}/retry`, { method: "POST", idempotencyKey: key }),
    { meta: OWN, onSuccess: refresh },
  );
}

/** Release a quarantined or lost batch and re-score its targets (`POST /api/batches/{id}/release {confirm: true}`,
 * keyed, 202). */
export function useReleaseBatch(): UseMutationResult<ReleasedOut, ProblemError, number> {
  const refresh = useRefresh(JOBS_KEY, SPEND_KEY);
  return useKeyedMutation(
    (submissionId: number, key) =>
      apiSend<ReleasedOut>(`/api/batches/${String(submissionId)}/release`, {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
        idempotencyKey: key,
      }),
    { meta: OWN, onSuccess: refresh },
  );
}

/** Confirm a mass disappearance, closing its missing postings (`POST /api/ats-snapshots/{id}/acknowledge`). */
export function useConfirmClosure(): UseMutationResult<SnapshotAcknowledgedOut, ProblemError, number> {
  const refresh = useRefresh(JOBS_KEY);
  return useMutation({
    mutationFn: (snapshotId: number) =>
      apiSend<SnapshotAcknowledgedOut>(`/api/ats-snapshots/${String(snapshotId)}/acknowledge`, { method: "POST" }),
    meta: OWN,
    onSuccess: refresh,
  });
}

/** Acknowledge an open alert (`POST /api/alerts/{id}/ack`): the alerts' tabs read again too. */
export function useAckAlert(): UseMutationResult<AcknowledgedOut, ProblemError, number> {
  const refresh = useRefresh(ALERTS_KEY);
  return useMutation({
    mutationFn: (alertId: number) => apiSend<AcknowledgedOut>(`/api/alerts/${String(alertId)}/ack`, { method: "POST" }),
    meta: OWN,
    onSuccess: refresh,
  });
}

/** After the scope's retry-apply (`system/api.ts` `useRetryApply`): Needs attention and the health read again. */
export function useAfterRetryApply(): () => void {
  return useRefresh();
}

// --- the golden set ---

export interface FlagsVars {
  caseId: number;
  flags: GoldenFlags;
}

/** Set a golden case's `active` or `is_canary` flag (`PATCH /api/golden-cases/{id}`). */
export function useSetGoldenFlags(): UseMutationResult<GoldenCaseOut, ProblemError, FlagsVars> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ caseId, flags }: FlagsVars) =>
      apiSend<GoldenCaseOut>(`/api/golden-cases/${String(caseId)}`, { method: "PATCH", body: JSON.stringify(flags) }),
    meta: OWN,
    onSuccess: (updated) => {
      // the case as answered at once, then the set and the gate read again
      queryClient.setQueryData<InfiniteData<Page<GoldenCaseOut>>>(GOLDEN_KEY, (old) =>
        old === undefined
          ? old
          : { ...old, pages: old.pages.map((page) => ({ ...page, items: page.items.map((c) => (c.id === updated.id ? updated : c)) })) },
      );
      void queryClient.invalidateQueries({ queryKey: GOLDEN_KEY });
      void queryClient.invalidateQueries({ queryKey: GOLDEN_RUNS_KEY });
    },
  });
}

/** Delete a golden case and its eval rows (`DELETE /api/golden-cases/{id}`, 204). */
export function useDeleteGoldenCase(): UseMutationResult<undefined, ProblemError, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (caseId: number) => apiSend<undefined>(`/api/golden-cases/${String(caseId)}`, { method: "DELETE" }),
    meta: OWN,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: GOLDEN_KEY });
      void queryClient.invalidateQueries({ queryKey: GOLDEN_RUNS_KEY });
    },
  });
}
