// The modal layer under the drawer and the confirmation dialog (spec §15.4, §15.9; plan Deviation 5): Radix
// Portal, DismissableLayer (Esc, a pointer down outside) and FocusScope (trapped, looping; focus returns to
// the opener on close), and none of Radix Dialog's scroll-lock helpers, which inject a <style> element. A
// backdrop covers the page behind, so a click outside lands on it and dismisses; the page scroll is locked by
// a class (`lockScroll`), never by a style attribute.
//
// A layer opened from another (a confirmation from the drawer) is rendered inside it, as its React
// descendant, even though both portal to <body>: Radix decides "outside" per layer from the React tree, so a
// descendant's clicks are inside the drawer and do not close it, Esc closes the topmost layer only, and the
// inner FocusScope pauses the outer one until it closes. A layer rendered as a sibling would close the
// drawer on every click inside the dialog.
import { DismissableLayer } from "@radix-ui/react-dismissable-layer";
import { FocusScope } from "@radix-ui/react-focus-scope";
import { Portal } from "@radix-ui/react-portal";
import { type ReactNode, useEffect, useRef } from "react";
import styles from "./ModalLayer.module.css";
import { lockScroll } from "./scrollLock";

export interface ModalLayerProps {
  onDismiss: () => void;
  role: "dialog" | "alertdialog";
  labelledBy: string;
  describedBy?: string;
  /** `container`: the panel itself takes the focus (its title is read); `first`: its first control. */
  initialFocus: "container" | "first";
  placement: "right" | "center";
  className: string;
  children: ReactNode;
}

export function ModalLayer(props: ModalLayerProps) {
  const { onDismiss, role, labelledBy, describedBy, initialFocus, placement, className, children } = props;
  const panel = useRef<HTMLDivElement>(null);
  useEffect(lockScroll, []);
  return (
    <Portal>
      <div className={placement === "center" ? styles.backdropCenter : styles.backdrop} aria-hidden="true" />
      <DismissableLayer asChild onEscapeKeyDown={onDismiss} onPointerDownOutside={onDismiss}
        onFocusOutside={(event) => { event.preventDefault(); }}>
        <FocusScope asChild trapped loop
          onMountAutoFocus={(event) => {
            if (initialFocus === "container") {
              event.preventDefault();
              panel.current?.focus();
            }
          }}>
          <div ref={panel} role={role} aria-modal="true" aria-labelledby={labelledBy} aria-describedby={describedBy}
            tabIndex={-1} className={className}>
            {children}
          </div>
        </FocusScope>
      </DismissableLayer>
    </Portal>
  );
}
