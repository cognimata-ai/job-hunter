// The settings form under test (plan M4 Task 28): `SettingsFormProvider` with the sections a test names, each in
// its `SectionFrame`, inside the app's providers (`renderInProviders`), against the `fetch` stub of `test/api.ts`;
// and the stub's settings requests, parsed.
import type { ReactNode } from "react";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import type { SettingsSection } from "../pages/ProfileSettingsPage";
import { BudgetSection } from "../settings/BudgetSection";
import { ScheduleSection } from "../settings/ScheduleSection";
import { ScopeSection } from "../settings/ScopeSection";
import { SectionFrame } from "../settings/SectionFrame";
import { type SettingsForm, SettingsFormProvider, useSettingsForm } from "../settings/useSettingsForm";
import { type FakeApi, type FakeRequest, requestsTo } from "./api";
import { renderInProviders } from "./app";
import type { AppParts } from "../app/App";

type Settings = components["schemas"]["Settings"];

const BODIES: Partial<Record<SettingsSection, ReactNode>> = {
  scope: <ScopeSection />,
  schedule: <ScheduleSection />,
  budget: <BudgetSection />,
};

/** Hands the form to the test on every render (`probe.form`), as a section of a later task holds it. */
export interface Probe {
  form: SettingsForm | null;
}

function FormProbe({ probe }: { probe: Probe }) {
  probe.form = useSettingsForm();
  return null;
}

/** Renders the form with `sections` (each framed, its own body or none), and a probe of the form. */
export function renderSettings(
  sections: readonly SettingsSection[] = ["scope", "schedule", "budget"],
): AppParts & { probe: Probe } {
  const probe: Probe = { form: null };
  const app = renderInProviders(
    <SettingsFormProvider>
      {sections.map((id) => (
        <SectionFrame key={id} id={id} title={en[`settings.section.${id}`]}>
          {BODIES[id]}
          {id === sections[0] && <FormProbe probe={probe} />}
        </SectionFrame>
      ))}
    </SettingsFormProvider>,
  );
  return { ...app, probe };
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
