// The app's parts and their providers: the query client (its session-expiry and transient-failure paths), the
// router, the toasts and the live region. `main.tsx` mounts it; tests mount it on a memory history.
import { type QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, type RouterHistory } from "@tanstack/react-router";
import { makeQueryClient } from "../api/queryClient";
import { LiveRegion } from "../components/LiveRegion";
import { createToastStore, Toaster, type ToastStore } from "../components/Toaster";
import i18n from "../i18n/index";
import { type AppRouter, createAppRouter } from "../router";
import { expireSession } from "./RequireSession";

export interface AppParts {
  router: AppRouter;
  queryClient: QueryClient;
  toasts: ToastStore;
}

export function createApp({ history }: { history?: RouterHistory } = {}): AppParts {
  const toasts = createToastStore();
  const parts: { router?: AppRouter } = {};
  const queryClient = makeQueryClient({
    onUnauthenticated: () => {
      if (parts.router !== undefined) {
        expireSession(parts.router, queryClient);
      }
    },
    onTransient: () => {
      toasts.show({ message: i18n.t("toast.transient"), tone: "critical" });
    },
  });
  const router = createAppRouter({ queryClient, history });
  parts.router = router;
  return { router, queryClient, toasts };
}

export function App({ app }: { app: AppParts }) {
  return (
    <QueryClientProvider client={app.queryClient}>
      <Toaster store={app.toasts}>
        <LiveRegion>
          <RouterProvider router={app.router} />
        </LiveRegion>
      </Toaster>
    </QueryClientProvider>
  );
}
