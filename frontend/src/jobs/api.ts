// The Jobs list's data (spec §14.2, §15.3; plan M4 Task 22, Ruling b): the types of `GET /api/jobs`, the
// spend's estimated cost of one scored ad, and the card's writes: the user's actions, Score now (also Score
// anyway, with `force`) and Retry. The list itself is `useJobsList` (Task 23).
//
// Every list query lives under `JOBS_KEY` (`["jobs", "list", <params>]`, and the facets `["jobs", "facets",
// …]`), so a write that changes a card invalidates `JOBS_KEY` and the list is read again: there is no
// optimistic update. Score now and Retry also read the header's status again, since its pending count changes.
//
// A failed write raises its own toast (`useCardFailure`), so the query client's generic transient one is not
// raised too (`meta.ownToast`). Its callbacks are the mutation's own, not `mutate`'s: they still run when the
// card has left the list meanwhile (a hidden job's Undo comes from a toast, after its card has gone).
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch, NETWORK_CODE, type ProblemError, toProblem } from "../api/client";
import { useKeyedMutation } from "../api/idempotency";
import type { components } from "../api/schema";
import { useToast } from "../components/Toaster";
import { useServerText } from "../i18n/index";
import { STATUS_KEY, type StatusOut } from "../system/api";

export type JobCardOut = components["schemas"]["JobCardOut"];
export type JobsPageOut = components["schemas"]["JobsPageOut"];
export type ScoreOut = components["schemas"]["ScoreOut"];
export type TargetOut = components["schemas"]["TargetOut"];
export type KnockoutOut = components["schemas"]["KnockoutOut"];
export type ScoringBucket = JobCardOut["bucket"];
export type SpendOut = components["schemas"]["SpendOut"];
export type ActionsOut = components["schemas"]["ActionsOut"];
export type TargetMovedOut = components["schemas"]["TargetMovedOut"];
export type FacetsOut = components["schemas"]["FacetsOut"];

export const JOBS_KEY = ["jobs"] as const;
export const SPEND_KEY = ["system", "spend"] as const;
export const SPEND_STALE_MS = 60_000;
/** The facets are counts for the filter panel: read again after a minute, or after a write. */
export const FACETS_STALE_MS = 60_000;

/**
 * The facets of the listed cards (`GET /api/jobs/facets`) under the list's API params (`toApiParams`): their
 * companies and sources with counts (Task 23's Company and Source filters), the companies by a `companyQ`
 * prefix when one is given. `enabled: false` reads nothing (a picker's search still being typed).
 */
export function useFacets(
  params: string,
  { companyQ = "", limit, enabled = true }: { companyQ?: string; limit?: number; enabled?: boolean } = {},
): UseQueryResult<FacetsOut> {
  return useQuery({
    queryKey: [...JOBS_KEY, "facets", params, companyQ, limit ?? null],
    queryFn: ({ signal }) => {
      const search = new URLSearchParams(params);
      if (companyQ !== "") {
        search.set("company_q", companyQ);
      }
      if (limit !== undefined) {
        search.set("limit", String(limit));
      }
      return apiFetch<FacetsOut>(`/api/jobs/facets?${search.toString()}`, { signal });
    },
    enabled,
    placeholderData: keepPreviousData,
    staleTime: FACETS_STALE_MS,
  });
}

/** The month's spend (`GET /api/system/spend`), for `c_hat`: one read for the page, fresh for 60 s. With `live`
 * (Budget & quota's live figures, plan M4 Task 28) it is also read again every 60 s while shown, a poll, so in the
 * background (`X-JH-Background: 1`); the two are never on one page. */
export function useSpend({ live = false }: { live?: boolean } = {}): UseQueryResult<SpendOut> {
  return useQuery({
    queryKey: SPEND_KEY,
    queryFn: ({ signal }) => apiFetch<SpendOut>("/api/system/spend", { signal, background: live }),
    staleTime: SPEND_STALE_MS,
    refetchInterval: live ? SPEND_STALE_MS : false,
  });
}

function capReached(status: StatusOut): boolean {
  return status.banners.some((banner) => banner.code === "cap_reached");
}

/**
 * Whether the header's status has a banner of code `cap_reached` (`StatusOut` has no pause reason): the status
 * the header strip reads and polls (`useStatus`), read from the query cache, so a page of cards adds no status
 * request and no poll; `false` until it has been read.
 *
 * Not a second `useQuery` of `STATUS_KEY`: each observer's render writes its options onto the shared query, so
 * an observer that never fetches (a `skipToken` query function) would leave its options for the next refetch,
 * and one with options of its own (no interval, another freshness) would change how the header's status is read.
 */
export function useCapReached(): boolean {
  const queryClient = useQueryClient();
  const subscribe = useCallback((onChange: () => void) => queryClient.getQueryCache().subscribe(onChange), [queryClient]);
  const read = () => {
    const status = queryClient.getQueryData<StatusOut>(STATUS_KEY);
    return status !== undefined && capReached(status);
  };
  return useSyncExternalStore(subscribe, read);
}

/** The translated text of a failed write: its problem's code, or the network problem's. */
function useProblemText(): (error: ProblemError) => string {
  const { t } = useTranslation();
  const serverText = useServerText();
  return useCallback(
    (error) => (error.code === NETWORK_CODE ? t("problem_box.network") : serverText("problem", error.code, error.message)),
    [t, serverText],
  );
}

/**
 * The answer to a card's failed write (Rulings f and i): a toast with the translated problem, and the list
 * read again when the card no longer is what it shows (a 404, a merged job among them, and a 409 for a state
 * that has moved). A merged job (a 404 with `merged_into_job_id`) reads "This job was merged into another".
 */
export function useCardFailure(): (error: ProblemError) => void {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  const problemText = useProblemText();
  return useCallback(
    (error) => {
      const merged = error.status === 404 && typeof error.extra.merged_into_job_id === "number";
      toast({ message: merged ? t("card.merged") : problemText(error), tone: "critical" });
      if (error.status === 404 || error.code === "conflict_state" || error.code === "filtered_requires_force") {
        void queryClient.invalidateQueries({ queryKey: JOBS_KEY });
      }
    },
    [t, toast, queryClient, problemText],
  );
}

/** What a card's quick action writes: one of the toggles, and `apply_to_group` on a group card; the drawer's
 * Seen toggle writes `seen` (Task 24). */
export interface CardActionBody {
  seen?: boolean;
  saved?: boolean;
  applied?: boolean;
  hidden?: boolean;
  apply_to_group?: true;
}

export interface JobActionVars {
  jobId: number;
  body: CardActionBody;
}

/**
 * The user's actions on a job (`PUT /api/jobs/{job_id}/actions`). The route takes no `Idempotency-Key`: a
 * repeated write leaves the same values. A success reads the list again.
 */
export function useJobActions(): UseMutationResult<ActionsOut, ProblemError, JobActionVars> {
  const queryClient = useQueryClient();
  const onFailure = useCardFailure();
  return useMutation<ActionsOut, ProblemError, JobActionVars>({
    mutationFn: async ({ jobId, body }) => {
      try {
        return await apiFetch<ActionsOut>(`/api/jobs/${String(jobId)}/actions`, {
          method: "PUT",
          body: JSON.stringify(body),
        });
      } catch (error) {
        throw toProblem(error);
      }
    },
    retry: false,
    meta: { ownToast: true },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: JOBS_KEY });
    },
    onError: onFailure,
  });
}

export interface ScoreNowVars {
  jobId: number;
  /** Score a filtered-out ad anyway (Score anyway, after its confirmation). */
  force: boolean;
}

/** After a move of the score lane: a notice, and the list and the header's status read again. */
function useMoved(): () => void {
  const { t } = useTranslation();
  const toast = useToast();
  const queryClient = useQueryClient();
  return useCallback(() => {
    toast({ message: t("card.score_now_queued") });
    void queryClient.invalidateQueries({ queryKey: JOBS_KEY });
    void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
  }, [t, toast, queryClient]);
}

/** Score now (`POST /api/jobs/{job_id}/score-now`, keyed; `{force: true}` for Score anyway, else `{}`). */
export function useScoreNow(): UseMutationResult<TargetMovedOut, ProblemError, ScoreNowVars> {
  const onMoved = useMoved();
  const onFailure = useCardFailure();
  return useKeyedMutation(
    ({ jobId, force }: ScoreNowVars, key) =>
      apiFetch<TargetMovedOut>(`/api/jobs/${String(jobId)}/score-now`, {
        method: "POST",
        body: JSON.stringify(force ? { force: true } : {}),
        idempotencyKey: key,
      }),
    { meta: { ownToast: true }, onSuccess: onMoved, onError: onFailure },
  );
}

export interface RetryVars {
  targetId: number;
}

/** Retry a refused or failed target (`POST /api/targets/{target_id}/retry`, keyed, no body). */
export function useRetryTarget(): UseMutationResult<TargetMovedOut, ProblemError, RetryVars> {
  const onMoved = useMoved();
  const onFailure = useCardFailure();
  return useKeyedMutation(
    ({ targetId }: RetryVars, key) =>
      apiFetch<TargetMovedOut>(`/api/targets/${String(targetId)}/retry`, { method: "POST", idempotencyKey: key }),
    { meta: { ownToast: true }, onSuccess: onMoved, onError: onFailure },
  );
}
