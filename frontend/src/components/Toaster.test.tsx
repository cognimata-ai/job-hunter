import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { createToastStore, TOAST_MS, Toaster, useToast } from "./Toaster";

afterEach(() => {
  vi.useRealTimers();
});

function Raise() {
  const show = useToast();
  return <button type="button" onClick={() => { show({ message: "Saved" }); }}>Raise</button>;
}

describe("Toaster", () => {
  it("shows a notice once while it is shown, and lets it go after a while or on Dismiss", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const store = createToastStore();
    render(<Toaster store={store}><Raise /></Toaster>);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    act(() => {
      store.show({ message: "Connection problem", tone: "critical" });
      store.show({ message: "Connection problem", tone: "critical" });
    });
    await user.click(screen.getByRole("button", { name: "Raise" }));
    expect(screen.getAllByText("Connection problem")).toHaveLength(1);
    expect(screen.getByText("Saved")).toBeTruthy();

    const [, dismissSaved] = screen.getAllByRole("button", { name: "Dismiss" });
    expect(dismissSaved).toBeDefined();
    await user.click(dismissSaved);
    expect(screen.queryByText("Saved")).toBeNull();
    act(() => {
      vi.advanceTimersByTime(TOAST_MS);
    });
    expect(screen.queryByText("Connection problem")).toBeNull();
  });
});
