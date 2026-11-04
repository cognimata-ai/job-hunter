import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import de from "../../i18n/de.json";
import en from "../../i18n/en.json";
import i18n from "../../i18n/index";
import { fakeApi, json, problem, profileList, requestsTo } from "../../test/api";
import { renderInProviders, settled } from "../../test/app";
import { rescoreEstimate } from "../../test/fixtures/drafts";
import { useProfile } from "../../profile/api";
import { ConfirmModal } from "./ConfirmModal";

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

function ProfileReader() {
  useProfile();
  return null;
}

function dialog(): HTMLElement {
  return screen.getByRole("alertdialog", { name: en["cv.confirm.title"] });
}

function confirmButton(): HTMLButtonElement {
  return within(dialog()).getByRole<HTMLButtonElement>("button", { name: en["cv.confirm.confirm"] });
}

describe("ConfirmModal", () => {
  it("the sentence from the estimate", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(rescoreEstimate()));
    renderInProviders(<ConfirmModal draftId={7} onCancel={vi.fn()} onInvalid={vi.fn()} />);
    const sentence = await within(dialog()).findByText(/^Creates profile v5/);
    expect(sentence.textContent).toBe(
      "Creates profile v5 and re-scores 1,180 retained ads. Batch allocation $22.44 (40% of the remaining scoring " +
        "budget): about 211 ads in one batch, incl. their expected critic follow-ups; the other ~969 are scored " +
        "online at the monthly pace (about 53 per day). This month $38.20 / $100.00.",
    );
    expect(requestsTo(api, "GET /api/profile/drafts/7/rescore-estimate")).toHaveLength(1);
  });

  it("says it in German, with German numbers", async () => {
    await i18n.changeLanguage("de");
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(rescoreEstimate()));
    renderInProviders(<ConfirmModal draftId={7} onCancel={vi.fn()} onInvalid={vi.fn()} />);
    const modal = screen.getByRole("alertdialog", { name: de["cv.confirm.title"] });
    const sentence = await within(modal).findByText(/^Erstellt Profil v5/);
    expect(sentence.textContent).toContain("1.180");
    expect(sentence.textContent).toMatch(/Batch-Zuteilung 22,44\s\$ \(40\s%/);
    expect(sentence.textContent).toMatch(/Diesen Monat 38,20\s\$ \/ 100,00\s\$\.$/);
  });

  it("one retained ad is said in the singular", async () => {
    const api = fakeApi({ signedIn: true });
    const estimate = rescoreEstimate({ targets: 1, batch_targets: 0, online_targets: 1, online_per_day: 1 });
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(estimate));
    renderInProviders(<ConfirmModal draftId={7} onCancel={vi.fn()} onInvalid={vi.fn()} />);
    const sentence = await within(dialog()).findByText(/^Creates profile v5/);
    expect(sentence.textContent).toMatch(/^Creates profile v5 and re-scores 1 retained ad\. /);
  });

  it("no retained ad: nothing to re-score", async () => {
    const api = fakeApi({ signedIn: true });
    const estimate = rescoreEstimate({ targets: 0, batch_targets: 0, online_targets: 0, online_per_day: 0 });
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(estimate));
    renderInProviders(<ConfirmModal draftId={7} onCancel={vi.fn()} onInvalid={vi.fn()} />);
    const sentence = await within(dialog()).findByText(/^Creates profile v5/);
    expect(sentence.textContent).toBe("Creates profile v5. No retained ad needs re-scoring. This month $38.20 / $100.00.");
  });

  it("keeps Confirm disabled with a skeleton line while the estimate loads", async () => {
    const api = fakeApi({ signedIn: true });
    let answer: (response: Response) => void = () => undefined;
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => new Promise<Response>((resolve) => { answer = resolve; }));
    renderInProviders(<ConfirmModal draftId={7} onCancel={vi.fn()} onInvalid={vi.fn()} />);
    expect(within(dialog()).getByRole("status").getAttribute("aria-busy")).toBe("true");
    expect(confirmButton().disabled).toBe(true);
    fireEvent.click(confirmButton());
    answer(json(rescoreEstimate()));
    await within(dialog()).findByText(/^Creates profile v5/);
    expect(confirmButton().disabled).toBe(false);
    expect(requestsTo(api, "POST /api/profile/drafts/7/confirm")).toHaveLength(0);
  });

  it("confirm sends expected_rescore_ack with a key, once however often it is clicked", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(rescoreEstimate()));
    let answer: (response: Response) => void = () => undefined;
    api.routes.set("POST /api/profile/drafts/7/confirm", () => new Promise<Response>((resolve) => { answer = resolve; }));
    renderInProviders(<ConfirmModal draftId={7} onCancel={vi.fn()} onInvalid={vi.fn()} />);
    await within(dialog()).findByText(/^Creates profile v5/);
    fireEvent.click(confirmButton());
    await waitFor(() => {
      expect(confirmButton().getAttribute("aria-busy")).toBe("true");
    });
    expect(confirmButton().disabled).toBe(true);
    fireEvent.click(confirmButton());
    const [request, ...more] = requestsTo(api, "POST /api/profile/drafts/7/confirm");
    expect(more).toEqual([]);
    expect(JSON.parse(request.body ?? "null")).toEqual({ expected_rescore_ack: true });
    expect(request.headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
    answer(json({ profile_version_id: 13, targets_to_create: 1180, est_usd: "62.120000", bulk_budget_usd: "22.440000" }, 201));
    await settled();
  });

  it("confirm invalidates the profile, settings, status and jobs, and names the new version", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(rescoreEstimate()));
    api.routes.set("POST /api/profile/drafts/7/confirm", () =>
      json({ profile_version_id: 13, targets_to_create: 1180, est_usd: "62.120000", bulk_budget_usd: "22.440000" }, 201),
    );
    const version = { id: 13, version_no: 6, confirmed_at: "2026-11-03T07:00:00Z", purged: false, profile: null };
    api.routes.set("GET /api/profile", () => json({ ...profileList(), active: version, versions: [version] }));
    // the section reads the profile, so its invalidation reads it again
    const app = renderInProviders(
      <>
        <ProfileReader />
        <ConfirmModal draftId={7} onCancel={vi.fn()} onInvalid={vi.fn()} />
      </>,
    );
    const invalidate = vi.spyOn(app.queryClient, "invalidateQueries");
    await within(dialog()).findByText(/^Creates profile v5/);
    await userEvent.setup().click(confirmButton());
    expect(await screen.findByText(en["cv.confirm.done"].replace("{{version}}", "6"))).toBeTruthy();
    const keys = invalidate.mock.calls.map(([filters]) => filters?.queryKey);
    expect(keys).toEqual(expect.arrayContaining([["profile"], ["settings"], ["system", "status"], ["jobs"]]));
  });

  it("a 409 shows its problem in the modal; a 422 closes it with its errors", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(rescoreEstimate()));
    const answers = [
      () => problem(409, "conflict_state", { detail: "The draft is text_review; this needs it ready." }),
      () =>
        problem(422, "validation_failed", {
          errors: [{ field: "skills[0].evidence", code: "ungrounded", message: "The evidence is not in the CV text." }],
        }),
    ];
    api.routes.set("POST /api/profile/drafts/7/confirm", () => (answers.shift() ?? answers[0])());
    const onInvalid = vi.fn();
    renderInProviders(<ConfirmModal draftId={7} onCancel={vi.fn()} onInvalid={onInvalid} />);
    await within(dialog()).findByText(/^Creates profile v5/);
    fireEvent.click(confirmButton());
    const alert = await within(dialog()).findByRole("alert");
    expect(alert.textContent).toContain("The draft is text_review; this needs it ready.");
    expect(onInvalid).not.toHaveBeenCalled();
    fireEvent.click(confirmButton());
    await waitFor(() => {
      expect(onInvalid).toHaveBeenCalledWith([
        { field: "skills[0].evidence", code: "ungrounded", message: "The evidence is not in the CV text." },
      ]);
    });
  });
});
