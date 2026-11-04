// The settings from a spec (plan M4 Task 34, resolution f): the active document, a save through the API as the
// UI makes one (the dry-run, then the save over the active version with every warning acknowledged and a fresh
// `Idempotency-Key`, `smoke_m2.save_settings`), a change run with its restore, and a USD amount as the UI
// formats it.
import type { APIResponse, Page } from "@playwright/test";
import type { components } from "../../src/api/schema";
import { type ApiContext, getJson } from "./api";

export type SettingsOut = components["schemas"]["SettingsOut"];
export type Settings = components["schemas"]["Settings"];
export type SaveOut = components["schemas"]["SaveOut"];
type DryRunOut = components["schemas"]["DryRunOut"];

/** `GET /api/settings`: the active version and its document. */
export function readSettings(api: ApiContext): Promise<SettingsOut> {
  return getJson<SettingsOut>(api, "/api/settings");
}

export interface Saved {
  /** The save's answer (201 with a `SaveOut`; the caller asserts it). */
  response: APIResponse;
  /** The warning codes of the dry-run, all acknowledged. */
  acknowledged: string[];
}

/**
 * One settings save through the API: the active document changed by `change` (on a copy), its dry-run, which
 * must have no errors (`what` names the change in that failure), then `POST /api/settings` over the active
 * version with every warning code of the dry-run acknowledged and a fresh key.
 */
export async function saveSettings(api: ApiContext, what: string, change: (payload: Settings) => void): Promise<Saved> {
  const active = await readSettings(api);
  const payload = structuredClone(active.payload);
  change(payload);
  const dryRun = await api.post("/api/settings/dry-run", { data: { payload } });
  if (dryRun.status() !== 200) {
    throw new Error(`the dry-run of ${what} answered ${String(dryRun.status())}, not 200`);
  }
  const preview = (await dryRun.json()) as DryRunOut;
  if (preview.errors.length > 0) {
    throw new Error(`the dry-run refused ${what}: ${preview.errors.map((e) => `${e.field} ${e.code}`).join(", ")}`);
  }
  const acknowledged = preview.warnings.map((warning) => warning.code);
  const response = await api.post("/api/settings", {
    data: { payload, base_version_id: active.version.id, acknowledged_warnings: acknowledged },
    headers: { "Idempotency-Key": api.key() },
  });
  return { response, acknowledged };
}

/** `run`, then `restore` whatever `run` did (resolution f: a failure leaves the next spec a known state). When
 * both fail, the error names both, `run`'s first; a failing `restore` alone fails the spec. */
export async function withRestore<T>(run: () => Promise<T>, restore: () => Promise<void>): Promise<T> {
  let result: T;
  try {
    result = await run();
  } catch (error) {
    try {
      await restore();
    } catch (restoreError) {
      throw new Error(`${String(error)}\n… and the restore failed too: ${String(restoreError)}`, { cause: restoreError });
    }
    throw error;
  }
  await restore();
  return result;
}

/** A USD amount (a `DecimalOut` string) as the UI formats it (`system/format.ts`'s `usd`, the en locale's
 * en-GB), in the page's own `Intl`. */
export function pageUsd(page: Page, amount: string): Promise<string> {
  return page.evaluate(
    (value) => new Intl.NumberFormat("en-GB", { style: "currency", currency: "USD", currencyDisplay: "narrowSymbol" }).format(Number(value)),
    amount,
  );
}
