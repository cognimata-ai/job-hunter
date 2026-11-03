// Non-blocking notices (spec §15.8: "Offline / 5xx: a non-blocking toast"). A store outside React, so the
// query client can raise one (`onTransient`); `<Toaster>` shows its notices and gives `useToast()` to the
// components inside it. A notice already shown is not shown twice; each one leaves after `TOAST_MS`.
import { createContext, type ReactNode, useContext, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "./cx";
import styles from "./Toaster.module.css";

export const TOAST_MS = 8000;

export type ToastTone = "info" | "critical";

export interface Toast {
  id: number;
  /** The notice, translated by the caller. */
  message: string;
  tone: ToastTone;
}

export interface ToastStore {
  show: (toast: { message: string; tone?: ToastTone }) => void;
  dismiss: (id: number) => void;
  subscribe: (listener: () => void) => () => void;
  getSnapshot: () => readonly Toast[];
}

export function createToastStore(): ToastStore {
  let toasts: readonly Toast[] = [];
  let nextId = 1;
  const listeners = new Set<() => void>();
  const timers = new Map<number, ReturnType<typeof setTimeout>>();
  const emit = () => {
    for (const listener of listeners) {
      listener();
    }
  };
  const store: ToastStore = {
    show: ({ message, tone = "info" }) => {
      if (toasts.some((toast) => toast.message === message)) {
        return;
      }
      const id = nextId++;
      toasts = [...toasts, { id, message, tone }];
      timers.set(id, setTimeout(() => { store.dismiss(id); }, TOAST_MS));
      emit();
    },
    dismiss: (id) => {
      clearTimeout(timers.get(id));
      timers.delete(id);
      toasts = toasts.filter((toast) => toast.id !== id);
      emit();
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    getSnapshot: () => toasts,
  };
  return store;
}

const ToastContext = createContext<ToastStore | null>(null);

/** Shows a notice (translated by the caller). Inside `<Toaster>` only. */
export function useToast(): ToastStore["show"] {
  const store = useContext(ToastContext);
  if (store === null) {
    throw new Error("useToast is used outside <Toaster>");
  }
  return store.show;
}

export function Toaster({ store, children }: { store: ToastStore; children: ReactNode }) {
  const { t } = useTranslation();
  const toasts = useSyncExternalStore(store.subscribe, store.getSnapshot);
  return (
    <ToastContext.Provider value={store}>
      {children}
      <section className={styles.region} aria-label={t("toast.region")} aria-live="polite">
        {toasts.map((toast) => (
          <div key={toast.id} className={cx(styles.toast, toast.tone === "critical" && styles.critical)}>
            <p className={styles.message}>{toast.message}</p>
            <button type="button" className={styles.dismiss} aria-label={t("action.dismiss")}
              onClick={() => { store.dismiss(toast.id); }}>
              <span aria-hidden="true">×</span>
            </button>
          </div>
        ))}
      </section>
    </ToastContext.Provider>
  );
}
