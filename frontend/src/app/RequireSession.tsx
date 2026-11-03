// The protected routes' guard (spec §15.8 "Session expired (401): redirect to Login, keeping the URL", E19).
import { type QueryClient, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "@tanstack/react-router";
import { type ReactNode, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { isProblem } from "../api/client";
import { isLoginPath, LOGIN_PATH, useSession } from "../api/session";
import { ProblemBox } from "../components/ProblemBox";
import { Spinner } from "../components/Spinner";
import type { AppRouter } from "../router";
import styles from "./AppShell.module.css";

/**
 * Sends the user to the login with the current path, search and hash as `redirect`, then clears the query
 * cache (nothing of the ended session stays). The guard and the query client's `onUnauthenticated` both
 * call it; once the login is the latest location, a later call does nothing, so a burst navigates once.
 */
export function expireSession(router: AppRouter, queryClient: QueryClient): void {
  const { pathname, href } = router.latestLocation;
  if (isLoginPath(pathname)) {
    return;
  }
  void router.navigate({ to: LOGIN_PATH, search: { redirect: href }, replace: true }).then(() => {
    queryClient.clear();
  });
}

/**
 * Renders `children` once the session read has found a session, and keeps them while a later read of it
 * fails transiently (a refetch on focus, an api restart): that failure is the toast's, not a page's (§15.8).
 * A 401 on any read, the first or a refetch, sends the user to the login; a first read that fails otherwise
 * shows the problem with [Retry].
 */
export function RequireSession({ children }: { children: ReactNode }) {
  const { t } = useTranslation();
  const session = useSession();
  const router = useRouter();
  const queryClient = useQueryClient();
  const missing = session.isError && isProblem(session.error, "unauthenticated");
  useEffect(() => {
    if (missing) {
      expireSession(router, queryClient);
    }
  }, [missing, router, queryClient]);
  if (session.data !== undefined) {
    return children;
  }
  if (session.isLoadingError && !missing) {
    return (
      <main className={styles.centred}>
        <ProblemBox error={session.error} onRetry={() => { void session.refetch(); }} />
      </main>
    );
  }
  return (
    <main className={styles.centred}>
      <Spinner label={t("app.loading")} />
    </main>
  );
}
