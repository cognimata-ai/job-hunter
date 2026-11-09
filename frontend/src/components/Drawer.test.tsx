import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { ConfirmDialog } from "./ConfirmDialog";
import { Drawer } from "./Drawer";
import { createToastStore, Toaster } from "./Toaster";

function Page() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button">Before</button>
      <button type="button" onClick={() => { setOpen(true); }}>Open ad</button>
      <Drawer open={open} onClose={() => { setOpen(false); }} titleId="drawer-title">
        <h2 id="drawer-title">Senior Backend Engineer</h2>
        <button type="button">Save</button>
        <a href="#apply">Apply link</a>
      </Drawer>
    </>
  );
}

describe("Drawer", () => {
  it("traps focus, closes on Esc and returns focus to the opener", async () => {
    const user = userEvent.setup();
    render(<Page />);
    const opener = screen.getByRole("button", { name: "Open ad" });
    await user.click(opener);

    const dialog = screen.getByRole("dialog", { name: "Senior Backend Engineer" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(document.body.classList.contains("scroll-locked")).toBe(true);

    for (let i = 0; i < 5; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement), `after tab ${String(i + 1)}`).toBe(true);
    }
    await user.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(document.activeElement).toBe(opener);
    expect(document.body.classList.contains("scroll-locked")).toBe(false);
  });

  it("closes on a click outside", async () => {
    const user = userEvent.setup();
    render(<Page />);
    await user.click(screen.getByRole("button", { name: "Open ad" }));
    await user.click(screen.getByRole("button", { name: "Before", hidden: true }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("has a close button with a label", async () => {
    const user = userEvent.setup();
    render(<Page />);
    await user.click(screen.getByRole("button", { name: "Open ad" }));
    await user.click(screen.getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

// A confirmation opened from the drawer is rendered inside it, as its React descendant (ModalLayer's rule).
function DrawerWithConfirmation() {
  const [drawer, setDrawer] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [hidden, setHidden] = useState(0);
  return (
    <>
      <button type="button" onClick={() => { setDrawer(true); }}>Open ad</button>
      <p>hidden {hidden}</p>
      <Drawer open={drawer} onClose={() => { setDrawer(false); }} titleId="ad-title">
        <h2 id="ad-title">Senior Backend Engineer</h2>
        <button type="button" onClick={() => { setConfirm(true); }}>Hide</button>
        <ConfirmDialog open={confirm} title="Hide this ad?" confirmLabel="Hide ad"
          onConfirm={() => { setHidden((n) => n + 1); setConfirm(false); }}
          onCancel={() => { setConfirm(false); }} />
      </Drawer>
    </>
  );
}

describe("Drawer with a ConfirmDialog inside", () => {
  it("keeps the drawer open on Confirm, closes only the dialog on Esc, and unlocks the page after both", async () => {
    const user = userEvent.setup();
    render(<DrawerWithConfirmation />);
    const locked = () => document.body.classList.contains("scroll-locked");
    await user.click(screen.getByRole("button", { name: "Open ad" }));
    await user.click(screen.getByRole("button", { name: "Hide" }));
    expect(screen.getByRole("alertdialog", { name: "Hide this ad?" })).toBeTruthy();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getByRole("dialog", { name: "Senior Backend Engineer" })).toBeTruthy();
    expect(locked()).toBe(true);

    await user.click(screen.getByRole("button", { name: "Hide" }));
    await user.click(screen.getByRole("button", { name: "Hide ad" }));
    expect(screen.getByText("hidden 1")).toBeTruthy();
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(screen.getByRole("dialog", { name: "Senior Backend Engineer" })).toBeTruthy();
    expect(locked()).toBe(true);

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(locked()).toBe(false);
  });

  it("stays open when a toast is dismissed", async () => {
    const user = userEvent.setup();
    const store = createToastStore();
    render(<Toaster store={store}><Page /></Toaster>);
    await user.click(screen.getByRole("button", { name: "Open ad" }));
    act(() => {
      store.show({ message: "Connection problem" });
    });
    await user.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("Connection problem")).toBeNull();
    expect(screen.getByRole("dialog", { name: "Senior Backend Engineer" })).toBeTruthy();
  });
});
