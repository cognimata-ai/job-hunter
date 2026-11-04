// The settings form under test (plan M4 Task 28): `SettingsFormProvider` with the sections a test names, each in
// its `SectionFrame`, inside the app's providers (as `renderInProviders` renders them, with no router: the Budget
// section's link needs one, so its tests render the app), against the `fetch` stub of `test/api.ts`; and the
// stub's settings requests, parsed.
import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { act, render } from "@testing-library/react";
import { type ReactNode, useState } from "react";
import type { components } from "../api/schema";
import { type AppParts, createApp } from "../app/App";
import { LiveRegion } from "../components/LiveRegion";
import { Toaster } from "../components/Toaster";
import en from "../i18n/en.json";
import type { SettingsSection } from "../pages/ProfileSettingsPage";
import { QueriesSection } from "../settings/QueriesSection";
import { ScheduleSection } from "../settings/ScheduleSection";
import { ScopeSection } from "../settings/ScopeSection";
import { ScoringSection } from "../settings/ScoringSection";
import { SectionFrame } from "../settings/SectionFrame";
import { WatchlistSection } from "../settings/WatchlistSection";
import { DisplaySection } from "../system/DisplaySection";
import { PrivacySection } from "../system/PrivacySection";
import { type SettingsForm, SettingsFormProvider, useSettingsForm } from "../settings/useSettingsForm";
import { type FakeApi, type FakeRequest, requestsTo } from "./api";

type Settings = components["schemas"]["Settings"];

const BODIES: Partial<Record<SettingsSection, ReactNode>> = {
  scope: <ScopeSection />,
  queries: <QueriesSection />,
  watchlist: <WatchlistSection />,
  schedule: <ScheduleSection />,
  scoring: <ScoringSection />,
  system: <DisplaySection />,
  privacy: <PrivacySection />,
};

/** Hands the form to the test on every render (`probe.form`), as a section of a later task holds it. */
export interface Probe {
  form: SettingsForm | null;
}

function FormProbe({ probe }: { probe: Probe }) {
  probe.form = useSettingsForm();
  return null;
}

/** Shows its children until `control.hide()` (the user leaving the page). */
function Mounted({ control, children }: { control: { hide: () => void }; children: ReactNode }) {
  const [shown, setShown] = useState(true);
  control.hide = () => { setShown(false); };
  return shown ? children : null;
}

/**
 * Renders the form with `sections` (each framed, its own body or none) and a probe of the form; `unmount()`
 * removes the form and leaves the providers (the toasts). `retryDelay` replaces the queries' backoff (a test of a
 * read that fails past its retries).
 */
export function renderSettings(
  sections: readonly SettingsSection[] = ["scope", "schedule"],
  { retryDelay }: { retryDelay?: number } = {},
): AppParts & { probe: Probe; unmount: () => void } {
  const probe: Probe = { form: null };
  const control = {
    hide: () => {
      throw new Error("the form is not rendered yet");
    },
  };
  const app = createApp({ history: createMemoryHistory({ initialEntries: ["/"] }) });
  if (retryDelay !== undefined) {
    const queries = app.queryClient.getDefaultOptions().queries;
    app.queryClient.setDefaultOptions({ queries: { ...queries, retryDelay } });
  }
  render(
    <QueryClientProvider client={app.queryClient}>
      <Toaster store={app.toasts}>
        <LiveRegion>
          <Mounted control={control}>
            <SettingsFormProvider>
              {sections.map((id) => (
                <SectionFrame key={id} id={id} title={en[`settings.section.${id}`]}>
                  {BODIES[id]}
                  {id === sections[0] && <FormProbe probe={probe} />}
                </SectionFrame>
              ))}
            </SettingsFormProvider>
          </Mounted>
        </LiveRegion>
      </Toaster>
    </QueryClientProvider>,
  );
  return { ...app, probe, unmount: () => { act(() => { control.hide(); }); } };
}

/** The form the probe holds (once the settings are read). */
export function formOf(probe: Probe): SettingsForm {
  if (probe.form === null) {
    throw new Error("the form is not loaded");
  }
  return probe.form;
}

/** The documents the dry-runs sent, in order. */
export function dryRunPayloads(api: FakeApi): Settings[] {
  return requestsTo(api, "POST /api/settings/dry-run").map((r) => (JSON.parse(r.body ?? "{}") as { payload: Settings }).payload);
}

/** The saves (`POST /api/settings`), in order. */
export function saves(api: FakeApi): FakeRequest[] {
  return requestsTo(api, "POST /api/settings");
}

/** A save's body. */
export function saveBody(request: FakeRequest): {
  payload: Settings;
  base_version_id: number;
  acknowledged_warnings: string[];
  eval_override_ack?: boolean;
} {
  return JSON.parse(request.body ?? "{}") as ReturnType<typeof saveBody>;
}
