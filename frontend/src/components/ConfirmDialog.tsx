// A confirmation (spec §15.7): centred, modal, focus on Cancel first; Esc and a click outside cancel. While
// `busy`, the confirm button is disabled, so an action is never confirmed twice; `confirmDisabled` disables it
// without the busy mark, until the dialog's own condition holds (a tick, plan M4 Task 30).
import { type ReactNode, useId } from "react";
import { useTranslation } from "react-i18next";
import styles from "./ConfirmDialog.module.css";
import { ModalLayer } from "./ModalLayer";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  onConfirm: () => void;
  onCancel: () => void;
  confirmLabel: string;
  busy?: boolean;
  /** The confirm button is disabled, not busy (a required tick is not set yet). */
  confirmDisabled?: boolean;
  children?: ReactNode;
}

export function ConfirmDialog(props: ConfirmDialogProps) {
  const { open, title, onConfirm, onCancel, confirmLabel, busy = false, confirmDisabled = false, children } = props;
  const { t } = useTranslation();
  const id = useId();
  if (!open) {
    return null;
  }
  const bodyId = children === undefined ? undefined : `${id}-body`;
  return (
    <ModalLayer onDismiss={onCancel} role="alertdialog" labelledBy={`${id}-title`} describedBy={bodyId}
      initialFocus="first" placement="center" className={styles.dialog}>
      <h2 id={`${id}-title`} className={styles.title}>{title}</h2>
      {children !== undefined && <div id={bodyId} className={styles.body}>{children}</div>}
      <div className={styles.actions}>
        <button type="button" className={styles.secondary} onClick={onCancel}>{t("action.cancel")}</button>
        <button type="button" className={styles.primary} onClick={onConfirm} disabled={busy || confirmDisabled} aria-busy={busy}>
          {busy && <span className={styles.busy} aria-hidden="true" />}
          {confirmLabel}
        </button>
      </div>
    </ModalLayer>
  );
}
