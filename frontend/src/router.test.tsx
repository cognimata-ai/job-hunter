import { createMemoryHistory } from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, createApp } from "./app/App";
import de from "./i18n/de.json";
import en from "./i18n/en.json";
import i18n from "./i18n/index";
import { fakeApi } from "./test/api";

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

/** Lets the query client's notification batch (a zero-delay timer) reach React. */
function settled(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 10));
}

function at(path: string, { retryDelay }: { retryDelay?: number } = {}) {
  const app = createApp({ history: createMemoryHistory({ initialEntries: [path] }) });
  if (retryDelay !== undefined) {
    // this test's client only: the same retry policy, without its waits
    const queries = app.queryClient.getDefaultOptions().queries;
    app.queryClient.setDefaultOptions({ queries: { ...queries, retryDelay } });
  }
  render(<App app={app} />);
  return app;
}

describe("the routes", () => {
  it("send a missing session to the login with the path, search and hash", async () => {
    const api = fakeApi();
    const app = at("/settings?tab=1#budget");
    await waitFor(() => {
      expect(app.router.state.location.pathname).toBe("/login");
    });
    expect(app.router.state.location.search).toEqual({ redirect: "/settings?tab=1#budget" });
    expect(await screen.findByRole("button", { name: en["login.submit"] })).toBeTruthy();
    expect(api.calls).not.toContain("GET /api/settings");
  });

  it("send an expired session to the login once, keeping the URL, and clear the cache", async () => {
    const api = fakeApi({ signedIn: true });
    const app = at("/settings#budget");
    expect(await screen.findByRole("heading", { level: 1, name: en["settings.title"] })).toBeTruthy();
    api.signedIn = false; // the session ends on the server
    const navigate = vi.spyOn(app.router, "navigate");
    await Promise.allSettled([
      app.queryClient.refetchQueries({ queryKey: ["settings"] }),
      app.queryClient.refetchQueries({ queryKey: ["session"] }),
    ]);
    await waitFor(() => {
      expect(app.router.state.location.pathname).toBe("/login");
    });
    expect(app.router.state.location.search).toEqual({ redirect: "/settings#budget" });
    expect(navigate).toHaveBeenCalledTimes(1);
    await waitFor(() => {
      expect(app.queryClient.getQueryCache().find({ queryKey: ["settings"] })).toBeUndefined();
    });
  });

  it("keep the signed-in page while a session refetch fails transiently, and raise the toast", async () => {
    const api = fakeApi({ signedIn: true });
    const app = at("/settings", { retryDelay: 0 });
    expect(await screen.findByRole("heading", { level: 1, name: en["settings.title"] })).toBeTruthy();
    api.sessionDown = true;
    await act(async () => {
      await app.queryClient.refetchQueries({ queryKey: ["session"] });
      await settled(); // the query client tells its observers in a batch of its own
    });
    const sessionReads = api.calls.filter((call) => call === "GET /api/auth/session");
    expect(sessionReads).toHaveLength(1 + 4); // the first read, then the refetch and its 3 retries
    expect(app.queryClient.getQueryState(["session"])?.status).toBe("error");
    expect(screen.getByRole("heading", { level: 1, name: en["settings.title"] })).toBeTruthy();
    expect(screen.getByRole("navigation", { name: en["nav.label"] })).toBeTruthy();
    expect(screen.getByRole("navigation", { name: en["settings.nav_label"] })).toBeTruthy();
    expect(screen.getByText(en["toast.transient"])).toBeTruthy();
    expect(app.router.state.location.pathname).toBe("/settings");
  });

  it("show the problem only when the first session read fails", async () => {
    const api = fakeApi({ signedIn: true });
    api.sessionDown = true;
    at("/settings", { retryDelay: 0 });
    expect((await screen.findByRole("alert")).textContent).toContain(en["problem.internal"]);
    expect(screen.queryByRole("heading", { level: 1, name: en["settings.title"] })).toBeNull();
  });

  it("render the shell with its landmarks and the settings' language", async () => {
    const api = fakeApi({ signedIn: true });
    api.uiLanguage = "de";
    at("/");
    expect(await screen.findByRole("heading", { level: 1, name: de["jobs.title"] })).toBeTruthy();
    expect(document.documentElement.lang).toBe("de");
    expect(screen.getByRole("banner")).toBeTruthy();
    expect(screen.getByRole("main")).toBeTruthy();
    const nav = screen.getByRole("navigation", { name: de["nav.label"] });
    const links = within(nav).getAllByRole("link").map((a) => a.textContent);
    expect(links).toEqual([de["nav.jobs"], de["nav.settings"]]);
    expect(within(nav).getByRole("link", { name: de["nav.jobs"] }).getAttribute("aria-current")).toBe("page");
  });

  it("give the settings page its nine section anchors in order, each with its section", async () => {
    fakeApi({ signedIn: true });
    at("/settings");
    const nav = await screen.findByRole("navigation", { name: en["settings.nav_label"] });
    const hrefs = within(nav).getAllByRole("link").map((a) => a.getAttribute("href"));
    const ids = ["cv", "scope", "queries", "watchlist", "schedule", "scoring", "budget", "system", "privacy"];
    expect(hrefs).toEqual(ids.map((id) => `/settings#${id}`));
    for (const id of ids) {
      const section = document.getElementById(id);
      expect(section?.tagName, id).toBe("SECTION");
      const heading = document.getElementById(section?.getAttribute("aria-labelledby") ?? "");
      expect(heading?.textContent, id).toBe(en[`settings.section.${id}` as keyof typeof en]);
    }
  });

  it("render the login bare at any case of its path", async () => {
    const api = fakeApi({ signedIn: true });
    const app = at("/Login");
    expect(await screen.findByRole("button", { name: en["login.submit"] })).toBeTruthy();
    expect(screen.queryByRole("banner")).toBeNull(); // not inside the shell
    expect(screen.queryByRole("navigation", { name: en["nav.label"] })).toBeNull();
    expect(api.calls).not.toContain("GET /api/settings");
    expect(app.router.state.location.pathname).toBe("/Login");
  });

  it("show a translated page for an unknown path", async () => {
    fakeApi({ signedIn: true });
    at("/nowhere");
    expect(await screen.findByRole("heading", { level: 1, name: en["not_found.title"] })).toBeTruthy();
  });
});
