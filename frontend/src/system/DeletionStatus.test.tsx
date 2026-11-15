import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { fakeApi, json, manifestOut, problem, requestsTo } from "../test/api";
import { renderInProviders } from "../test/app";
import { DeletionStatus } from "./DeletionStatus";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

type ManifestOut = components["schemas"]["ManifestOut"];

const LIST = "GET /api/privacy/manifests";
const LOAD_MORE = en["jobs.load_more"];

/** The newest 20 deletions, ids 30 down to 11, each a shipped golden-case deletion. */
function newest20(): ManifestOut[] {
  return Array.from({ length: 20 }, (_, i) => manifestOut({ id: 30 - i, kind: "golden_case_delete" }));
}

/** The cursor and limit of each list read, in order. */
function pagesRead(api: ReturnType<typeof fakeApi>): (string | null)[][] {
  return requestsTo(api, LIST).map((r) => [r.search.get("cursor"), r.search.get("limit")]);
}

/** The item of the deletion `id`, titled by its kind and id. */
async function itemOf(kind: string, id: number): Promise<HTMLElement> {
  const part = await screen.findByRole("region", { name: en["settings_privacy.deletions"] });
  const title = `${en[`settings_privacy.kind.${kind}` as keyof typeof en]} #${String(id)}`;
  const item = (await within(part).findByText(title)).closest("li");
  if (item === null) {
    throw new Error(`no item for ${title}`);
  }
  return item;
}

describe("DeletionStatus", () => {
  it("shows confirmed offsite only at shipped", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = fakeApi({ signedIn: true });
    api.manifests = [
      manifestOut({ id: 3, kind: "source_purge", status: "executing", completed_at: null, shipped_at: null }),
      manifestOut({ id: 2, kind: "golden_case_delete", status: "completed", shipped_at: null }),
      manifestOut({ id: 1, kind: "delete_everything", status: "shipped" }),
    ];
    renderInProviders(<DeletionStatus />);
    const executing = await itemOf("source_purge", 3);
    const completed = await itemOf("golden_case_delete", 2);
    const shipped = await itemOf("delete_everything", 1);
    expect(executing.textContent).toContain(en["settings_privacy.status.executing"]);
    expect(completed.textContent).toContain(en["settings_privacy.status.completed"]);
    expect(shipped.textContent).toContain(en["settings_privacy.status.shipped"]);
    // "Confirmed offsite" is the shipped one's alone
    for (const item of [executing, completed]) {
      expect(item.textContent).not.toContain(en["settings_privacy.status.shipped"]);
    }
    expect(screen.getAllByText(en["settings_privacy.status.shipped"])).toHaveLength(1);
    expect(requestsTo(api, LIST)[0]?.search.get("limit")).toBe("20");

    // read again every 10 s in the background while one is not shipped
    const reads = () => requestsTo(api, LIST);
    expect(reads()).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(9_000);
    });
    expect(reads()).toHaveLength(1);
    api.manifests = api.manifests.map((m) => manifestOut({ ...m, status: "shipped", completed_at: m.created_at, shipped_at: m.created_at }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1_000);
    });
    await waitFor(() => {
      expect(reads()).toHaveLength(2);
    });
    await waitFor(() => {
      expect(screen.getAllByText(en["settings_privacy.status.shipped"])).toHaveLength(3);
    });
    // all shipped: the poll stops
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(reads()).toHaveLength(2);
    for (const read of reads()) {
      expect(read.headers.get("X-JH-Background")).toBe("1");
    }
  });

  it("lists the deleted golden cases of a source purge", async () => {
    const api = fakeApi({ signedIn: true });
    api.manifests = [
      manifestOut({ id: 9, kind: "source_purge", status: "completed", deleted_golden_case_ids: [3, 7] }),
      manifestOut({ id: 8, kind: "source_purge", status: "shipped", deleted_golden_case_ids: [] }),
      manifestOut({ id: 7, kind: "source_purge", status: "executing", completed_at: null, shipped_at: null, deleted_golden_case_ids: [] }),
      manifestOut({ id: 6, kind: "golden_case_delete", status: "shipped", deleted_golden_case_ids: [] }),
      manifestOut({ id: 5, kind: "source_purge", status: "shipped", deleted_golden_case_ids: [12] }),
    ];
    renderInProviders(<DeletionStatus />);
    expect((await itemOf("source_purge", 9)).textContent).toContain("Deleted golden cases: #3 and #7");
    expect((await itemOf("source_purge", 5)).textContent).toContain("Deleted golden case: #12");
    // none deleted once it knows; nothing said while it is still finding them; another kind deletes none
    expect((await itemOf("source_purge", 8)).textContent).toContain(en["settings_privacy.no_deleted_cases"]);
    for (const [kind, id] of [["source_purge", 7], ["golden_case_delete", 6]] as const) {
      const item = await itemOf(kind, id);
      expect(item.textContent).not.toContain("Deleted golden case");
      expect(item.textContent).not.toContain(en["settings_privacy.no_deleted_cases"]);
    }
  });

  it("says when there is no deletion", async () => {
    fakeApi({ signedIn: true });
    renderInProviders(<DeletionStatus />);
    expect(await screen.findByText(en["settings_privacy.no_deletions"])).toBeTruthy();
  });

  it("pages beyond the newest 20 with Load more, and reads every loaded page again while one is not shipped", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = fakeApi({ signedIn: true });
    let older = [
      manifestOut({ id: 10, kind: "golden_case_delete", status: "completed", shipped_at: null }),
      manifestOut({ id: 9, kind: "source_purge", status: "shipped" }),
    ];
    api.routes.set(LIST, (request) =>
      json(request.search.get("cursor") === "c11" ? { items: older, next_cursor: null } : { items: newest20(), next_cursor: "c11" }),
    );
    renderInProviders(<DeletionStatus />);
    await itemOf("golden_case_delete", 11);
    const part = screen.getByRole("region", { name: en["settings_privacy.deletions"] });
    expect(within(part).queryByText(`${en["settings_privacy.kind.golden_case_delete"]} #10`)).toBeNull();
    // all of the first page shipped: no poll
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(pagesRead(api)).toEqual([[null, "20"]]);

    fireEvent.click(within(part).getByRole("button", { name: LOAD_MORE }));
    const pending = await itemOf("golden_case_delete", 10);
    expect(pending.textContent).toContain(en["settings_privacy.status.completed"]);
    expect((await itemOf("source_purge", 9)).textContent).toContain(en["settings_privacy.status.shipped"]);
    expect(pagesRead(api)).toEqual([[null, "20"], ["c11", "20"]]);
    expect(within(part).queryByRole("button", { name: LOAD_MORE })).toBeNull(); // the last page
    expect(within(part).getAllByRole("listitem")).toHaveLength(22);

    // an older deletion not yet shipped: every loaded page is read again every 10 s, in the background
    older = older.map((m) => manifestOut({ ...m, status: "shipped", shipped_at: "2026-11-03T05:15:00Z" }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    await waitFor(() => {
      expect(pagesRead(api)).toEqual([[null, "20"], ["c11", "20"], [null, "20"], ["c11", "20"]]);
    });
    await waitFor(() => {
      expect((pending.closest("ul") ?? part).textContent).not.toContain(en["settings_privacy.status.completed"]);
    });
    expect(within(part).getAllByRole("listitem")).toHaveLength(22);
    // all shipped: the poll stops
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });
    expect(requestsTo(api, LIST)).toHaveLength(4);
    for (const read of requestsTo(api, LIST)) {
      expect(read.headers.get("X-JH-Background")).toBe("1");
    }
  });

  it("a failed Load more keeps the loaded deletions and says why by its code", async () => {
    const api = fakeApi({ signedIn: true });
    const answers = [problem(429, "throttled", { detail: "slow down, says the server" })];
    api.routes.set(LIST, (request) =>
      request.search.get("cursor") === null
        ? json({ items: newest20(), next_cursor: "c11" })
        : (answers.shift() ?? json({ items: [manifestOut({ id: 10, kind: "golden_case_delete" })], next_cursor: null })),
    );
    renderInProviders(<DeletionStatus />);
    await itemOf("golden_case_delete", 11);
    const part = screen.getByRole("region", { name: en["settings_privacy.deletions"] });
    fireEvent.click(within(part).getByRole("button", { name: LOAD_MORE }));
    const alert = await within(part).findByRole("alert");
    expect(alert.textContent).toContain(en["problem.throttled"]);
    expect(alert.textContent).not.toContain("says the server");
    expect(within(part).getAllByRole("listitem")).toHaveLength(20);
    // Load more again reads the page again
    fireEvent.click(within(part).getByRole("button", { name: LOAD_MORE }));
    await itemOf("golden_case_delete", 10);
    expect(within(part).queryByRole("alert")).toBeNull();
    expect(within(part).queryByRole("button", { name: LOAD_MORE })).toBeNull();
  });
});
