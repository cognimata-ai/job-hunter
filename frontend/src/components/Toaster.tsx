// Non-blocking notices (spec §15.8: "Offline / 5xx: a non-blocking toast"). A store outside React, so the
// query client can raise one (`onTransient`); `<Toaster>` shows its notices and gives `useToast()` to the
// components inside it.
//
// - A plain notice is not shown twice while it is shown, and leaves `TOAST_MS` after it appears. Its timer
//   pauses while focus or the pointer is inside it, and starts again from the full `TOAST_MS` once both
//   have left, so a notice being read or reached is never pulled away.
// - A notice with an action (a hidden job's Undo) has no timer at all (WCAG 2.2.1): it stays until its action
//   or its Dismiss button is used, however long the keyboard path to it takes, and focus is never moved to it.
//   Its button dismisses it, then runs the action. It is never merged with an equal one, since each acts on
//   its own subject; at most `MAX_ACTION_TOASTS` are shown, and a further one drops the oldest of them.
//
// The region is a branch of every open layer (Radix `DismissableLayerBranch`): dismissing a notice is not a
// click outside an open drawer or dialog, so it does not close them.
import { DismissableLayerBranch } from "@radix-ui/react-dismissable-layer";
import { createContext, type FocusEvent, type ReactNode, useContext, useRef, useSyncExternalStore } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "./cx";
import styles from "./Toaster.module.css";

export const TOAST_MS = 8000;
/** The most notices with an action shown at once; a further one drops the oldest of them. */
export const MAX_ACTION_TOASTS = 3;

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
  /** Stops a plain notice's timer (focus or the pointer is inside it). */
  pause: (id: number) => void;
  /** Starts a paused plain notice's timer again, from the full `TOAST_MS`. */
  resume: (id: number) => void;
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
  const arm = (id: number) => {
    timers.set(id, setTimeout(() => { store.dismiss(id); }, TOAST_MS));
  };
  const store: ToastStore = {
    show: ({ message, tone = "info", action }) => {
      const id = nextId++;
      if (action === undefined) {
        if (toasts.some((toast) => toast.action === undefined && toast.message === message)) {
          return;
        }
        toasts = [...toasts, { id, message, tone }];
        arm(id);
      } else {
        const withAction = toasts.filter((toast) => toast.action !== undefined);
        const dropped = new Set(withAction.slice(0, withAction.length - MAX_ACTION_TOASTS + 1).map((x) => x.id));
        toasts = [...toasts.filter((toast) => !dropped.has(toast.id)), { id, message, tone, action }];
      }
      emit();
    },
    dismiss: (id) => {
      clearTimeout(timers.get(id));
      timers.delete(id);
      toasts = toasts.filter((toast) => toast.id !== id);
      emit();
    },
    pause: (id) => {
      clearTimeout(timers.get(id));
      timers.delete(id);
    },
    resume: (id) => {
      const toast = toasts.find((x) => x.id === id);
      if (toast !== undefined && toast.action === undefined && !timers.has(id)) {
        arm(id);
      }
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

/** One notice: it holds its timer while focus or the pointer is inside it. */
function ToastItem({ toast, store }: { toast: Toast; store: ToastStore }) {
  const { t } = useTranslation();
  const inside = useRef({ focus: false, pointer: false });
  const hold = (part: "focus" | "pointer", value: boolean) => {
    inside.current[part] = value;
    if (inside.current.focus || inside.current.pointer) {
      store.pause(toast.id);
    } else {
      store.resume(toast.id);
    }
  };
  return (
    <div className={cx(styles.toast, toast.tone === "critical" && styles.critical)}
      onFocus={() => { hold("focus", true); }}
      onBlur={(event: FocusEvent<HTMLDivElement>) => {
        if (!event.currentTarget.contains(event.relatedTarget)) {
          hold("focus", false);
        }
      }}
      onMouseEnter={() => { hold("pointer", true); }}
      onMouseLeave={() => { hold("pointer", false); }}>
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
  );
}

export function Toaster({ store, children }: { store: ToastStore; children: ReactNode }) {
  const { t } = useTranslation();
  const toasts = useSyncExternalStore(store.subscribe, store.getSnapshot);
  return (
    <ToastContext.Provider value={store}>
      {children}
      <DismissableLayerBranch asChild>
        <section className={styles.region} aria-label={t("toast.region")} aria-live="polite">
          {toasts.map((toast) => <ToastItem key={toast.id} toast={toast} store={store} />)}
        </section>
      </DismissableLayerBranch>
    </ToastContext.Provider>
  );
}
