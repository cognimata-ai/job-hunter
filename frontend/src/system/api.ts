// The header strip's data (spec §15, §15.1, §15.8; plan M4 Task 21): the system status, a run's detail, Run
// now, the logout and the scope's retry-apply.
//
// Polls send `X-JH-Background: 1` (D28): a poll never extends the session's idle timeout. The status is read
// every 60 s; a run every 10 s while it is `queued` or `running`, and no more once it has ended, when the
// status is read again at once (so the header's Last run follows).
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch, isProblem, type ProblemError } from "../api/client";
import { useKeyedMutation } from "../api/idempotency";
import type { components } from "../api/schema";
import { LOGIN_PATH } from "../api/session";
import { useToast } from "../components/Toaster";

export type StatusOut = components["schemas"]["StatusOut"];
export type BannerOut = components["schemas"]["BannerOut"];
export type HeaderQuotaOut = components["schemas"]["HeaderQuotaOut"];
export type RunOut = components["schemas"]["RunOut"];
export type RunDetailOut = components["schemas"]["RunDetailOut"];
export type RunNowIn = components["schemas"]["RunNowIn"];
export type RunNowOut = components["schemas"]["RunNowOut"];
export type RunStatus = RunOut["status"];
export type PartialCause = RunDetailOut["partial_causes"][number];
export type JsonValue = components["schemas"]["JsonValue"];

/**
 * The quota providers (`quota.windows.Provider`). `StatusOut.quota` is typed by `string` keys in schema.d.ts
 * (openapi-typescript drops the document's `propertyNames` enum), so the union is written here; the header
 * reads each window through `quotaOf`, and a provider missing from an answer shows as having none.
 */
export type Provider = "jsearch" | "serpapi" | "adzuna";

export const STATUS_KEY = ["system", "status"] as const;
export const runKey = (id: number | null) => ["runs", id] as const;
export const STATUS_POLL_MS = 60_000;
export const RUN_POLL_MS = 10_000;

/** Whether a run in `status` is still active: queued or running. */
export function isActive(status: RunStatus | undefined): boolean {
  return status === "queued" || status === "running";
}

/** `provider`'s current window in `status`, `null` without one. */
export function quotaOf(status: StatusOut, provider: Provider): HeaderQuotaOut | null {
  const windows: Partial<Record<Provider, HeaderQuotaOut | null>> = status.quota;
  return windows[provider] ?? null;
}

/** The header strip (`GET /api/system/status`), polled every 60 s in the background. */
export function useStatus(): UseQueryResult<StatusOut> {
  return useQuery({
    queryKey: STATUS_KEY,
    queryFn: ({ signal }) => apiFetch<StatusOut>("/api/system/status", { signal, background: true }),
    refetchInterval: STATUS_POLL_MS,
  });
}

/**
 * Run `runId` with its report (`GET /api/runs/{id}`), polled every 10 s in the background while it is queued
 * or running; none for `null`. When a polled run leaves queued and running, the status is read again.
 */
export function useRun(runId: number | null): UseQueryResult<RunDetailOut> {
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: runKey(runId),
    queryFn: ({ signal }) => apiFetch<RunDetailOut>(`/api/runs/${String(runId)}`, { signal, background: true }),
    enabled: runId !== null,
    refetchInterval: (q) => (isActive(q.state.data?.run.status) ? RUN_POLL_MS : false),
  });
  const status = query.data?.run.status;
  const previous = useRef(status);
  useEffect(() => {
    if (isActive(previous.current) && status !== undefined && !isActive(status)) {
      void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
    }
    previous.current = status;
  }, [status, queryClient]);
  return query;
}

/**
 * Run now (`POST /api/runs {include_paid}`, keyed: a retry of the same action after a 5xx or no answer sends
 * its key again, so the server replays instead of queueing twice). On a 202 the status in the cache shows the
 * answer's run as the active one until the status is read again, which happens at once.
 */
export function useRunNow(): UseMutationResult<RunNowOut, ProblemError, RunNowIn> {
  const queryClient = useQueryClient();
  return useKeyedMutation(
    (vars: RunNowIn, key) =>
      apiFetch<RunNowOut>("/api/runs", { method: "POST", body: JSON.stringify(vars), idempotencyKey: key }),
    {
      onSuccess: ({ run }) => {
        if (isActive(run.status)) {
          queryClient.setQueryData<StatusOut>(STATUS_KEY, (old) =>
            old === undefined ? old : { ...old, active_run_id: run.id },
          );
        }
        void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
      },
    },
  );
}

/**
 * The scope's retry-apply (`POST /api/settings/scope/retry-apply`, 202 `{operation_id}`). It takes no
 * `Idempotency-Key`: a repeated request finds the apply live and is refused (409 `conflict_state`). A 202
 * reads the status again.
 */
export function useRetryApply(): UseMutationResult<{ operation_id: number }, unknown, undefined> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<{ operation_id: number }>("/api/settings/scope/retry-apply", { method: "POST" }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
    },
  });
}

/**
 * The logout (`POST /api/auth/logout`, 204). An `authCall`: its 401 (the session had already ended) is an
 * answer, not an expiry. On a 204 or a 401 it goes to the login without `redirect`, then clears the cache, as
 * `expireSession` does; on any other failure (no answer, a 5xx) the user stays signed in, told by a toast.
 */
export function useLogout(): UseMutationResult<undefined, unknown, undefined> {
  const { t } = useTranslation();
  const router = useRouter();
  const queryClient = useQueryClient();
  const toast = useToast();
  const leave = () => {
    void router.navigate({ to: LOGIN_PATH, search: {}, replace: true }).then(() => {
      queryClient.clear();
    });
  };
  return useMutation({
    mutationFn: () => apiFetch<undefined>("/api/auth/logout", { method: "POST" }),
    meta: { authCall: true },
    onSuccess: leave,
    onError: (error) => {
      if (isProblem(error, "unauthenticated")) {
        leave();
      } else {
        toast({ message: t("header.logout_failed"), tone: "critical" });
      }
    },
  });
}
