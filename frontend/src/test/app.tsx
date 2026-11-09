// Renders the whole app (its query client, router, toasts and live region) at a path, on a memory history,
// against the `fetch` stub of `test/api.ts`; or one component inside the app's providers, without the router.
import { QueryClientProvider } from "@tanstack/react-query";
import { createMemoryHistory } from "@tanstack/react-router";
import { act, render } from "@testing-library/react";
import type { ReactNode } from "react";
import { App, type AppParts, createApp } from "../app/App";
import { LiveRegion } from "../components/LiveRegion";
import { Toaster } from "../components/Toaster";

/** 07:00 UTC on 3 Nov 2026: the moment `statusOut()` describes. */
export const NOW = new Date("2026-11-03T07:00:00Z");

/** Renders the app at `path`; `retryDelay` replaces the queries' backoff (a read that fails past its retries). */
export function renderApp(path: string, { retryDelay }: { retryDelay?: number } = {}): AppParts {
  const app = createApp({ history: createMemoryHistory({ initialEntries: [path] }) });
  if (retryDelay !== undefined) {
    const queries = app.queryClient.getDefaultOptions().queries;
    app.queryClient.setDefaultOptions({ queries: { ...queries, retryDelay } });
  }
  render(<App app={app} />);
  return app;
}

/** Renders `ui` inside the app's query client, toasts and live region (no router, no shell: the display time
 * zone is the default one). */
export function renderInProviders(ui: ReactNode): AppParts {
  const app = createApp({ history: createMemoryHistory({ initialEntries: ["/"] }) });
  render(
    <QueryClientProvider client={app.queryClient}>
      <Toaster store={app.toasts}>
        <LiveRegion>{ui}</LiveRegion>
      </Toaster>
    </QueryClientProvider>,
  );
  return app;
}

/** Lets the query client's notification batch (a zero-delay timer) and the live region's delay reach the DOM. */
export async function settled(ms = 150): Promise<void> {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}
