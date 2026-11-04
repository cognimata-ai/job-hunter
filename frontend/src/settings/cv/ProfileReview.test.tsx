import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import codes from "../../i18n/codes.json";
import de from "../../i18n/de.json";
import en from "../../i18n/en.json";
import i18n from "../../i18n/index";
import { fakeApi, json, problem, requestsTo } from "../../test/api";
import { renderInProviders, settled } from "../../test/app";
import { profile, readyDraft, rescoreEstimate } from "../../test/fixtures/drafts";
import { type DraftOut, useDraft } from "./api";
import {
  CEFR_LEVELS,
  DEGREE_LEVELS,
  LANGUAGE_CODES,
  PROFILE_SENIORITIES,
  ProfileReview,
  ROLE_SENIORITIES,
  SKILL_CATEGORIES,
  SKILL_LEVELS,
  WORK_AUTHORIZATIONS,
} from "./ProfileReview";

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

function Harness({ onPaste = vi.fn() }: { onPaste?: () => void }) {
  const draft = useDraft(7);
  return draft.data === undefined ? null : <ProfileReview draftId={7} draft={draft.data} onPaste={onPaste} />;
}

function draftRoute(answer: () => DraftOut) {
  const api = fakeApi({ signedIn: true });
  api.routes.set("GET /api/profile/drafts/7", () => json(answer()));
  return api;
}

function group(name: string): HTMLElement {
  return screen.getByRole("group", { name });
}

function describedBy(element: HTMLElement): string {
  return (element.getAttribute("aria-describedby") ?? "")
    .split(" ")
    .map((id) => document.getElementById(id)?.textContent ?? "")
    .join(" ");
}

function sentDraft(api: ReturnType<typeof fakeApi>, index = 0): unknown {
  return (JSON.parse(requestsTo(api, "PUT /api/profile/drafts/7")[index].body ?? "{}") as { draft: unknown }).draft;
}

describe("ProfileReview", () => {
  it("shows the profile in the mock's order", async () => {
    draftRoute(() => readyDraft());
    renderInProviders(<Harness />);
    const form = await screen.findByRole("form", { name: en["cv.profile.title"] });
    const headings = within(form).getAllByRole("heading", { level: 4 }).map((h) => h.textContent);
    expect(headings).toEqual([
      en["cv.profile.target_titles"],
      en["cv.profile.roles"],
      en["cv.profile.skills"],
      en["cv.profile.languages"],
      en["cv.profile.education"],
      en["cv.profile.certifications"],
      en["cv.profile.domains"],
      en["cv.profile.you_only"],
    ]);
    expect(within(form).getByRole<HTMLTextAreaElement>("textbox", { name: en["cv.profile.summary"] }).value).toBe(
      "Backend engineer building Python services.",
    );
    expect(within(form).getByRole("button", { name: en["cv.profile.remove_title"].replace("{{title}}", "Backend Engineer") })).toBeTruthy();
    expect(within(form).getByText(en["cv.profile.recomputed"].replace("{{years}}", "6.4"))).toBeTruthy();
    expect(within(form).getByText("Senior Backend Engineer · 2021-03 – present · domain fintech")).toBeTruthy();
    expect(within(form).getByRole<HTMLSelectElement>("combobox", { name: en["cv.profile.work_authorization"] }).value).toBe("unspecified");
  });

  it("edits a skill and a language level and saves", async () => {
    const api = draftRoute(() => readyDraft());
    api.routes.set("PUT /api/profile/drafts/7", () =>
      json({ validation_issues: [{ path: "skills[1].evidence", code: "ungrounded", message: "Not in the CV text.", proposal: null }] }),
    );
    renderInProviders(<Harness />);
    const skill = await screen.findByRole("group", { name: en["cv.profile.skill_n"].replace("{{n}}", "1") });
    const user = userEvent.setup();
    await user.selectOptions(within(skill).getByRole("combobox", { name: en["cv.profile.skill_level"] }), "advanced");
    const name = within(skill).getByRole("textbox", { name: en["cv.profile.skill_name"] });
    await user.clear(name);
    await user.type(name, "Python 3");
    const language = group(en["cv.profile.language_n"].replace("{{n}}", "2"));
    await user.selectOptions(within(language).getByRole("combobox", { name: en["cv.profile.cefr"] }), "C2");
    expect(screen.getByText(en["cv.profile.unsaved"])).toBeTruthy();
    await user.click(screen.getByRole("button", { name: en["cv.profile.save"] }));
    await waitFor(() => {
      expect(requestsTo(api, "PUT /api/profile/drafts/7")).toHaveLength(1);
    });
    const base = profile();
    expect(sentDraft(api)).toEqual({
      ...base,
      skills: [{ ...base.skills[0], name: "Python 3", level: "advanced" }, base.skills[1]],
      languages: [base.languages[0], { ...base.languages[1], cefr: "C2" }],
    });
    // the answer's issues replace the draft's, inline at their field
    const evidence = within(group(en["cv.profile.skill_n"].replace("{{n}}", "2"))).getByRole("textbox", {
      name: en["cv.profile.evidence"],
    });
    await waitFor(() => {
      expect(describedBy(evidence)).toContain("Not in the CV text.");
    });
    expect(evidence.getAttribute("aria-invalid")).toBe("true");
  });

  it("shows issues inline for both path forms", async () => {
    const issues = [
      { path: "skills[0].evidence", code: "ungrounded" as const, message: "The quote is not in the CV text.", proposal: null },
      { path: "roles[7].title", code: "limit" as const, message: "At most 200 characters allowed.", proposal: null },
      { path: "target_titles[0].title", code: "contact_removed" as const, message: "Contact data (email) was found in this value and removed.", proposal: null },
    ];
    const api = draftRoute(() => readyDraft(issues));
    api.routes.set("PUT /api/profile/drafts/7", () =>
      problem(422, "validation_failed", {
        errors: [
          { field: "body.draft.languages.1.evidence", code: "invalid", message: "Not a string the profile can hold" },
          { field: "body.draft.roles[0].highlights[0]", code: "invalid", message: "Holds a NUL character" },
          { field: "body.draft.target_titles[1].title", code: "invalid", message: "Holds a NUL in the title" },
        ],
      }),
    );
    renderInProviders(<Harness />);
    const skill = await screen.findByRole("group", { name: en["cv.profile.skill_n"].replace("{{n}}", "1") });
    const evidence = within(skill).getByRole("textbox", { name: en["cv.profile.evidence"] });
    expect(describedBy(evidence)).toContain(en["issue.ungrounded"]);
    expect(describedBy(evidence)).toContain("The quote is not in the CV text.");
    // a target title's text: at its chip
    const chip = (title: string) =>
      screen.getByRole("button", { name: en["cv.profile.remove_title"].replace("{{title}}", title) }).closest("li") as HTMLElement;
    expect(describedBy(chip("Backend Engineer"))).toContain(en["issue.contact_removed"]);
    expect(describedBy(chip("Backend Engineer"))).toContain("Contact data (email) was found in this value and removed.");
    // a path no field shows: in the summary, with the count
    const summary = screen.getByRole("region", { name: "⚠ 3 validation issues" });
    expect(within(summary).queryByText(/Contact data \(email\)/)).toBeNull(); // shown at its chip, not twice
    expect(within(summary).getByText(/At most 200 characters allowed\./)).toBeTruthy();
    expect(within(summary).getByText("roles[7].title")).toBeTruthy();
    await userEvent.setup().click(screen.getByRole("button", { name: en["cv.profile.save"] }));
    // the 422 form (`body.draft.…`, dotted or bracketed indexes) lands at the same fields
    const language = group(en["cv.profile.language_n"].replace("{{n}}", "2"));
    const quote = within(language).getByRole("textbox", { name: en["cv.profile.evidence"] });
    await waitFor(() => {
      expect(describedBy(quote)).toContain("Not a string the profile can hold");
    });
    const role = group(en["cv.profile.role_n"].replace("{{n}}", "1"));
    expect((role.closest("details") as HTMLDetailsElement).open).toBe(true); // a role with an issue opens
    const highlight = within(role).getByRole("textbox", { name: en["cv.profile.highlight_n"].replace("{{n}}", "1") });
    expect(describedBy(highlight)).toContain("Holds a NUL character");
    expect(describedBy(chip("Softwareentwickler Backend"))).toContain("Holds a NUL in the title");
  });

  it("in German an issue of a known code shows its translation only; an unknown code keeps the server's text", async () => {
    await i18n.changeLanguage("de");
    const issues = [
      { path: "skills[0].evidence", code: "ungrounded" as const, message: "The quote is not in the CV text.", proposal: null },
      { path: "roles[7].title", code: "brand_new_rule" as unknown as "limit", message: "A rule the catalogue lacks.", proposal: null },
    ];
    draftRoute(() => readyDraft(issues));
    renderInProviders(<Harness />);
    const skill = await screen.findByRole("group", { name: de["cv.profile.skill_n"].replace("{{n}}", "1") });
    const evidence = within(skill).getByRole("textbox", { name: de["cv.profile.evidence"] });
    expect(describedBy(evidence)).toContain(de["issue.ungrounded"]);
    expect(describedBy(evidence)).not.toContain("The quote is not in the CV text.");
    const summary = screen.getByRole("region", { name: de["cv.profile.issues_other"].replace("{{count}}", "2") });
    expect(within(summary).getByText(/A rule the catalogue lacks\./)).toBeTruthy();
    expect(within(summary).getByText("roles[7].title", { selector: "code" })).toBeTruthy();
  });

  it("a confirm's 422 closes the modal and shows its errors at their fields", async () => {
    const api = draftRoute(() => readyDraft());
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(rescoreEstimate()));
    api.routes.set("POST /api/profile/drafts/7/confirm", () =>
      problem(422, "validation_failed", {
        errors: [
          { field: "skills[0].evidence", code: "ungrounded", message: "The quote is not in the CV text." },
          { field: "body.expected_rescore_ack", code: "ack_required", message: "Acknowledge the estimate." },
        ],
      }),
    );
    renderInProviders(<Harness />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: en["cv.profile.confirm"] }));
    const modal = await screen.findByRole("alertdialog", { name: en["cv.confirm.title"] });
    await within(modal).findByText(/^Creates profile v5/);
    await user.click(within(modal).getByRole("button", { name: en["cv.confirm.confirm"] }));
    await waitFor(() => {
      expect(screen.queryByRole("alertdialog")).toBeNull();
    });
    const skill = group(en["cv.profile.skill_n"].replace("{{n}}", "1"));
    const evidence = within(skill).getByRole("textbox", { name: en["cv.profile.evidence"] });
    expect(describedBy(evidence)).toContain("The quote is not in the CV text.");
    expect(evidence.getAttribute("aria-invalid")).toBe("true");
    // the acknowledgement has no field: in the summary, with its path
    expect(within(screen.getByRole("region", { name: "⚠ 2 validation issues" })).getByText("body.expected_rescore_ack")).toBeTruthy();
    expect(requestsTo(api, "POST /api/profile/drafts/7/confirm")[0].headers.get("Idempotency-Key")).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("a proposal is shown as text and applied", async () => {
    const issues = [{ path: "roles[1].start", code: "date_invalid" as const, message: "Not a date; removed.", proposal: "2018-01-01" }];
    draftRoute(() => readyDraft(issues, { draft: profile({ roles: [profile().roles[0], { ...profile().roles[1], start: null }] }) }));
    renderInProviders(<Harness />);
    const role = await screen.findByRole("group", { name: en["cv.profile.role_n"].replace("{{n}}", "2") });
    const start = within(role).getByLabelText<HTMLInputElement>(en["cv.profile.start"]);
    expect(start.value).toBe("");
    expect(within(role).getByText(en["cv.profile.suggested"].replace("{{value}}", "2018-01-01"))).toBeTruthy();
    fireEvent.click(within(role).getByRole("button", { name: en["cv.profile.apply"] }));
    expect(start.value).toBe("2018-01-01");
  });

  it("discard", async () => {
    const api = draftRoute(() => readyDraft());
    api.routes.set("POST /api/profile/drafts/7/discard", () => new Response(null, { status: 204 }));
    const app = renderInProviders(<Harness />);
    const invalidate = vi.spyOn(app.queryClient, "invalidateQueries");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: en["cv.profile.discard"] }));
    const dialog = screen.getByRole("alertdialog", { name: en["cv.discard.title"] });
    expect(requestsTo(api, "POST /api/profile/drafts/7/discard")).toHaveLength(0);
    await user.click(within(dialog).getByRole("button", { name: en["cv.discard.confirm"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/profile/drafts/7/discard")).toHaveLength(1);
    });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["profile"] });
  });

  it("schema_mismatch offers discard and paste only", async () => {
    const mismatch = [{ path: "", code: "schema_mismatch" as const, message: "The stored profile no longer matches.", proposal: null }];
    draftRoute(() => readyDraft(mismatch, { draft: null, repairs: ["paste_text"], recomputed_total_years: null }));
    const onPaste = vi.fn();
    renderInProviders(<Harness onPaste={onPaste} />);
    expect(await screen.findByText(en["issue.schema_mismatch"])).toBeTruthy();
    expect(screen.queryByRole("form")).toBeNull();
    const buttons = screen.getAllByRole("button").map((b) => b.textContent);
    expect(buttons).toEqual([en["cv.profile.discard"], en["draft_repair.paste_text"]]);
    fireEvent.click(screen.getByRole("button", { name: en["draft_repair.paste_text"] }));
    expect(onPaste).toHaveBeenCalled();
  });

  it("confirm with unsaved edits saves first", async () => {
    const api = draftRoute(() => readyDraft());
    api.routes.set("PUT /api/profile/drafts/7", () => json({ validation_issues: [] }));
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(rescoreEstimate()));
    renderInProviders(<Harness />);
    const user = userEvent.setup();
    const summary = await screen.findByRole("textbox", { name: en["cv.profile.summary"] });
    await user.type(summary, " Fintech.");
    await user.click(screen.getByRole("button", { name: en["cv.profile.confirm"] }));
    expect(await screen.findByRole("alertdialog", { name: en["cv.confirm.title"] })).toBeTruthy();
    expect(api.calls.filter((c) => c.startsWith("PUT ") || c.endsWith("/rescore-estimate"))).toEqual([
      "PUT /api/profile/drafts/7",
      "GET /api/profile/drafts/7/rescore-estimate",
    ]);
    expect((sentDraft(api) as { summary: string }).summary).toBe("Backend engineer building Python services. Fintech.");
  });

  it("an edit made while confirm's save is pending keeps the confirmation closed and survives", async () => {
    const api = draftRoute(() => readyDraft());
    const answers: (() => void)[] = [];
    api.routes.set("PUT /api/profile/drafts/7", () =>
      new Promise<Response>((resolve) => {
        answers.push(() => { resolve(json({ validation_issues: [] })); });
      }),
    );
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(rescoreEstimate()));
    renderInProviders(<Harness />);
    const user = userEvent.setup();
    const summary = await screen.findByRole<HTMLTextAreaElement>("textbox", { name: en["cv.profile.summary"] });
    await user.type(summary, " A");
    await user.click(screen.getByRole("button", { name: en["cv.profile.confirm"] }));
    await waitFor(() => {
      expect(answers).toHaveLength(1);
    });
    await user.type(summary, " B"); // while the save of " A" is pending
    answers[0]();
    await settled();
    // the saved draft is not what the form shows: no confirmation on it, and the edit is kept
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(requestsTo(api, "GET /api/profile/drafts/7/rescore-estimate")).toHaveLength(0);
    expect(summary.value).toBe("Backend engineer building Python services. A B");
    expect(screen.getByRole("alert").textContent).toBe(en["cv.profile.changed_while_saving"]);
    expect(screen.getByText(en["cv.profile.unsaved"])).toBeTruthy();
    // Confirm again saves the edit first, then opens the confirmation on it
    await user.click(screen.getByRole("button", { name: en["cv.profile.confirm"] }));
    await waitFor(() => {
      expect(answers).toHaveLength(2);
    });
    answers[1]();
    expect(await screen.findByRole("alertdialog", { name: en["cv.confirm.title"] })).toBeTruthy();
    expect((sentDraft(api, 0) as { summary: string }).summary).toBe("Backend engineer building Python services. A");
    expect((sentDraft(api, 1) as { summary: string }).summary).toBe("Backend engineer building Python services. A B");
    expect(screen.queryByText(en["cv.profile.changed_while_saving"])).toBeNull();
  });

  it("confirm without edits opens the modal at once; a save's 422 keeps it closed", async () => {
    const api = draftRoute(() => readyDraft());
    api.routes.set("GET /api/profile/drafts/7/rescore-estimate", () => json(rescoreEstimate()));
    api.routes.set("PUT /api/profile/drafts/7", () =>
      problem(422, "validation_failed", { errors: [{ field: "body.draft.summary", code: "invalid", message: "Holds a NUL character" }] }),
    );
    renderInProviders(<Harness />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: en["cv.profile.confirm"] }));
    expect(await screen.findByRole("alertdialog", { name: en["cv.confirm.title"] })).toBeTruthy();
    expect(requestsTo(api, "PUT /api/profile/drafts/7")).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: en["action.cancel"] }));
    await user.type(screen.getByRole("textbox", { name: en["cv.profile.summary"] }), "!");
    await user.click(screen.getByRole("button", { name: en["cv.profile.confirm"] }));
    await settled();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(describedBy(screen.getByRole("textbox", { name: en["cv.profile.summary"] }))).toContain("Holds a NUL character");
  });

  it("a role's seniority reads as the profile's, in both languages", () => {
    for (const [lang, table] of [["en", en], ["de", de]] as const) {
      const strings = table as Record<string, string>;
      for (const level of codes.seniority) {
        expect(strings[`role_seniority.${level}`], `${lang}: ${level}`).toBe(strings[`seniority.${level}`]);
      }
    }
  });

  it("each closed set lists exactly its catalogue namespace", () => {
    expect([...PROFILE_SENIORITIES]).toEqual(codes.order.seniority);
    expect([...ROLE_SENIORITIES].sort()).toEqual(codes.role_seniority);
    expect([...SKILL_CATEGORIES].sort()).toEqual(codes.skill_category);
    expect([...SKILL_LEVELS].sort()).toEqual(codes.skill_level);
    expect([...CEFR_LEVELS].sort()).toEqual(codes.cefr);
    expect([...LANGUAGE_CODES].sort()).toEqual(codes.language_code);
    expect([...WORK_AUTHORIZATIONS].sort()).toEqual(codes.work_authorization);
    expect(DEGREE_LEVELS.every((level) => codes.degree_level.includes(level))).toBe(true);
  });
});
