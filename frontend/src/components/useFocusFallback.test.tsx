import { act, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it } from "vitest";
import { useFocusFallback } from "./useFocusFallback";

/** A list under a heading, the hook watching it; `dialog` renders a modal outside it, as a portal does. */
function List({ items, dialog = false, elsewhere = true }: { items: string[]; dialog?: boolean; elsewhere?: boolean }) {
  const root = useRef<HTMLElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  useFocusFallback(root, heading);
  return (
    <div>
      <section ref={root} aria-labelledby="list-heading">
        <h3 ref={heading} id="list-heading" tabIndex={-1}>List</h3>
        <ul>
          {items.map((item) => (
            <li key={item}><button type="button">{item}</button></li>
          ))}
        </ul>
      </section>
      {elsewhere && <button type="button">Elsewhere</button>}
      {dialog && (
        <div role="alertdialog" aria-modal="true" aria-label="Sure?">
          <button type="button">Confirm</button>
        </div>
      )}
    </div>
  );
}

function focus(name: string): void {
  act(() => {
    screen.getByRole("button", { name }).focus();
  });
}

describe("useFocusFallback", () => {
  it("hands the focus to the fallback when the control that had it leaves", async () => {
    const { rerender } = render(<List items={["One", "Two"]} />);
    focus("One");
    rerender(<List items={["Two"]} />);
    await act(async () => {
      await Promise.resolve(); // the mutation observer's delivery
    });
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "List" }));
  });

  it("hands it over too when a dialog opened from inside closes after its opener left", async () => {
    const { rerender } = render(<List items={["One", "Two"]} />);
    focus("One");
    rerender(<List items={["One", "Two"]} dialog />);
    focus("Confirm");
    rerender(<List items={["Two"]} />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "List" }));
  });

  it("leaves the focus where the user moved it", async () => {
    const { rerender } = render(<List items={["One", "Two"]} />);
    focus("One");
    focus("Elsewhere");
    rerender(<List items={["Two"]} />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.activeElement).toBe(screen.getByRole("button", { name: "Elsewhere" }));
    // nor takes it back when what the user moved to leaves later, and the list changes again
    rerender(<List items={["Two"]} elsewhere={false} />);
    rerender(<List items={[]} elsewhere={false} />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.activeElement).toBe(document.body);
  });

  it("leaves a focus the user dropped while its control stays", async () => {
    const { rerender } = render(<List items={["One", "Two"]} />);
    focus("One");
    act(() => {
      (document.activeElement as HTMLElement).blur();
    });
    rerender(<List items={["One", "Two", "Three"]} />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.activeElement).toBe(document.body);
  });

  it("forgets a control the user clicked away from, so its later removal moves no focus", async () => {
    // plan M5 Task 33, Ruling T33-15: a click on content that takes no focus blurs it with no next element
    const { rerender } = render(<List items={["One", "Two"]} />);
    focus("One");
    act(() => {
      (document.activeElement as HTMLElement).blur();
    });
    await act(async () => {
      await Promise.resolve();
    });
    rerender(<List items={["Two"]} />); // a background read removes it later
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.activeElement).toBe(document.body);
  });

  it("keeps following a control while the window is away, its focus kept", async () => {
    const { rerender } = render(<List items={["One", "Two"]} />);
    focus("One");
    act(() => {
      // the window loses the focus: no next element, and the control stays the document's active element
      screen.getByRole("button", { name: "One" }).dispatchEvent(new FocusEvent("focusout", { bubbles: true, relatedTarget: null }));
    });
    await act(async () => {
      await Promise.resolve();
    });
    rerender(<List items={["Two"]} />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(document.activeElement).toBe(screen.getByRole("heading", { name: "List" }));
  });
});
