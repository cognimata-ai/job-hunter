// The privacy routes' data (spec §20.3-§20.5, §19.5, §14.2; plan M5 Task 20, Deviations 19, 21, 22): the export,
// the two destructive requests, the deletions' manifests and the retry of a failed purge.
//
// - **The keyed requests** (`POST /api/privacy/export`, `/delete-everything`, `/purge-source`) carry an
//   `Idempotency-Key` made once per user action (`useKeyedMutation`): kept after a network failure or a 5xx, so
//   a manual retry of a lost request sends it again and the server answers its stored 202 instead of acting
//   twice; dropped after any other answer, so the next submit is a new action. A deletion's action is its kind
//   and phrase: its password and TOTP code are no part of it (`DeletionVars.reauth` is a function, which
//   `actionId` leaves out), as the server's fingerprint leaves them out (Deviation 21), so the retry of a lost
//   request with a new code, its first one used, keeps the key.
// - **The reads** are background polls (`X-JH-Background: 1`): an export every 3 s while it runs, the deletions
//   every 10 s while one is not `shipped` (D18: done only then).
// - **The exports in use** (`GET /api/privacy/exports`, M5 follow-ups Task F5) are read when the export's card
//   shows, so after a reload it finds a running export, or a ready one before its expiry, again; it then reads that
//   export by its own route, as one it started.
// - **The retry of a failed purge** (`POST /api/system/purges/{operation_id}/retry`) is not keyed (§14.1).
// - **The statement's operator values** (`GET /api/privacy/operator`, M5 follow-ups Task F1) are read once per
//   session: the api reads its operator file only when it starts, and the query cache is emptied at the logout
//   and when a session ends (`expireSession`).
//
// `useSingleFlight` sends a destructive request at most once at a time: a second submit before React has drawn
// the busy button (a double click) is dropped.
import {
  type MutateOptions,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useRef } from "react";
import { apiSend, type ProblemError } from "../api/client";
import { useKeyedMutation } from "../api/idempotency";
import { isTransient } from "../api/queryClient";
import type { components } from "../api/schema";
import { STATUS_KEY } from "./api";
import { ATTENTION_KEY, HEALTH_KEY } from "./systemApi";

export type ExportOut = components["schemas"]["ExportOut"];
export type ManifestOut = components["schemas"]["ManifestOut"];
export type ManifestKind = ManifestOut["kind"];
export type ManifestStatus = ManifestOut["status"];
export type ManifestQueuedOut = components["schemas"]["ManifestQueuedOut"];
type ManifestsPageOut = components["schemas"]["ManifestsPageOut"];
type ExportsOut = components["schemas"]["ExportsOut"];
type ExportQueuedOut = components["schemas"]["ExportQueuedOut"];
type PurgeRetryOut = components["schemas"]["PurgeRetryOut"];
export type OperatorOut = components["schemas"]["OperatorOut"];

export const MANIFESTS_KEY = ["privacy", "manifests"] as const;
export const MANIFEST_KEY = ["privacy", "manifest"] as const;
export const EXPORT_KEY = ["privacy", "export"] as const;
export const EXPORTS_KEY = ["privacy", "exports"] as const;
export const OPERATOR_KEY = ["privacy", "operator"] as const;

/** How often a running export is read again. */
export const EXPORT_POLL_MS = 3_000;
/** How often a deletion that is not confirmed offsite is read again. */
export const MANIFEST_POLL_MS = 10_000;
/** The newest deletions the list shows. */
export const MANIFESTS_LIMIT = 20;

/** The phrases the server takes (`api/routes/privacy.py`), typed by the user exactly. */
export const PHRASES = { delete_everything: "DELETE EVERYTHING", source_purge: "PURGE ADZUNA" } as const;
export type DeletionKind = keyof typeof PHRASES;

/** The field a source purge's refusal names when Adzuna is the only enabled source (Ruling T15-4 (3)). */
export const SOURCES_ENABLED_FIELD = "payload.sources.enabled";

// --- the reads ---

/** The newest deletions, newest first (`GET /api/privacy/manifests`), read again every 10 s while one is not
 * shipped. */
export function useManifests(): UseQueryResult<ManifestsPageOut, ProblemError> {
  return useQuery({
    queryKey: MANIFESTS_KEY,
    queryFn: ({ signal }) =>
      apiSend<ManifestsPageOut>(`/api/privacy/manifests?limit=${String(MANIFESTS_LIMIT)}`, { signal, background: true }),
    refetchInterval: (query) => (query.state.data?.items.some((m) => m.status !== "shipped") === true ? MANIFEST_POLL_MS : false),
  });
}

/** One deletion (`GET /api/privacy/manifests/{id}`), read again every 10 s until it is shipped; a read that is
 * answered with a refusal (a 4xx: the answer, which a poll does not change) is not read again until a Retry. */
export function useManifest(manifestId: number): UseQueryResult<ManifestOut, ProblemError> {
  return useQuery({
    queryKey: [...MANIFEST_KEY, manifestId],
    queryFn: ({ signal }) => apiSend<ManifestOut>(`/api/privacy/manifests/${String(manifestId)}`, { signal, background: true }),
    refetchInterval: (query) => {
      if (query.state.status === "error" && !isTransient(query.state.error)) {
        return false;
      }
      return query.state.data?.status === "shipped" ? false : MANIFEST_POLL_MS;
    },
  });
}

/** The exports in use (`GET /api/privacy/exports`), newest first: the running one and the ready ones before their
 * expiry, by id, state and times only. */
export function useExportsInUse(): UseQueryResult<ExportsOut, ProblemError> {
  return useQuery({
    queryKey: EXPORTS_KEY,
    queryFn: ({ signal }) => apiSend<ExportsOut>("/api/privacy/exports", { signal }),
  });
}

/** An export's state (`GET /api/privacy/exports/{id}`), read again every 3 s while it runs; none for `null`. */
export function useExport(exportId: number | null): UseQueryResult<ExportOut, ProblemError> {
  return useQuery({
    queryKey: [...EXPORT_KEY, exportId],
    queryFn: ({ signal }) => apiSend<ExportOut>(`/api/privacy/exports/${String(exportId)}`, { signal, background: true }),
    enabled: exportId !== null,
    refetchInterval: (query) => (query.state.data?.status === "running" ? EXPORT_POLL_MS : false),
  });
}

/** The privacy statement's operator values (`GET /api/privacy/operator`), all null when the server has no
 * operator file; read once for the session (never stale, kept until the cache is emptied). */
export function useOperator(): UseQueryResult<OperatorOut, ProblemError> {
  return useQuery({
    queryKey: OPERATOR_KEY,
    queryFn: ({ signal }) => apiSend<OperatorOut>("/api/privacy/operator", { signal }),
    staleTime: Infinity,
    gcTime: Infinity,
  });
}

/** Where a ready export downloads from: the zip, as an attachment. */
export function downloadHref(exportId: number): string {
  return `/api/privacy/exports/${String(exportId)}?download=1`;
}

// --- the writes ---

const OWN = { ownToast: true } as const;

/** Ask for an export (`POST /api/privacy/export`, keyed, 202 `{export_id}`). */
export function useStartExport(): UseMutationResult<ExportQueuedOut, ProblemError, undefined> {
  return useKeyedMutation(
    (_vars: undefined, key) => apiSend<ExportQueuedOut>("/api/privacy/export", { method: "POST", idempotencyKey: key }),
    { meta: OWN },
  );
}

/** A re-authentication: the password and, with TOTP on, a code. */
export interface Reauth {
  password: string;
  totp: string | null;
}

/** A destructive request: the action (its kind and phrase) and its credentials, read when it is sent. */
export interface DeletionVars {
  kind: DeletionKind;
  confirm: string;
  reauth: () => Reauth;
}

const DELETION_PATHS = {
  delete_everything: "/api/privacy/delete-everything",
  source_purge: "/api/privacy/purge-source",
} as const satisfies Record<DeletionKind, string>;

/** Delete everything (`POST /api/privacy/delete-everything`) or purge Adzuna (`POST /api/privacy/purge-source`),
 * keyed, 202 `{manifest_id}`; once answered, the deletions are read again, and the status (its banners). */
export function useRequestDeletion(): UseMutationResult<ManifestQueuedOut, ProblemError, DeletionVars> {
  const queryClient = useQueryClient();
  return useKeyedMutation(
    ({ kind, confirm, reauth }: DeletionVars, key) => {
      const { password, totp } = reauth();
      const credentials = totp === null ? { password } : { password, totp };
      const body = kind === "source_purge" ? { source: "adzuna", confirm, ...credentials } : { confirm, ...credentials };
      return apiSend<ManifestQueuedOut>(DELETION_PATHS[kind], { method: "POST", body: JSON.stringify(body), idempotencyKey: key });
    },
    {
      meta: OWN,
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: MANIFESTS_KEY });
        void queryClient.invalidateQueries({ queryKey: STATUS_KEY });
      },
    },
  );
}

/** Retry a failed purge (`POST /api/system/purges/{operation_id}/retry`, 202): Needs attention, the status, the
 * health and the deletions are read again. */
export function useRetryPurge(): UseMutationResult<PurgeRetryOut, ProblemError, number> {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (operationId: number) =>
      apiSend<PurgeRetryOut>(`/api/system/purges/${String(operationId)}/retry`, { method: "POST" }),
    meta: OWN,
    onSuccess: () => {
      for (const queryKey of [ATTENTION_KEY, STATUS_KEY, HEALTH_KEY, MANIFESTS_KEY]) {
        void queryClient.invalidateQueries({ queryKey });
      }
    },
  });
}

/** `mutation.mutate`, at most one request at a time: a call while one is in flight is dropped. */
export function useSingleFlight<D, V>(
  mutation: UseMutationResult<D, ProblemError, V>,
): (vars: V, options?: MutateOptions<D, ProblemError, V>) => void {
  const inFlight = useRef(false);
  return (vars, options) => {
    if (inFlight.current) {
      return;
    }
    inFlight.current = true;
    mutation.mutate(vars, {
      ...options,
      onSettled: (...settled) => {
        inFlight.current = false;
        options?.onSettled?.(...settled);
      },
    });
  };
}
