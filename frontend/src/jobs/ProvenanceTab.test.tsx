import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { fakeApi, jobDetail, json, problem, requestsTo } from "../test/api";
import { NOW, renderInProviders } from "../test/app";
import { ProvenanceTab } from "./ProvenanceTab";

type MergeEventOut = components["schemas"]["MergeEventOut"];

const REL = "noopener noreferrer nofollow";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: NOW });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function merge(extra: Partial<MergeEventOut>): MergeEventOut {
  return { id: 9, kind: "strong_key_merge", from_job_id: 6, into_job_id: 5, created_at: "2026-11-02T10:00:00Z", reversible: true, ...extra };
}

/** A row's header and data cells, in order. */
function cellsOf(row: HTMLElement): HTMLElement[] {
  return Array.from(row.querySelectorAll<HTMLElement>("th, td"));
}

function section(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

describe("ProvenanceTab", () => {
  it("lists the postings with their own links, and each field's provenance", () => {
    fakeApi({ signedIn: true });
    renderInProviders(<ProvenanceTab detail={jobDetail()} />);
    const postings = within(section(en["provenance.postings"])).getByRole("table");
    const [, posting] = within(postings).getAllByRole("row");
    const cells = cellsOf(posting).map((cell) => cell.textContent);
    expect(cells.slice(0, 2)).toEqual(["Lever", "abc-123"]);
    expect(cells[4]).toBe(en["filters.availability.active"]);
    const link = within(posting).getByRole("link", { name: new RegExp(en["provenance.posting_link"]) });
    expect(link.getAttribute("href")).toBe("https://jobs.lever.co/acme/abc-123");
    expect(link.getAttribute("rel")).toBe(REL);
    expect(link.getAttribute("target")).toBe("_blank");

    const fields = within(section(en["provenance.fields"])).getByRole("table");
    const [, title] = within(fields).getAllByRole("row");
    const titleCells = cellsOf(title);
    expect(titleCells.slice(0, 3).map((cell) => cell.textContent)).toEqual(["title", "Lever", "source_rank"]);
    expect(titleCells[0].querySelector("code")).not.toBeNull(); // the field and the rule are the server's, verbatim
    expect(titleCells[2].querySelector("code")).not.toBeNull();
  });

  it("Unmerge is offered only for a reversible event into this job", () => {
    fakeApi({ signedIn: true });
    const detail = jobDetail({
      merge_events: [
        merge({ id: 1 }),
        merge({ id: 2, reversible: false }),
        merge({ id: 3, from_job_id: 5, into_job_id: 8 }),
        merge({ id: 4, kind: "unmerge", reversible: false }),
      ],
    });
    renderInProviders(<ProvenanceTab detail={detail} />);
    const events = within(section(en["provenance.merges"])).getAllByRole("row").slice(1);
    expect(events).toHaveLength(4);
    expect(events.map((row) => within(row).queryByRole("button", { name: en["provenance.unmerge"] }) !== null)).toEqual([
      true,
      false,
      false,
      false,
    ]);
    expect(events[0].textContent).toContain(en["merge_kind.strong_key_merge"]);
    expect(events[3].textContent).toContain(en["merge_kind.unmerge"]);
  });

  it("Unmerge confirms and posts {merge_event_id}", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/jobs/5/unmerge", () => json({ unmerge_event_id: 12 }, 202));
    const app = renderInProviders(<ProvenanceTab detail={jobDetail({ merge_events: [merge({ id: 9 })] })} />);
    const invalidate = vi.spyOn(app.queryClient, "invalidateQueries");
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["provenance.unmerge"] }));
    const dialog = screen.getByRole("alertdialog", { name: en["provenance.unmerge_title"].replace("{{job}}", "6") });
    expect(requestsTo(api, "POST /api/jobs/5/unmerge")).toHaveLength(0); // nothing before the confirmation
    await user.click(within(dialog).getByRole("button", { name: en["provenance.unmerge"] }));
    await waitFor(() => {
      expect(requestsTo(api, "POST /api/jobs/5/unmerge")).toHaveLength(1);
    });
    const [request] = requestsTo(api, "POST /api/jobs/5/unmerge");
    expect(JSON.parse(request.body ?? "null")).toEqual({ merge_event_id: 9 });
    expect(request.headers.has("Idempotency-Key")).toBe(false);
    expect(await screen.findByText(en["provenance.unmerged"].replace("{{job}}", "6"))).toBeTruthy();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["jobs"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["system", "status"] });
  });

  it("a refused Unmerge shows its problem inline", async () => {
    const api = fakeApi({ signedIn: true });
    api.routes.set("POST /api/jobs/5/unmerge", () => problem(409, "conflict_state"));
    renderInProviders(<ProvenanceTab detail={jobDetail({ merge_events: [merge({ id: 9 })] })} />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: en["provenance.unmerge"] }));
    await user.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: en["provenance.unmerge"] }));
    const alert = await within(section(en["provenance.merges"])).findByRole("alert");
    expect(alert.textContent).toContain(en["problem.conflict_state"]);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.queryByText(en["provenance.unmerged"].replace("{{job}}", "6"))).toBeNull();
  });

  it("without merge events says so", () => {
    fakeApi({ signedIn: true });
    renderInProviders(<ProvenanceTab detail={jobDetail()} />);
    expect(within(section(en["provenance.merges"])).getByText(en["provenance.no_merges"])).toBeTruthy();
  });
});
