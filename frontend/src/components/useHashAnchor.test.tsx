import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";
import { PIN_MS, useHashAnchor } from "./useHashAnchor";

/** A `ResizeObserver` the test reports through (`fire`), which reports nothing once disconnected. */
class FakeObserver {
  static made: FakeObserver[] = [];
  active = true;
  constructor(private readonly callback: ResizeObserverCallback) {
    FakeObserver.made.push(this);
  }
  observe(): void {
    // the test reports the size changes
  }
  unobserve(): void {
    // nothing to undo
  }
  disconnect(): void {
    this.active = false;
  }
  fire(): void {
    if (this.active) {
      this.callback([], this);
    }
  }
}

function Page({ hash }: { hash: string }) {
  useHashAnchor(hash);
  return (
    <div>
      <section id="above">above</section>
      <section id="target">target</section>
    </div>
  );
}

/** The ids of the elements `scrollIntoView` was called on, in order. */
function scrolledIds(spy: MockInstance<Element["scrollIntoView"]>): string[] {
  return spy.mock.contexts.map((element) => (element as Element).id);
}

function setScrollY(value: number): void {
  Object.defineProperty(window, "scrollY", { value, configurable: true });
}

let scrolled: MockInstance<Element["scrollIntoView"]>;

beforeEach(() => {
  FakeObserver.made = [];
  vi.stubGlobal("ResizeObserver", FakeObserver);
  scrolled = vi.spyOn(Element.prototype, "scrollIntoView");
  setScrollY(0);
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  setScrollY(0);
});

describe("useHashAnchor", () => {
  it("scrolls an unscrolled page to the element its hash names, and again as the page's size changes", () => {
    render(<Page hash="target" />);
    expect(scrolledIds(scrolled)).toEqual(["target"]);
    const [observer] = FakeObserver.made;
    observer.fire();
    observer.fire();
    expect(scrolledIds(scrolled)).toEqual(["target", "target", "target"]);
  });

  it.each(["pointerdown", "wheel", "touchstart", "keydown"])("lets go at the user's first %s", (type) => {
    render(<Page hash="target" />);
    window.dispatchEvent(new Event(type));
    FakeObserver.made[0].fire();
    expect(scrolledIds(scrolled)).toEqual(["target"]);
  });

  it(`lets go after ${String(PIN_MS)} ms`, () => {
    vi.useFakeTimers();
    render(<Page hash="target" />);
    const [observer] = FakeObserver.made;
    vi.advanceTimersByTime(PIN_MS - 1);
    observer.fire();
    expect(scrolledIds(scrolled)).toEqual(["target", "target"]);
    vi.advanceTimersByTime(1);
    observer.fire();
    expect(scrolledIds(scrolled)).toEqual(["target", "target"]);
  });

  it("lets go when the page unmounts", () => {
    const { unmount } = render(<Page hash="target" />);
    unmount();
    FakeObserver.made[0].fire();
    expect(scrolledIds(scrolled)).toEqual(["target"]);
  });

  it("holds an element the router scrolled to, at its scroll margin", () => {
    setScrollY(4_000);
    const target = (): HTMLElement | null => document.getElementById("target");
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      return DOMRect.fromRect({ x: 0, y: this === target() ? 0 : -300, width: 100, height: 50 });
    });
    render(<Page hash="target" />);
    FakeObserver.made[0].fire();
    expect(scrolledIds(scrolled)).toEqual(["target", "target"]);
  });

  it("leaves a scroll position the router restored", () => {
    setScrollY(4_000);
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue(
      DOMRect.fromRect({ x: 0, y: 600, width: 100, height: 50 }),
    );
    render(<Page hash="target" />);
    expect(scrolled).not.toHaveBeenCalled();
    expect(FakeObserver.made).toEqual([]);
  });

  it("does nothing without a hash, or for a hash that names no element", () => {
    const { rerender } = render(<Page hash="" />);
    rerender(<Page hash="nowhere" />);
    expect(scrolled).not.toHaveBeenCalled();
    expect(FakeObserver.made).toEqual([]);
  });
});
