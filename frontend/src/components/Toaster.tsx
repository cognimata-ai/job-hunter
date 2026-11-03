// Non-blocking notices (spec §15.8: "Offline / 5xx: a non-blocking toast"). A store outside React, so the
// query client can raise one (`onTransient`); `<Toaster>` shows its notices and gives `useToast()` to the
// components inside it. A notice already shown is not shown twice; each one leaves after `TOAST_MS`. A notice
// may carry one action (a hidden job's Undo): its button runs the action and dismisses the notice, and such a
// notice is never merged with an equal one, since each acts on its own subject. The region is a branch of
// every open layer (Radix `DismissableLayerBranch`): dismissing a notice is not a click outside an open drawer
// or dialog, so it does not close them.
import { DismissableLayerBranch } from "@radix-ui/react-dismissable-layer";
import { createContext, type ReactNode, useContext, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "./cx";
import styles from "./Toaster.module.css";

export const TOAST_MS = 8000;

export type ToastTone = "info" | "critical";

/** A notice's one action: its button's label (translated by the caller) and what it does. */
export interface ToastAction {
  label: string;
  onAction: () => void;
}

export interface Toast {
  id: number;
  /** The notice, translated by the caller. */
  message: string;
  tone: ToastTone;
  action?: ToastAction;
}

export interface ToastStore {
  show: (toast: { message: string; tone?: ToastTone; action?: ToastAction }) => void;
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
    show: ({ message, tone = "info", action }) => {
      if (action === undefined && toasts.some((toast) => toast.action === undefined && toast.message === message)) {
        return;
      }
      const id = nextId++;
      toasts = [...toasts, action === undefined ? { id, message, tone } : { id, message, tone, action }];
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
      <DismissableLayerBranch asChild>
        <section className={styles.region} aria-label={t("toast.region")} aria-live="polite">
          {toasts.map((toast) => (
            <div key={toast.id} className={cx(styles.toast, toast.tone === "critical" && styles.critical)}>
              <p className={styles.message}>{toast.message}</p>
              {toast.action !== undefined && (
                <button type="button" className={styles.action}
                  onClick={() => {
                    store.dismiss(toast.id);
                    toast.action?.onAction();
                  }}>
                  {toast.action.label}
                </button>
              )}
              <button type="button" className={styles.dismiss} aria-label={t("action.dismiss")}
                onClick={() => { store.dismiss(toast.id); }}>
                <span aria-hidden="true">×</span>
              </button>
            </div>
          ))}
        </section>
      </DismissableLayerBranch>
    </ToastContext.Provider>
  );
}
