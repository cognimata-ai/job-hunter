import { createMemoryHistory } from "@tanstack/react-router";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { App, createApp } from "./app/App";
import de from "./i18n/de.json";
import en from "./i18n/en.json";
import i18n from "./i18n/index";
import userEvent from "@testing-library/user-event";
import { fakeApi } from "./test/api";
import { parseSearch, stringifySearch } from "./router";

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

  it("scroll a fresh load of the settings to the part its hash names, once the session is read", async () => {
    // The router scrolls to a hash when the route renders, which a fresh load does behind the session read's
    // spinner, before the part exists (plan M5 Task 33, the visual pass).
    fakeApi({ signedIn: true });
    const scrolled = vi.spyOn(Element.prototype, "scrollIntoView");
    at("/settings#privacy-statement");
    expect(await screen.findByRole("heading", { level: 2, name: en["privacy_statement.title"] })).toBeTruthy();
    await waitFor(() => {
      expect(scrolled.mock.contexts.map((element) => (element as Element).id)).toContain("privacy-statement");
    });
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

  it("the router serialises sets as repeated keys and keeps strings", async () => {
    expect(stringifySearch({ work_mode: ["remote", "hybrid"], loc_id: "geonames:2950159", q: "123", score_min: 40, include_remote: false, job: undefined, x: null })).toBe(
      "?work_mode=remote&work_mode=hybrid&loc_id=geonames%3A2950159&q=123&score_min=40&include_remote=false",
    );
    expect(stringifySearch({})).toBe("");
    expect(parseSearch("?work_mode=remote&work_mode=hybrid&loc_id=geonames:2950159&q=123&e=&t=true")).toEqual({
      work_mode: ["remote", "hybrid"], // repeated: an array
      loc_id: "geonames:2950159", // once: a string, not JSON
      q: "123", // a numeric-looking value stays a string
      e: "",
      t: "true",
    });
    expect(parseSearch("q=%22quoted%22+text")).toEqual({ q: '"quoted" text' });
    fakeApi({ signedIn: true });
    const app = at("/settings");
    await screen.findByRole("heading", { level: 1, name: en["settings.title"] });
    await app.router.navigate({ to: "/", search: { work_mode: ["remote", "hybrid"], q: "007", loc_id: "geonames:2950159", loc_lat: 52.5, loc_lon: 13.4, radius_km: 30 } });
    const { searchStr, search } = app.router.state.location;
    expect(new URLSearchParams(searchStr).getAll("work_mode")).toEqual(["remote", "hybrid"]);
    expect(searchStr).not.toContain("%22");
    expect(search).toMatchObject({ work_mode: ["remote", "hybrid"], q: "007", loc_id: "geonames:2950159", radius_km: "30" });
  });

  it("a login redirect with search and hash survives", async () => {
    fakeApi({ loginAnswers: [() => new Response(null, { status: 204 })] });
    const app = at("/settings?a=1&b=2#x");
    await waitFor(() => {
      expect(app.router.state.location.pathname).toBe("/login");
    });
    expect(app.router.state.location.search).toEqual({ redirect: "/settings?a=1&b=2#x" });
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText(en["login.username"]), "smoke");
    await user.type(screen.getByLabelText(en["login.password"]), "correct horse");
    await user.click(screen.getByRole("button", { name: en["login.submit"] }));
    await waitFor(() => {
      expect(app.router.state.location.href).toBe("/settings?a=1&b=2#x");
    });
    expect(app.router.state.location.search).toEqual({ a: "1", b: "2" });
    expect(app.router.state.location.hash).toBe("x");
    expect(await screen.findByRole("heading", { level: 1, name: en["settings.title"] })).toBeTruthy();
  });

  it("show a translated page for an unknown path", async () => {
    fakeApi({ signedIn: true });
    at("/nowhere");
    expect(await screen.findByRole("heading", { level: 1, name: en["not_found.title"] })).toBeTruthy();
  });
});
