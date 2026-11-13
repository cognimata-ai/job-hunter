// The part of a page that the location's hash names, brought into view and held there while the page fills in
// (spec §15.7: Profile & Settings' sections and the privacy statement by hash; plan M5 Task 33, the visual pass).
//
// The router scrolls to a hash once, when the route renders (`scrollRestoration`). A fresh load (a reload, a
// bookmark, a link from outside the app) renders the session read's spinner then (`RequireSession`), so the part
// is not there yet and the page stays at its top. And a page reached by a link before its data was read grows
// above the part as each section's data arrives, which carries the part out of view.
//
// So when the page mounts, or its hash changes, and the hash names an element, the hook acts unless the router
// restored a scroll position: on a page still at its top (the router found nothing to scroll to) or with the
// element at its scroll margin (the router just scrolled it there), it scrolls the element into view, then again
// whenever the document's size changes, until the user's first pointer, wheel, touch or key input, any scroll it
// did not cause (the scrollbar, assistive technology: the page is no longer where the last pin put it, its height
// unchanged; plan M5 Task 33, Ruling T33-15), or `PIN_MS`, whichever comes first. A scroll that comes with a
// change of the document's height is the browser keeping what was in view as the page grows (scroll anchoring),
// which can arrive before the size change is reported: it pins again. A position the router restored (back and
// forward) is left as it is.
import { useEffect } from "react";

/** How long the hook holds the element at most. */
export const PIN_MS = 10_000;
/** How far, in px, the page may be from where the last pin put it before a scroll counts as the user's. */
const MOVED_PX = 2;
const USER_INPUT = ["pointerdown", "wheel", "touchstart", "keydown"] as const;

/** Whether the element sits at its scroll margin from the viewport's top, where scrolling it into view puts it. */
function atScrollMargin(element: HTMLElement): boolean {
  const margin = Number.parseFloat(getComputedStyle(element).scrollMarginTop);
  return Math.abs(element.getBoundingClientRect().top - (Number.isNaN(margin) ? 0 : margin)) <= 1;
}

/** Brings the element `hash` names (the router's `location.hash`, without `#`) into view and holds it there. */
export function useHashAnchor(hash: string): void {
  useEffect(() => {
    const target = hash === "" ? null : document.getElementById(hash);
    if (target === null || !(window.scrollY === 0 || atScrollMargin(target))) {
      return undefined;
    }
    const page = document.documentElement;
    let pinnedAt = window.scrollY;
    let pinnedHeight = page.scrollHeight;
    const pin = (): void => {
      target.scrollIntoView();
      pinnedAt = window.scrollY;
      pinnedHeight = page.scrollHeight;
    };
    const observer = new ResizeObserver(pin);
    const onScroll = (): void => {
      if (page.scrollHeight !== pinnedHeight) {
        pin(); // the page grew or shrank: the browser's anchoring, not the user
      } else if (Math.abs(window.scrollY - pinnedAt) > MOVED_PX) {
        stop(); // the page moved, and not by a pin
      }
    };
    const stop = (): void => {
      observer.disconnect();
      window.clearTimeout(timer);
      window.removeEventListener("scroll", onScroll);
      for (const type of USER_INPUT) {
        window.removeEventListener(type, stop, true);
      }
    };
    const timer = window.setTimeout(stop, PIN_MS);
    for (const type of USER_INPUT) {
      window.addEventListener(type, stop, { capture: true, passive: true });
    }
    window.addEventListener("scroll", onScroll, { passive: true });
    pin();
    observer.observe(document.body);
    return stop;
  }, [hash]);
}
