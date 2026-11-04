import { act, fireEvent, render, screen } from "@testing-library/react";
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

  it("an action notice has no timer: it stays until its action or Dismiss is used", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const store = createToastStore();
    render(<Toaster store={store}><span /></Toaster>);
    act(() => {
      store.show({ message: "Hidden: Backend Engineer", action: { label: "Undo", onAction: vi.fn() } });
    });
    act(() => {
      vi.advanceTimersByTime(3 * TOAST_MS);
    });
    expect(screen.getByText("Hidden: Backend Engineer")).toBeTruthy();
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Hidden: Backend Engineer")).toBeNull();
  });

  it("a fourth action notice drops the oldest one, and no plain one", () => {
    const store = createToastStore();
    render(<Toaster store={store}><span /></Toaster>);
    act(() => {
      store.show({ message: "Connection problem" });
      for (const n of [1, 2, 3, 4]) {
        store.show({ message: `Hidden: job ${String(n)}`, action: { label: "Undo", onAction: vi.fn() } });
      }
    });
    expect(screen.getAllByText(/^Hidden: /).map((p) => p.textContent)).toEqual([
      "Hidden: job 2",
      "Hidden: job 3",
      "Hidden: job 4",
    ]);
    expect(screen.getByText("Connection problem")).toBeTruthy();
  });

  it("a plain notice waits while focus or the pointer is inside it, then leaves TOAST_MS after", () => {
    vi.useFakeTimers();
    const store = createToastStore();
    render(<Toaster store={store}><span /></Toaster>);
    act(() => {
      store.show({ message: "Saved" });
    });
    const dismiss = screen.getByRole("button", { name: "Dismiss" });
    const toast = screen.getByText("Saved").parentElement as HTMLElement;
    act(() => {
      vi.advanceTimersByTime(TOAST_MS / 2);
      dismiss.focus();
      vi.advanceTimersByTime(2 * TOAST_MS);
    });
    expect(screen.getByText("Saved")).toBeTruthy(); // focus inside: no timer
    act(() => {
      fireEvent.mouseEnter(toast);
      dismiss.blur();
      vi.advanceTimersByTime(2 * TOAST_MS);
    });
    expect(screen.getByText("Saved")).toBeTruthy(); // the pointer is still inside
    act(() => {
      fireEvent.mouseLeave(toast);
      vi.advanceTimersByTime(TOAST_MS - 1);
    });
    expect(screen.getByText("Saved")).toBeTruthy(); // both left: the full TOAST_MS again
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.queryByText("Saved")).toBeNull();
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
