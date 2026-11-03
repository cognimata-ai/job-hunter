// Renders the whole app (its query client, router, toasts and live region) at a path, on a memory history,
// against the `fetch` stub of `test/api.ts`.
import { createMemoryHistory } from "@tanstack/react-router";
import { act, render } from "@testing-library/react";
import { App, type AppParts, createApp } from "../app/App";

/** 07:00 UTC on 3 Nov 2026: the moment `statusOut()` describes. */
export const NOW = new Date("2026-11-03T07:00:00Z");

export function renderApp(path: string): AppParts {
  const app = createApp({ history: createMemoryHistory({ initialEntries: [path] }) });
  render(<App app={app} />);
  return app;
}

/** Lets the query client's notification batch (a zero-delay timer) and the live region's delay reach the DOM. */
export async function settled(ms = 150): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}
