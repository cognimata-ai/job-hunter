// The routes (spec §15): code routes under the shell. `/login` is outside the session; `/` (Jobs) and
// `/settings` (Profile & Settings, its sections by hash: #cv, #scope, #queries, #watchlist, #schedule,
// #scoring, #budget, #system, #privacy) need one, and a missing session goes to
// `/login?redirect=<path + search + hash>` (`app/RequireSession`).
import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Link,
  type RouterHistory,
} from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { AppShell } from "./app/AppShell";
import styles from "./app/Pages.module.css";
import { LoginPage } from "./pages/LoginPage";
import { ProfileSettingsPage } from "./pages/ProfileSettingsPage";

export interface RouterContext {
  queryClient: QueryClient;
}

export interface LoginSearch {
  /** Where to go after the login (`safeRedirect` checks it). */
  redirect?: string;
}

export function parseLoginSearch(search: Record<string, unknown>): LoginSearch {
  return typeof search.redirect === "string" ? { redirect: search.redirect } : {};
}

function JobsPage() {
  const { t } = useTranslation();
  return <h1 className={styles.title}>{t("jobs.title")}</h1>;
}

function NotFoundPage() {
  const { t } = useTranslation();
  return (
    <div className={styles.notFound}>
      <h1 className={styles.title}>{t("not_found.title")}</h1>
      <Link to="/">{t("not_found.link")}</Link>
    </div>
  );
}

export const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: AppShell,
  notFoundComponent: NotFoundPage,
});

export const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: parseLoginSearch,
  component: LoginPage,
});

export const jobsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  // Task 23's parseJobsSearch replaces the identity.
  validateSearch: (search: Record<string, unknown>) => search,
  component: JobsPage,
});

export const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: ProfileSettingsPage,
});

export const routeTree = rootRoute.addChildren([loginRoute, jobsRoute, settingsRoute]);

export function createAppRouter({ queryClient, history }: { queryClient: QueryClient; history?: RouterHistory }) {
  return createRouter({ routeTree, context: { queryClient }, history, scrollRestoration: true });
}

export type AppRouter = ReturnType<typeof createAppRouter>;

declare module "@tanstack/react-router" {
  interface Register {
    router: AppRouter;
  }
}
