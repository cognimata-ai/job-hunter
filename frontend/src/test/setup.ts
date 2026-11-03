// Vitest setup (jsdom): the app's i18n in English, Testing Library's cleanup after each test (vitest runs
// without globals, so it is not registered by itself), and a no-op `scrollTo`, which jsdom lacks and the
// router calls after each navigation.
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { initI18n } from "../i18n/index";

initI18n("en");
window.scrollTo = () => undefined;

afterEach(() => {
  cleanup();
});
