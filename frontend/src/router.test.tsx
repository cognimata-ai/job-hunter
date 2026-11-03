import { createMemoryHistory } from "@tanstack/react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
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

function at(path: string) {
  const app = createApp({ history: createMemoryHistory({ initialEntries: [path] }) });
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

  it("show a translated page for an unknown path", async () => {
    fakeApi({ signedIn: true });
    at("/nowhere");
    expect(await screen.findByRole("heading", { level: 1, name: en["not_found.title"] })).toBeTruthy();
  });
});
