// Where the focus goes when the control that had it leaves its list (plan M5 Task 33, the visual pass; the
// pattern of M4's watchlist, whose heading takes the focus once a removed company's row has gone).
//
// An action that removes its own control drops the focus on the document's body, and a keyboard user starts
// again from the page's top: an alert acknowledged or resolved leaves the Open tab, a requested deletion replaces
// its golden case's Delete, a retried purge leaves Needs attention on the next read. A confirmation dialog gives
// the focus back to the control that opened it, which may be gone by then too.
//
// So the hook remembers the element last focused inside `root`, also while the focus is in a dialog opened from
// it (`aria-modal`, which portals out of `root`). When `root` changes and that element has left the document
// with the focus on the body, the focus goes to `fallback` (the list's heading, with `tabIndex={-1}`). A focus
// the user moved anywhere else, or dropped while its control stays, is left alone.
import { type RefObject, useEffect } from "react";

const MODAL = "[aria-modal='true']";

/** Moves the focus to `fallback` when the element focused inside `root` leaves the document. */
export function useFocusFallback(root: RefObject<HTMLElement | null>, fallback: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const container = root.current;
    if (container === null) {
      return undefined;
    }
    let last: Element | null = null;
    const onFocusIn = (event: FocusEvent): void => {
      last = event.target instanceof Element ? event.target : null;
    };
    const onFocusOut = (event: FocusEvent): void => {
      const next = event.relatedTarget;
      if (next instanceof Element && !container.contains(next) && next.closest(MODAL) === null) {
        last = null; // the user moved on
      }
    };
    const observer = new MutationObserver(() => {
      const active = document.activeElement;
      if (last !== null && !last.isConnected && (active === null || active === document.body)) {
        last = null;
        fallback.current?.focus();
      }
    });
    container.addEventListener("focusin", onFocusIn);
    container.addEventListener("focusout", onFocusOut);
    observer.observe(container, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      container.removeEventListener("focusin", onFocusIn);
      container.removeEventListener("focusout", onFocusOut);
    };
  }, [root, fallback]);
}
