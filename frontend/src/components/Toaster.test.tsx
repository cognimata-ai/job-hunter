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

  it("a notice with an action runs it once and leaves, and is never merged with an equal one", async () => {
    const store = createToastStore();
    render(<Toaster store={store}><span /></Toaster>);
    const first = vi.fn();
    const second = vi.fn();
    act(() => {
      store.show({ message: "Hidden", action: { label: "Undo", onAction: first } });
      store.show({ message: "Hidden", action: { label: "Undo", onAction: second } }); // another job's
    });
    const undos = screen.getAllByRole("button", { name: "Undo" });
    expect(undos).toHaveLength(2);
    await userEvent.setup().click(undos[1]);
    expect(second).toHaveBeenCalledTimes(1);
    expect(first).not.toHaveBeenCalled();
    expect(screen.getAllByRole("button", { name: "Undo" })).toHaveLength(1);
    expect(screen.getAllByText("Hidden")).toHaveLength(1);
  });
});
