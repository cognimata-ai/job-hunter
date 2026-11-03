// The detail drawer's frame (spec §15.4): from the right at 50 % of the width, the full screen at 768 px and
// below; modal, focus trapped and restored, Esc and a click outside close it.
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "./cx";
import styles from "./Drawer.module.css";
import { ModalLayer } from "./ModalLayer";

export interface DrawerProps {
  open: boolean;
  onClose: () => void;
  /** The id of the drawer's title, in `children`: the dialog's name. */
  titleId: string;
  side?: "right";
  children: ReactNode;
}

export function Drawer({ open, onClose, titleId, side = "right", children }: DrawerProps) {
  const { t } = useTranslation();
  if (!open) {
    return null;
  }
  return (
    <ModalLayer onDismiss={onClose} role="dialog" labelledBy={titleId} initialFocus="container" placement={side}
      className={cx(styles.drawer, styles[side])}>
      <button type="button" className={styles.close} onClick={onClose} aria-label={t("action.close")}>
        <span aria-hidden="true">×</span>
      </button>
      <div className={styles.body}>{children}</div>
    </ModalLayer>
  );
}
