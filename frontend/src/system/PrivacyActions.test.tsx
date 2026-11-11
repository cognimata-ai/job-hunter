import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { type FakeApi, fakeApi, json, problem, requestsTo, SESSION } from "../test/api";
import { renderInProviders } from "../test/app";
import { makeFormat } from "./format";
import { DeletionStatus } from "./DeletionStatus";
import { PrivacyActions } from "./PrivacyActions";
import { makeUnits } from "./units";

type ExportOut = components["schemas"]["ExportOut"];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const DELETE = "POST /api/privacy/delete-everything";
const PURGE = "POST /api/privacy/purge-source";
const EXPORT = "POST /api/privacy/export";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

function withTotp(api: FakeApi): void {
  api.routes.set("GET /api/auth/session", () => json({ ...SESSION, totp_enabled: true }));
}

/** Opens the dialog of `which` and returns it. */
async function openDialog(which: "delete" | "purge"): Promise<HTMLElement> {
  fireEvent.click(await screen.findByRole("button", { name: en[`settings_privacy.${which}.open`] }));
  return screen.getByRole("alertdialog", { name: en[`settings_privacy.${which}.dialog_title`] });
}

function field(dialog: HTMLElement, label: string): HTMLInputElement {
  return within(dialog).getByLabelText<HTMLInputElement>(label);
}

function type(input: HTMLInputElement, value: string): void {
  fireEvent.change(input, { target: { value } });
}

function submitOf(dialog: HTMLElement, which: "delete" | "purge"): HTMLButtonElement {
  return within(dialog).getByRole<HTMLButtonElement>("button", { name: en[`settings_privacy.${which}.submit`] });
}

function bodyOf(api: FakeApi, call: string, index = -1): unknown {
  return JSON.parse(requestsTo(api, call).at(index)?.body ?? "null");
}

function keysOf(api: FakeApi, call: string): (string | null)[] {
  return requestsTo(api, call).map((request) => request.headers.get("Idempotency-Key"));
}

function exportOut(status: ExportOut["status"], overrides: Partial<ExportOut> = {}): ExportOut {
  return {
    id: 5,
    status,
    created_at: "2026-11-03T07:00:00Z",
    expires_at: "2026-11-10T07:00:00Z",
    size_bytes: status === "ready" ? 58_700_000 : null,
    ...overrides,
  };
}

describe("PrivacyActions", () => {
  it("the delete dialog submits only on the exact phrase", async () => {
    const api = fakeApi({ signedIn: true });
    let answer: (response: Response) => void = () => undefined;
    api.routes.set(DELETE, () => new Promise<Response>((resolve) => { answer = resolve; }));
    renderInProviders(<><PrivacyActions /><DeletionStatus /></>);
    const dialog = await openDialog("delete");
    // §20.4: backups age out within about 3 months, the replay keeps the data from returning, done at "confirmed offsite"
    expect(dialog.textContent).toContain(en["settings_privacy.later"]);
    expect(dialog.textContent).toContain(en["settings_privacy.delete.what"]);
    expect(within(dialog).getByText("DELETE EVERYTHING").tagName).toBe("CODE");
    const phrase = field(dialog, en["settings_privacy.phrase_label"]);
    const submit = submitOf(dialog, "delete");
    type(field(dialog, en["login.password"]), "correct horse");
    expect(submit.disabled).toBe(true);
    // a prefix of the phrase is no mismatch yet; anything else is, and says so
    type(phrase, "DELETE EVERY");
    expect(submit.disabled).toBe(true);
    expect(phrase.getAttribute("aria-invalid")).toBe("false");
    for (const wrong of ["delete everything", "DELETE EVERYTHING ", " DELETE EVERYTHING", "DELETE  EVERYTHING"]) {
      type(phrase, wrong);
      expect(submit.disabled, wrong).toBe(true);
      expect(phrase.getAttribute("aria-invalid"), wrong).toBe("true");
      expect(within(dialog).getByText(en["problem.confirmation_mismatch"])).toBeTruthy();
    }
    type(phrase, "DELETE EVERYTHING");
    expect(phrase.getAttribute("aria-invalid")).toBe("false");
    expect(within(dialog).queryByText(en["problem.confirmation_mismatch"])).toBeNull();
    expect(submit.disabled).toBe(false);
    // the password is needed too
    type(field(dialog, en["login.password"]), "");
    expect(submit.disabled).toBe(true);
    type(field(dialog, en["login.password"]), "correct horse");
    expect(requestsTo(api, DELETE)).toHaveLength(0);

    // a double submit sends once: the button is busy until the answer
    fireEvent.click(submit);
    fireEvent.click(submit);
    await waitFor(() => {
      expect(submit.disabled).toBe(true);
    });
    fireEvent.click(submit);
    expect(requestsTo(api, DELETE)).toHaveLength(1);
    expect(bodyOf(api, DELETE)).toEqual({ confirm: "DELETE EVERYTHING", password: "correct horse" });
    const reads = requestsTo(api, "GET /api/privacy/manifests").length;
    answer(json({ manifest_id: 41 }, 202));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    expect(requestsTo(api, DELETE)).toHaveLength(1);
    // the deletion's status is read again, so it shows
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/privacy/manifests").length).toBeGreaterThan(reads);
    });
    expect(screen.getByText(en["settings_privacy.requested"])).toBeTruthy();
    // Cancel sends nothing, and the purge's phrase is its own
    const purge = await openDialog("purge");
    type(field(purge, en["settings_privacy.phrase_label"]), "DELETE EVERYTHING");
    type(field(purge, en["login.password"]), "correct horse");
    expect(submitOf(purge, "purge").disabled).toBe(true);
    fireEvent.click(within(purge).getByRole("button", { name: en["action.cancel"] }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(requestsTo(api, PURGE)).toHaveLength(0);
  });

  it("shows the TOTP field only when enabled", async () => {
    const api = fakeApi({ signedIn: true });
    renderInProviders(<PrivacyActions />);
    for (const which of ["delete", "purge"] as const) {
      const off = await openDialog(which);
      expect(within(off).getByLabelText(en["login.password"])).toBeTruthy();
      expect(within(off).queryByLabelText(en["login.totp"])).toBeNull();
      fireEvent.click(within(off).getByRole("button", { name: en["action.cancel"] }));
    }
    cleanup();

    withTotp(api);
    api.routes.set(PURGE, () => json({ manifest_id: 42 }, 202));
    renderInProviders(<PrivacyActions />);
    const dialog = await openDialog("purge");
    expect(dialog.textContent).toContain(en["settings_privacy.purge.what"]);
    expect(dialog.textContent).toContain(en["settings_privacy.later"]);
    const totp = await within(dialog).findByLabelText<HTMLInputElement>(en["login.totp"]);
    type(field(dialog, en["settings_privacy.phrase_label"]), "PURGE ADZUNA");
    type(field(dialog, en["login.password"]), "correct horse");
    const submit = submitOf(dialog, "purge");
    // six digits, or the submit stays off
    type(totp, "12345");
    expect(submit.disabled).toBe(true);
    type(totp, "123456");
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);
    await waitFor(() => {
      expect(requestsTo(api, PURGE)).toHaveLength(1);
    });
    expect(bodyOf(api, PURGE)).toEqual({ source: "adzuna", confirm: "PURGE ADZUNA", password: "correct horse", totp: "123456" });
  });

  it("sends one Idempotency-Key per action and reuses it on a network retry", async () => {
    const api = fakeApi({ signedIn: true });
    withTotp(api);
    const answers: (() => Response | Promise<Response>)[] = [
      () => Promise.reject(new TypeError("Failed to fetch")), // the response is lost
      () => problem(403, "reauth_failed"),
      () => json({ manifest_id: 43 }, 202),
    ];
    api.routes.set(DELETE, () => (answers.shift() ?? (() => problem(500, "internal")))());
    api.routes.set(EXPORT, () => json({ export_id: 5 }, 202));
    api.routes.set("GET /api/privacy/exports/5", () => json(exportOut("running")));
    renderInProviders(<PrivacyActions />);
    const dialog = await openDialog("delete");
    type(field(dialog, en["settings_privacy.phrase_label"]), "DELETE EVERYTHING");
    type(field(dialog, en["login.password"]), "correct horse");
    type(await within(dialog).findByLabelText<HTMLInputElement>(en["login.totp"]), "111111");
    fireEvent.click(submitOf(dialog, "delete"));
    expect(await within(dialog).findByText(en["problem_box.network"])).toBeTruthy();
    // the retry of the lost request, with a new code (the first one was used): the same key
    type(field(dialog, en["login.totp"]), "222222");
    fireEvent.click(submitOf(dialog, "delete"));
    expect(await within(dialog).findByText(en["problem.reauth_failed"])).toBeTruthy();
    // after an answer, the next submit is a new action
    type(field(dialog, en["login.totp"]), "333333");
    fireEvent.click(submitOf(dialog, "delete"));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    const keys = keysOf(api, DELETE);
    expect(keys).toHaveLength(3);
    for (const key of keys) {
      expect(key).toMatch(UUID);
    }
    expect(keys[1]).toBe(keys[0]);
    expect(keys[2]).not.toBe(keys[0]);
    expect(requestsTo(api, DELETE).map((request) => (JSON.parse(request.body ?? "{}") as { totp: string }).totp)).toEqual([
      "111111",
      "222222",
      "333333",
    ]);
    // the export is an action of its own
    fireEvent.click(screen.getByRole("button", { name: en["settings_privacy.export.start"] }));
    await waitFor(() => {
      expect(requestsTo(api, EXPORT)).toHaveLength(1);
    });
    const [exportKey] = keysOf(api, EXPORT);
    expect(exportKey).toMatch(UUID);
    expect(keys).not.toContain(exportKey);
  });

  it("shows reauth_failed and confirmation_mismatch by code", async () => {
    const api = fakeApi({ signedIn: true });
    const answers: Response[] = [
      problem(422, "confirmation_mismatch", { errors: [{ field: "body.confirm", code: "confirmation_mismatch", message: "type exactly: DELETE EVERYTHING" }] }),
      problem(403, "reauth_failed"),
      problem(429, "throttled", { retry_after_s: 540 }),
      problem(409, "conflict_state", { detail: "purge_in_progress" }),
      problem(418, "teapot", { detail: "I am a teapot, says the server" }),
      problem(409, "delete_everything_in_progress"),
    ];
    api.routes.set(DELETE, () => answers.shift() ?? problem(500, "internal"));
    renderInProviders(<PrivacyActions />);
    const dialog = await openDialog("delete");
    type(field(dialog, en["settings_privacy.phrase_label"]), "DELETE EVERYTHING");
    type(field(dialog, en["login.password"]), "correct horse");
    // each submit clears the last answer's problem, then shows its own
    const shown = async (): Promise<string> => {
      const sent = requestsTo(api, DELETE).length;
      fireEvent.click(submitOf(dialog, "delete"));
      expect(within(dialog).queryByRole("alert")).toBeNull();
      await waitFor(() => {
        expect(requestsTo(api, DELETE)).toHaveLength(sent + 1);
      });
      return (await within(dialog).findByRole("alert")).textContent;
    };
    // each by its code, never by the server's own text
    let text = await shown();
    expect(text).toContain(en["problem.confirmation_mismatch"]);
    expect(text).not.toContain("server text");
    text = await shown();
    expect(text).toContain(en["problem.reauth_failed"]);
    expect(text).not.toContain("server text");
    text = await shown();
    expect(text).toContain("Too many attempts — try again in 9 min");
    text = await shown();
    expect(text).toContain(en["problem.conflict_state"]);
    expect(within(within(dialog).getByRole("alert")).getByText("purge_in_progress").tagName).toBe("CODE");
    // a code the catalogue lacks: the generic text, not the server's title or detail
    text = await shown();
    expect(text).toContain(en["settings_privacy.error_generic"]);
    expect(text).not.toMatch(/teapot|server/);
    text = await shown();
    expect(text).toContain(en["problem.delete_everything_in_progress"]);
    expect(requestsTo(api, DELETE)).toHaveLength(6);
    fireEvent.click(within(dialog).getByRole("button", { name: en["action.cancel"] }));

    // the purge while Adzuna is the only enabled source: enable another source first
    api.routes.set(PURGE, () =>
      problem(422, "validation_failed", { errors: [{ field: "payload.sources.enabled", code: "invalid", message: "at least one source" }] }),
    );
    const purge = await openDialog("purge");
    type(field(purge, en["settings_privacy.phrase_label"]), "PURGE ADZUNA");
    type(field(purge, en["login.password"]), "correct horse");
    fireEvent.click(submitOf(purge, "purge"));
    const alert = await within(purge).findByRole("alert");
    expect(alert.textContent).toContain(en["settings_privacy.enable_another_source"]);
    expect(alert.textContent).not.toContain("at least one source");
  });

  it("export polls until ready and offers the download", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = fakeApi({ signedIn: true });
    let state = exportOut("running");
    api.routes.set(EXPORT, () => json({ export_id: 5 }, 202));
    api.routes.set("GET /api/privacy/exports/5", () => json(state));
    renderInProviders(<PrivacyActions />);
    const start = await screen.findByRole<HTMLButtonElement>("button", { name: en["settings_privacy.export.start"] });
    fireEvent.click(start);
    expect(await screen.findByText(en["settings_privacy.export.running"])).toBeTruthy();
    const reads = () => requestsTo(api, "GET /api/privacy/exports/5");
    await waitFor(() => {
      expect(reads()).toHaveLength(1);
    });
    expect(start.disabled).toBe(true); // one export at a time
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    await waitFor(() => {
      expect(reads()).toHaveLength(2);
    });
    state = exportOut("ready");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    const link = await screen.findByRole("link", { name: en["settings_privacy.export.download"] });
    expect(link.getAttribute("href")).toBe("/api/privacy/exports/5?download=1");
    // the expiry, in the display time zone, and the size
    const expires = makeFormat("en-GB", "Europe/Berlin").dateTimeMinutes("2026-11-10T07:00:00Z");
    const size = makeUnits("en-GB").bytes(58_700_000);
    expect(screen.getByText(new RegExp(`${expires}.*${size}|${size}.*${expires}`))).toBeTruthy();
    expect(start.disabled).toBe(false);
    // ready: the poll stops; every read was a background one
    const done = reads().length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_000);
    });
    expect(reads()).toHaveLength(done);
    for (const read of reads()) {
      expect(read.headers.get("X-JH-Background")).toBe("1");
    }
    expect(keysOf(api, EXPORT)[0]).toMatch(UUID);

    // a purge deleted it meanwhile: following the link reads the export again, which says so
    state = exportOut("purged");
    link.addEventListener("click", (event) => { event.preventDefault(); }); // jsdom does not navigate
    fireEvent.click(link);
    expect(await screen.findByText(en["settings_privacy.export.gone"])).toBeTruthy();
    expect(screen.queryByRole("link", { name: en["settings_privacy.export.download"] })).toBeNull();
  });
});
