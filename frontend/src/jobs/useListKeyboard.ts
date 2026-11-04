// The Jobs list's keyboard (spec §15.9; plan M4 Task 23, Ruling g): `j` and `k` move focus to the next and the
// previous card, and Enter on a focused card opens it (the card's own handler, `JobCard`, calls the `onOpen`
// given here). One card is in the tab order (`focused`, a roving `tabIndex`): the last one focus was in, else the
// first.
//
// - The keys are the page's, wherever focus is, except: with a modifier held, while typing (an `input`,
//   `textarea`, `select` or `[contenteditable]`), inside a dialog, or once something else has handled the key.
//   From outside the list, `j` or `k` focuses its current card.
// - A card that leaves the list while focus is inside it (Hide, or a write after which the filters exclude it)
//   takes focus with it to `body`; focus then moves to the next card that is still listed, else the previous
//   one, else the list heading. A card that leaves while focus is elsewhere moves nothing.
import { type RefObject, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

/** Where the keys are not the list's. */
const NOT_THE_LIST = 'input, textarea, select, [contenteditable]:not([contenteditable="false"]), [role="dialog"], [role="alertdialog"]';

export interface CardProps {
  focused: boolean;
  onOpen: (jobId: number) => void;
}

export interface ItemProps {
  ref: (element: HTMLLIElement | null) => void;
  onFocus: () => void;
}

export interface ListKeyboard {
  /** A card's `focused` and `onOpen` (`JobCard`'s props). */
  cardProps: (jobId: number) => CardProps;
  /** The props of the list item that holds a card. */
  itemProps: (jobId: number) => ItemProps;
  /** The list's heading (focusable with `tabIndex={-1}`), focused when no card is left. */
  headingRef: RefObject<HTMLHeadingElement | null>;
  /** Moves focus to a listed card. */
  focusCard: (jobId: number) => void;
}

/** Whether focus has dropped to the document: nothing, or `body`, holds it. */
export function focusDropped(): boolean {
  const active = document.activeElement;
  return active === null || active === document.body || !active.isConnected;
}

/** Whether a key is not the list's (module comment): a modifier held, typing, a dialog's, or handled already.
 * Label mode's `1`/`2`/`3` follow the same rule. */
export function ignoredKey(event: KeyboardEvent): boolean {
  if (event.defaultPrevented || event.isComposing || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) {
    return true;
  }
  return event.target instanceof Element && event.target.closest(NOT_THE_LIST) !== null;
}

export function useListKeyboard(ids: readonly number[], onOpen: (jobId: number) => void): ListKeyboard {
  const [current, setCurrent] = useState<number | null>(null);
  const focusedId = current !== null && ids.includes(current) ? current : (ids.at(0) ?? null);
  const items = useRef(new Map<number, HTMLLIElement>());
  const refs = useRef(new Map<number, (element: HTMLLIElement | null) => void>());
  const headingRef = useRef<HTMLHeadingElement>(null);
  // the card focus was last inside (`focusin`); not cleared when focus leaves for `body`
  const lastFocused = useRef<number | null>(null);
  const shown = useRef({ ids, focusedId });
  const before = useRef(ids);

  const focusCard = useCallback((jobId: number) => {
    setCurrent(jobId);
    items.current.get(jobId)?.querySelector<HTMLElement>(":scope > article")?.focus();
  }, []);

  useLayoutEffect(() => {
    shown.current = { ids, focusedId };
  }, [ids, focusedId]);

  useEffect(() => {
    const cardWith = (target: EventTarget | null): number | null => {
      if (!(target instanceof Node)) {
        return null;
      }
      for (const [jobId, item] of items.current) {
        if (item.contains(target)) {
          return jobId;
        }
      }
      return null;
    };
    const onFocusIn = (event: FocusEvent) => {
      lastFocused.current = cardWith(event.target);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.key !== "j" && event.key !== "k") || ignoredKey(event)) {
        return;
      }
      const { ids: listed, focusedId: roving } = shown.current;
      const from = cardWith(event.target);
      const at = from === null ? -1 : listed.indexOf(from);
      const to = at === -1 ? roving : listed[event.key === "j" ? Math.min(at + 1, listed.length - 1) : Math.max(at - 1, 0)];
      if (to !== null) {
        event.preventDefault();
        focusCard(to);
      }
    };
    document.addEventListener("focusin", onFocusIn);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("focusin", onFocusIn);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [focusCard]);

  // after a change of the list: the card focus was in has left, and focus with it
  useLayoutEffect(() => {
    const previous = before.current;
    before.current = ids;
    const gone = lastFocused.current;
    if (gone === null || ids.includes(gone) || !focusDropped()) {
      return;
    }
    const at = previous.indexOf(gone);
    const next = previous.slice(at + 1).find((jobId) => ids.includes(jobId));
    const back = previous.slice(0, Math.max(at, 0)).reverse().find((jobId) => ids.includes(jobId));
    const to = next ?? back;
    if (to === undefined) {
      headingRef.current?.focus();
    } else {
      focusCard(to);
    }
  }, [ids, focusCard]);

  const itemProps = useCallback((jobId: number): ItemProps => {
    let ref = refs.current.get(jobId);
    if (ref === undefined) {
      ref = (element) => {
        if (element === null) {
          items.current.delete(jobId); // the card has left the list
          refs.current.delete(jobId);
        } else {
          items.current.set(jobId, element);
        }
      };
      refs.current.set(jobId, ref);
    }
    return { ref, onFocus: () => { setCurrent(jobId); } };
  }, []);

  const cardProps = useCallback((jobId: number): CardProps => ({ focused: jobId === focusedId, onOpen }), [focusedId, onOpen]);

  return { cardProps, itemProps, headingRef, focusCard };
}
