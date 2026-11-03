// The app's TanStack Query client (spec §15.8, E19): its retry policy and its two global error paths.
import { CancelledError, MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { isProblem } from "./client";

/** The query and mutation `meta` the client reads. */
export interface AppMeta extends Record<string, unknown> {
  /** The login and the session read: a 401 is their answer, never an expired session. */
  authCall?: true;
}

declare module "@tanstack/react-query" {
  interface Register {
    queryMeta: AppMeta;
    mutationMeta: AppMeta;
  }
}

const MAX_RETRIES = 3;

/** A failure worth retrying, and worth the transient toast: no answer at all (offline, a network error, not a
 * `ProblemError`), or a 5xx. A 4xx is the answer and is never retried; a cancelled query was not a failure. */
export function isTransient(error: unknown): boolean {
  if (error instanceof CancelledError) {
    return false;
  }
  return !isProblem(error) || error.status === 0 || error.status >= 500;
}

/**
 * The client: queries retry a transient failure 3 times, after 1 s, 2 s and 4 s, and never a 4xx; mutations
 * never retry (a manual retry reuses the action's `Idempotency-Key`, `api/idempotency.ts`).
 *
 * A 401 `unauthenticated` from anything but an `authCall` calls `onUnauthenticated` once for a burst: it is
 * armed again only when an `authCall` succeeds (a login, or a session read that finds the session).
 *
 * `onTransient` (the toast, §15.8: "a non-blocking toast plus automatic retry with backoff") is called once
 * per fetch of a query, at its first transient failure, while its retries run (the cache's `failed` event);
 * a query that does not retry calls it at its one failure; its final failure after retries does not call it
 * again. A mutation calls it when it fails transiently.
 */
export function makeQueryClient({
  onUnauthenticated,
  onTransient,
}: {
  onUnauthenticated: () => void;
  onTransient: () => void;
}): QueryClient {
  let expired = false;
  const unauthenticated = (error: unknown, meta: AppMeta | undefined) => {
    if (isProblem(error, "unauthenticated") && meta?.authCall !== true && !expired) {
      expired = true;
      onUnauthenticated();
    }
  };
  const succeeded = (meta: AppMeta | undefined) => {
    if (meta?.authCall === true) {
      expired = false;
    }
  };
  const queryCache = new QueryCache({
    onError: (error, query) => {
      unauthenticated(error, query.meta);
      // a count of 1 is a failure with no retry, so no `failed` event announced it (`fetch` resets the count)
      if (query.state.fetchFailureCount === 1 && isTransient(error)) {
        onTransient();
      }
    },
    onSuccess: (_data, query) => {
      succeeded(query.meta);
    },
  });
  queryCache.subscribe((event) => {
    if (
      event.type === "updated" &&
      event.action.type === "failed" &&
      event.action.failureCount === 1 &&
      isTransient(event.action.error)
    ) {
      onTransient();
    }
  });
  return new QueryClient({
    queryCache,
    mutationCache: new MutationCache({
      onError: (error, _vars, _context, mutation) => {
        unauthenticated(error, mutation.meta);
        if (isTransient(error)) {
          onTransient();
        }
      },
      onSuccess: (_data, _vars, _context, mutation) => {
        succeeded(mutation.meta);
      },
    }),
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => failureCount < MAX_RETRIES && isTransient(error),
        retryDelay: (failureCount) => 1000 * 2 ** failureCount,
      },
      mutations: { retry: false },
    },
  });
}
