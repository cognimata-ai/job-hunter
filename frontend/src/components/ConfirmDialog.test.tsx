import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ConfirmDialog } from "./ConfirmDialog";

function setup(busy = false) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <>
      <button type="button">Elsewhere</button>
      <ConfirmDialog open title="Delete everything?" confirmLabel="Delete" busy={busy}
        onConfirm={onConfirm} onCancel={onCancel}>
        <p>This removes every job and profile.</p>
      </ConfirmDialog>
    </>,
  );
  return { onConfirm, onCancel, user: userEvent.setup() };
}

describe("ConfirmDialog", () => {
  it("confirm and cancel", async () => {
    const { onConfirm, onCancel, user } = setup();
    const dialog = screen.getByRole("alertdialog", { name: "Delete everything?" });
    expect(dialog.getAttribute("aria-modal")).toBe("true");
    expect(dialog.getAttribute("aria-describedby")).not.toBeNull();
    // the safe choice has the focus first
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Cancel" }));

    await user.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);

    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(2);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("does not confirm twice while busy", async () => {
    const { onConfirm, user } = setup(true);
    const confirm = screen.getByRole("button", { name: /Delete/ });
    expect(confirm.hasAttribute("disabled")).toBe(true);
    expect(confirm.getAttribute("aria-busy")).toBe("true");
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("renders nothing when closed", () => {
    render(<ConfirmDialog open={false} title="t" confirmLabel="Go" onConfirm={vi.fn()} onCancel={vi.fn()} />);
    expect(screen.queryByRole("alertdialog")).toBeNull();
  });
});
