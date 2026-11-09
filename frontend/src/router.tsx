// The routes (spec §15): code routes under the shell. `/login` is outside the session; `/` (Jobs) and
// `/settings` (Profile & Settings, its sections by hash: #cv, #scope, #queries, #watchlist, #schedule,
// #scoring, #budget, #system, #privacy) need one, and a missing session goes to
// `/login?redirect=<path + search + hash>` (`app/RequireSession`).
//
// The search is a plain query string (plan M4 Task 23, Ruling T23-a), read as the api reads its own: a key given
// once is a string, a repeated key an array of strings (`?work_mode=remote&work_mode=hybrid`), and nothing is
// JSON (the router's default would quote `"geonames:2950159"` and turn `123` into a number). Each route's
// `validateSearch` types its strings. The Jobs route's URL holds only its non-default values: every navigation
// to it is normalised (`parseJobsSearch`) and its defaults stripped.
import type { QueryClient } from "@tanstack/react-query";
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  Link,
  type RouterHistory,
  type SearchSchemaInput,
  stripSearchParams,
} from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { AppShell } from "./app/AppShell";
import styles from "./app/Pages.module.css";
import { DEFAULT_SEARCH, type JobsSearch, parseJobsSearch } from "./jobs/search";
import { JobsPage } from "./pages/JobsPage";
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

/** The search of a query string (with or without its `?`): a key given once a string, a repeated key an
 * array of its strings in order. */
export function parseSearch(searchStr: string): Record<string, string | string[]> {
  const params = new URLSearchParams(searchStr);
  const search: Record<string, string | string[]> = {};
  for (const name of new Set(params.keys())) {
    const values = params.getAll(name);
    search[name] = values.length === 1 ? values[0] : values;
  }
  return search;
}

function textOf(value: unknown): string | null {
  return typeof value === "string" || typeof value === "number" || typeof value === "boolean" ? String(value) : null;
}

/** A search as its query string (`?…`, or `""` when empty): an array as one repeated key per item, a number or
 * a boolean as its text; `undefined`, `null` and any other value are left out. */
export function stringifySearch(search: Record<string, unknown>): string {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(search)) {
    for (const item of Array.isArray(value) ? (value as unknown[]) : [value]) {
      const text = textOf(item);
      if (text !== null) {
        params.append(name, text);
      }
    }
  }
  const searchStr = params.toString();
  return searchStr === "" ? "" : `?${searchStr}`;
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
  // A navigation names the fields it sets (`SearchSchemaInput`: the input is a partial search); the page reads
  // the whole one, each field at its default when absent or invalid.
  validateSearch: (raw: Partial<JobsSearch> & SearchSchemaInput): JobsSearch => parseJobsSearch(raw),
  // each middleware works on what the later ones return: the next search is normalised, then its defaults are
  // left out of the URL
  search: { middlewares: [stripSearchParams(DEFAULT_SEARCH), ({ search, next }) => parseJobsSearch({ ...next(search) })] },
  component: JobsPage,
});

export const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: ProfileSettingsPage,
});

export const routeTree = rootRoute.addChildren([loginRoute, jobsRoute, settingsRoute]);

export function createAppRouter({ queryClient, history }: { queryClient: QueryClient; history?: RouterHistory }) {
  return createRouter({
    routeTree,
    context: { queryClient },
    history,
    scrollRestoration: true,
    parseSearch,
    stringifySearch,
  });
}

export type AppRouter = ReturnType<typeof createAppRouter>;

declare module "@tanstack/react-router" {
  interface Register {
    router: AppRouter;
  }
}
