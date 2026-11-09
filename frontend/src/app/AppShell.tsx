// The shell of the signed-in pages (spec §15, §15.9): the header (`system/HeaderStrip`: the strip with the
// navigation, then the banners), and the page's main landmark. It reads the settings for the UI language and
// the display time zone. The login page is rendered bare (it is the only route outside the session).
import { useQuery } from "@tanstack/react-query";
import { Outlet, useMatch } from "@tanstack/react-router";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { apiFetch } from "../api/client";
import type { components } from "../api/schema";
import { Spinner } from "../components/Spinner";
import { HeaderStrip } from "../system/HeaderStrip";
import styles from "./AppShell.module.css";
import { DEFAULT_TIMEZONE, DisplayContext } from "./display";
import { RequireSession } from "./RequireSession";

// Task 20's interface: the display settings, re-exported from their own module.
export { type Display, useDisplay } from "./display";

export type SettingsOut = components["schemas"]["SettingsOut"];

export const SETTINGS_KEY = ["settings"] as const;

/** The one read of the settings (`GET /api/settings`), under `SETTINGS_KEY`; a poll passes `background`. */
export function readSettings({ signal, background }: { signal?: AbortSignal; background?: boolean } = {}): Promise<SettingsOut> {
  return apiFetch<SettingsOut>("/api/settings", { signal, background });
}

export function AppShell() {
  // by route match, as the router matches the path (without regard to case): `/LOGIN` is the login too
  const login = useMatch({ from: "/login", shouldThrow: false });
  if (login !== undefined) {
    return <Outlet />;
  }
  return (
    <RequireSession>
      <Shell />
    </RequireSession>
  );
}

function Shell() {
  const { t, i18n } = useTranslation();
  const main = useRef<HTMLElement>(null);
  const settings = useQuery({
    queryKey: SETTINGS_KEY,
    queryFn: ({ signal }) => readSettings({ signal }),
  });
  const display = settings.data?.payload.display;
  const language = display?.ui_language;
  useEffect(() => {
    if (language !== undefined && language !== i18n.language) {
      void i18n.changeLanguage(language);
    }
  }, [language, i18n]);
  if (settings.isPending) {
    return (
      <main className={styles.centred}>
        <Spinner label={t("app.loading")} />
      </main>
    );
  }
  return (
    <DisplayContext.Provider value={{ timezone: display?.timezone ?? DEFAULT_TIMEZONE }}>
      <a href="#main" className={styles.skip}
        onClick={(event) => { event.preventDefault(); main.current?.focus(); }}>
        {t("app.skip_to_main")}
      </a>
      <header className={styles.header}>
        <HeaderStrip />
      </header>
      <main id="main" ref={main} tabIndex={-1} className={styles.main}>
        <Outlet />
      </main>
    </DisplayContext.Provider>
  );
}
