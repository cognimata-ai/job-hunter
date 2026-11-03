import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";
import { Drawer } from "./Drawer";

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
