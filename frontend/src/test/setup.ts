// Vitest setup (jsdom): the app's i18n in English, Testing Library's cleanup after each test (vitest runs
// without globals, so it is not registered by itself), and what jsdom lacks: a no-op `scrollTo`, which the
// router calls after each navigation, a no-op `scrollIntoView`, which a combobox calls on its active option,
// and a `ResizeObserver` that never reports, which Radix Slider measures its thumbs with.
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { initI18n } from "../i18n/index";

initI18n("en");
window.scrollTo = () => undefined;
Element.prototype.scrollIntoView = () => undefined;
globalThis.ResizeObserver = class {
  observe(): void {
    // jsdom lays nothing out: no size to report
  }
  unobserve(): void {
    // nothing observed
  }
  disconnect(): void {
    // nothing observed
  }
};

afterEach(() => {
  cleanup();
});
