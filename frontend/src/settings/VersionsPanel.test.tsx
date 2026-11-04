import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { activationPreview, fakeApi, json, problem, promptSchemaVersion, requestsTo, rubricVersion, settingsRead } from "../test/api";
import { renderSettings } from "../test/settings";

afterEach(() => {
  vi.restoreAllMocks();
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const PREVIEW_2 = "GET /api/settings/rubric-versions/2/activation-preview";
const ACTIVATE_2 = "POST /api/settings/rubric-versions/2/activate";

function versions(): HTMLElement {
  const element = document.getElementById("scoring");
  if (element === null) {
    throw new Error("no scoring section");
  }
  return within(element).getByRole("region", { name: en["settings_scoring.versions"] });
}

function activated() {
  return json({ version: 13, llm_tuple_id: 5, targets_to_create: 1180, bulk_budget_usd: "22.440000" }, 201);
}

/** v1 active and v2 newer and inactive, v2's preview as given. */
function newerRubric(api: ReturnType<typeof fakeApi>, preview = activationPreview()) {
  api.rubricVersions = [rubricVersion(), rubricVersion({ id: 2, version: "v2", active: false, content_hash: "9c8b7a6f5e4d3c2b1a0f" })];
  api.routes.set(PREVIEW_2, () => json(preview));
}

async function notice(): Promise<HTMLElement> {
  return within(await waitFor(versions)).findByRole("group", { name: "New rubric v2 available" });
}

describe("VersionsPanel", () => {
  it("a newer version shows the available notice with eval and cost", async () => {
    const api = fakeApi({ signedIn: true });
    newerRubric(api);
    renderSettings(["scoring"]);
    const box = await notice();
    await waitFor(() => {
      expect(box.textContent).toContain("Eval: passed, ρ = 0.71");
    });
    expect(box.textContent).toContain("Re-scores 1,180 ads, about $41.30");
    // read once
    expect(requestsTo(api, PREVIEW_2)).toHaveLength(1);
    // the lists: each row's version or hash prefix, its date and its active marker
    const rubrics = within(versions()).getByRole("list", { name: en["settings_scoring.rubric_versions"] });
    const rows = within(rubrics).getAllByRole("listitem");
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("v1 · 3f9a0c2d"),
      expect.stringContaining("v2 · 9c8b7a6f"),
    ]);
    expect(rows[0].textContent).toContain(en["settings_scoring.version_active"]);
    expect(rows[0].textContent).toContain("20 Oct 2026");
    expect(within(rows[0]).queryByRole("button")).toBeNull(); // the active one is not activated again
    expect(within(rows[1]).getByRole("button", { name: "Activate rubric v2" })).toBeTruthy();
  });

  it("prompt/schema versions show their schema dry-run", async () => {
    const api = fakeApi({ signedIn: true });
    api.promptSchemaVersions = [
      promptSchemaVersion({ dry_run: { eval_run_id: 3, passed: true, transport: "live", finished_at: "2026-10-21T10:00:00Z" } }),
      promptSchemaVersion({ id: 2, active: false, prompt_schema_hash: "77aa88bb99cc00dd", dry_run: { eval_run_id: 4, passed: null, transport: null, finished_at: null } }),
      promptSchemaVersion({ id: 3, active: false, prompt_schema_hash: "1234567890abcdef", dry_run: null }),
    ];
    api.routes.set("GET /api/settings/prompt-schema-versions/2/activation-preview", () => json(activationPreview()));
    api.routes.set("GET /api/settings/prompt-schema-versions/3/activation-preview", () => json(activationPreview({ version_id: 3 })));
    renderSettings(["scoring"]);
    const list = await within(await waitFor(versions)).findByRole("list", { name: en["settings_scoring.prompt_versions"] });
    const rows = within(list).getAllByRole("listitem");
    expect(rows[0].textContent).toContain("a1b2c3d4");
    expect(rows[0].textContent).toContain("Schema dry-run passed (live, 21 Oct, 12:00)");
    expect(rows[1].textContent).toContain(en["settings_scoring.schema_dry_run_running"]);
    expect(rows[2].textContent).toContain(en["settings_scoring.schema_not_verified"]);
    // the newer inactive ones each have their notice
    expect(within(versions()).getAllByRole("group", { name: en["settings_scoring.new_prompt"] })).toHaveLength(2);
  });

  it("Activate shows the preview and posts a keyed activation with the ack", async () => {
    const api = fakeApi({ signedIn: true });
    newerRubric(api);
    api.routes.set(ACTIVATE_2, activated);
    renderSettings(["scoring"]);
    const user = userEvent.setup();
    await user.click(within(await notice()).getByRole("button", { name: "Activate rubric v2" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Activate rubric v2?" });
    expect(dialog.textContent).toContain(en["eval_gate.passed"]);
    expect(dialog.textContent).toContain("#7 · passed, ρ = 0.71");
    expect(dialog.textContent).toContain(en["activation.schema_verified"]);
    expect(dialog.textContent).toContain("1,180 ads, about $41.30 online or $20.65 in batches; bulk budget $22.44");
    expect(within(dialog).queryByRole("checkbox")).toBeNull(); // no override for a passed gate
    expect(requestsTo(api, PREVIEW_2)).toHaveLength(1); // the notice's read, not a second one
    const reads = requestsTo(api, "GET /api/settings").length;
    await user.click(within(dialog).getByRole("button", { name: en["activation.confirm"] }));
    await waitFor(() => {
      expect(requestsTo(api, ACTIVATE_2)).toHaveLength(1);
    });
    const [request] = requestsTo(api, ACTIVATE_2);
    expect(request.headers.get("Idempotency-Key")).toMatch(UUID);
    expect(request.headers.get("X-JH-Request")).toBe("1");
    expect(JSON.parse(request.body ?? "{}")).toEqual({ expected_rescore_ack: true });
    expect(await screen.findByText(en["activation.activated"])).toBeTruthy();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    // rebase(): the settings are read again
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/settings").length).toBeGreaterThan(reads);
    });
  });

  it("a failed gate needs the override tick", async () => {
    const api = fakeApi({ signedIn: true });
    newerRubric(api, activationPreview({ gate_state: "failed", latest_golden_run: { eval_run_id: 9, passed: false, spearman_rho: "0.402", finished_at: "2026-11-03T06:00:00Z" } }));
    api.routes.set(ACTIVATE_2, activated);
    renderSettings(["scoring"]);
    const user = userEvent.setup();
    await user.click(within(await notice()).getByRole("button", { name: "Activate rubric v2" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Activate rubric v2?" });
    expect(dialog.textContent).toContain(en["eval_gate.failed"]);
    const confirm = within(dialog).getByRole<HTMLButtonElement>("button", { name: en["activation.confirm"] });
    expect(confirm.disabled).toBe(true);
    await user.click(within(dialog).getByRole("checkbox", { name: en["activation.override"] }));
    expect(confirm.disabled).toBe(false);
    await user.click(confirm);
    await waitFor(() => {
      expect(requestsTo(api, ACTIVATE_2)).toHaveLength(1);
    });
    expect(JSON.parse(requestsTo(api, ACTIVATE_2)[0].body ?? "{}")).toEqual({ expected_rescore_ack: true, eval_override_ack: true });
  });

  it("an eval_gate_failed answer offers the tick", async () => {
    const api = fakeApi({ signedIn: true });
    newerRubric(api);
    let answers = 0;
    api.routes.set(ACTIVATE_2, () => (answers++ === 0 ? problem(409, "eval_gate_failed") : activated()));
    renderSettings(["scoring"]);
    const user = userEvent.setup();
    await user.click(within(await notice()).getByRole("button", { name: "Activate rubric v2" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Activate rubric v2?" });
    await user.click(within(dialog).getByRole("button", { name: en["activation.confirm"] }));
    const tick = await within(dialog).findByRole("checkbox", { name: en["activation.override"] });
    expect(within(dialog).getByRole<HTMLButtonElement>("button", { name: en["activation.confirm"] }).disabled).toBe(true);
    await user.click(tick);
    await user.click(within(dialog).getByRole("button", { name: en["activation.confirm"] }));
    await waitFor(() => {
      expect(requestsTo(api, ACTIVATE_2)).toHaveLength(2);
    });
    expect(JSON.parse(requestsTo(api, ACTIVATE_2)[1].body ?? "{}")).toEqual({ expected_rescore_ack: true, eval_override_ack: true });
  });

  it("an older inactive version shows no notice but can be activated", async () => {
    const api = fakeApi({ signedIn: true });
    api.rubricVersions = [rubricVersion({ active: false }), rubricVersion({ id: 2, version: "v2", active: true })];
    api.routes.set("GET /api/settings/rubric-versions/1/activation-preview", () => json(activationPreview({ version_id: 1, targets: 12 })));
    renderSettings(["scoring"]);
    const list = await within(await waitFor(versions)).findByRole("list", { name: en["settings_scoring.rubric_versions"] });
    expect(within(versions()).queryByRole("group", { name: /New rubric/ })).toBeNull();
    expect(requestsTo(api, "GET /api/settings/rubric-versions/1/activation-preview")).toHaveLength(0);
    await userEvent.setup().click(within(list).getByRole("button", { name: "Activate rubric v1" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Activate rubric v1?" });
    await waitFor(() => {
      expect(dialog.textContent).toContain("12 ads, about $41.30 online");
    });
  });

  it("schema_dry_run_required shows its text", async () => {
    const api = fakeApi({ signedIn: true });
    newerRubric(api);
    api.routes.set(ACTIVATE_2, () =>
      problem(409, "conflict_state", {
        title: "The model policy has no passed live schema dry-run for generator on claude-haiku-5-5",
        detail: "schema_dry_run_required",
      }),
    );
    renderSettings(["scoring"]);
    const user = userEvent.setup();
    await user.click(within(await notice()).getByRole("button", { name: "Activate rubric v2" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Activate rubric v2?" });
    await user.click(within(dialog).getByRole("button", { name: en["activation.confirm"] }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toContain(en["activation.schema_required"]);
    expect(alert.textContent).toContain("generator on claude-haiku-5-5");
    expect(within(dialog).queryByRole("checkbox")).toBeNull();
  });

  it("another refusal is a problem in the dialog", async () => {
    const api = fakeApi({ signedIn: true });
    newerRubric(api);
    api.routes.set(ACTIVATE_2, () => problem(409, "idempotency_in_progress"));
    renderSettings(["scoring"]);
    const user = userEvent.setup();
    await user.click(within(await notice()).getByRole("button", { name: "Activate rubric v2" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Activate rubric v2?" });
    await user.click(within(dialog).getByRole("button", { name: en["activation.confirm"] }));
    expect((await within(dialog).findByRole("alert")).textContent).toContain(en["problem.idempotency_in_progress"]);
  });

  it("an unverified schema in the preview names its models", async () => {
    const api = fakeApi({ signedIn: true });
    newerRubric(api, activationPreview({ schema_verified: false, unverified_models: ["claude-fable-5-1", "claude-haiku-5-5"] }));
    renderSettings(["scoring"]);
    await userEvent.setup().click(within(await notice()).getByRole("button", { name: "Activate rubric v2" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Activate rubric v2?" });
    expect(dialog.textContent).toContain("Not verified for claude-fable-5-1, claude-haiku-5-5");
  });

  it("activation invalidates the versions, status and jobs", async () => {
    const api = fakeApi({ signedIn: true });
    newerRubric(api);
    api.routes.set(ACTIVATE_2, activated);
    const app = renderSettings(["scoring"]);
    app.queryClient.setQueryData(["system", "status"], { stale: false });
    app.queryClient.setQueryData(["jobs", "list", "probe"], { stale: false });
    const user = userEvent.setup();
    await user.click(within(await notice()).getByRole("button", { name: "Activate rubric v2" }));
    const dialog = await screen.findByRole("alertdialog", { name: "Activate rubric v2?" });
    const lists = requestsTo(api, "GET /api/settings/rubric-versions").length;
    const schemas = requestsTo(api, "GET /api/settings/prompt-schema-versions").length;
    await user.click(within(dialog).getByRole("button", { name: en["activation.confirm"] }));
    await waitFor(() => {
      expect(requestsTo(api, "GET /api/settings/rubric-versions").length).toBeGreaterThan(lists);
    });
    expect(requestsTo(api, "GET /api/settings/prompt-schema-versions").length).toBeGreaterThan(schemas);
    expect(app.queryClient.getQueryState(["system", "status"])?.isInvalidated).toBe(true);
    expect(app.queryClient.getQueryState(["jobs", "list", "probe"])?.isInvalidated).toBe(true);
  });

  it("after the activation the form's base is the version it saved", async () => {
    const api = fakeApi({ signedIn: true });
    newerRubric(api);
    let saved = false;
    api.routes.set(ACTIVATE_2, () => {
      saved = true;
      return activated();
    });
    const base = settingsRead();
    api.routes.set("GET /api/settings", () =>
      json(saved ? { ...base, version: { ...base.version, id: 42, version_no: 13, effect_classes: ["llm_tuple"] } } : base),
    );
    const { probe } = renderSettings(["scoring"]);
    const user = userEvent.setup();
    const box = await notice();
    // an unsaved change: the rebase keeps it on the new base, with no "changed elsewhere" offer
    fireEvent.change(screen.getByRole("spinbutton", { name: en["settings_scoring.relevance_threshold"] }), { target: { value: "65" } });
    await user.click(within(box).getByRole("button", { name: "Activate rubric v2" }));
    await user.click(within(await screen.findByRole("alertdialog")).getByRole("button", { name: en["activation.confirm"] }));
    await waitFor(() => {
      expect(probe.form?.base.version.id).toBe(42);
    });
    expect(probe.form?.stale).toBe(false);
    expect(probe.form?.draft.evaluation.relevance_threshold).toBe(65);
    expect([...(probe.form?.dirty ?? [])]).toEqual(["evaluation.relevance_threshold"]);
  });
});
