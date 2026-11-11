import { act, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import en from "../i18n/en.json";
import { fakeApi, manifestOut, requestsTo } from "../test/api";
import { renderInProviders } from "../test/app";
import { DeletionStatus } from "./DeletionStatus";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const LIST = "GET /api/privacy/manifests";

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
});
