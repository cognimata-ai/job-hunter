// An editable combobox with a list popup (WAI-ARIA 1.2, "list autocomplete without automatic selection"; plan
// M4 Task 23, Ruling f): an `input role="combobox"` that owns a `listbox` of `option`s. Built by hand: there is
// no combobox package.
//
// - Typing opens the list (the caller reads the options for the text); Down and Up open it too, or move the
//   active option (wrapping), which `aria-activedescendant` names; Enter picks the active option; Esc closes
//   the list; Tab leaves it closed, picking nothing. A pointer picks an option without taking focus from the
//   field.
// - The caller owns the text and the options; the list's open state and its active option are the
//   component's.
import { type ReactNode, type Ref, useId, useState } from "react";
import { cx } from "./cx";
import styles from "./Combobox.module.css";

export interface ComboboxProps<T> {
  /** The field's visible label. */
  label: string;
  /** The field's text. */
  value: string;
  onValueChange: (text: string) => void;
  /** Called when the list opens (the caller may read its options then). */
  onOpenChange?: (open: boolean) => void;
  options: readonly T[];
  optionKey: (option: T) => string;
  /** The option as it is read and shown. */
  optionText: (option: T) => string;
  onPick: (option: T) => void;
  /** The text below the list when it holds no option (a search that found nothing); none while loading. */
  emptyText: string;
  loading?: boolean;
  /** The field's `aria-describedby` (a hint, an error). */
  describedBy?: string;
  /** Called when focus leaves the field. */
  onBlur?: () => void;
  /** What follows the field inside its row (a clear button). */
  after?: ReactNode;
  /** The field itself (a caller returns focus to it). */
  inputRef?: Ref<HTMLInputElement>;
  className?: string;
}

export function Combobox<T>({
  label,
  value,
  onValueChange,
  onOpenChange,
  options,
  optionKey,
  optionText,
  onPick,
  emptyText,
  loading = false,
  describedBy,
  onBlur,
  after,
  inputRef,
  className,
}: ComboboxProps<T>) {
  const id = useId();
  const listId = `${id}-list`;
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const shownActive = open && active < options.length ? active : -1;
  const setOpenState = (next: boolean, nextActive = -1) => {
    setActive(nextActive);
    if (next !== open) {
      setOpen(next);
      onOpenChange?.(next);
    }
  };
  const pick = (option: T) => {
    setOpenState(false);
    onPick(option);
  };
  const move = (step: 1 | -1) => {
    if (!open) {
      setOpenState(true, step === 1 ? 0 : options.length - 1);
      return;
    }
    if (options.length === 0) {
      return;
    }
    const from = shownActive === -1 ? (step === 1 ? -1 : options.length) : shownActive;
    setActive((from + step + options.length) % options.length);
  };
  const optionId = (index: number) => `${id}-option-${String(index)}`;
  return (
    <div className={cx(styles.combobox, className)}>
      <label htmlFor={`${id}-input`} className={styles.label}>{label}</label>
      <div className={styles.row}>
        <input ref={inputRef} id={`${id}-input`} type="text" role="combobox" className={styles.input} value={value}
          autoComplete="off" spellCheck={false} aria-autocomplete="list" aria-expanded={open} aria-controls={listId}
          aria-activedescendant={shownActive === -1 ? undefined : optionId(shownActive)} aria-describedby={describedBy}
          onChange={(event) => {
            onValueChange(event.target.value);
            setOpenState(true);
          }}
          onBlur={() => {
            setOpenState(false);
            onBlur?.();
          }}
          onKeyDown={(event) => {
            switch (event.key) {
              case "ArrowDown":
              case "ArrowUp":
                event.preventDefault();
                move(event.key === "ArrowDown" ? 1 : -1);
                break;
              case "Enter":
                if (open && shownActive !== -1) {
                  event.preventDefault();
                  pick(options[shownActive]);
                }
                break;
              case "Escape":
                if (open) {
                  event.preventDefault();
                  setOpenState(false);
                }
                break;
              default:
                break;
            }
          }} />
        {after}
      </div>
      <div className={styles.popup} hidden={!open}>
        <ul id={listId} role="listbox" aria-label={label} className={styles.list}>
          {options.map((option, index) => (
            <li key={optionKey(option)} id={optionId(index)} role="option" aria-selected={index === shownActive}
              className={cx(styles.option, index === shownActive && styles.active)}
              ref={index === shownActive ? (element) => { element?.scrollIntoView({ block: "nearest" }); } : undefined}
              onMouseDown={(event) => { event.preventDefault(); }}
              onClick={() => { pick(option); }}>
              {optionText(option)}
            </li>
          ))}
        </ul>
        {options.length === 0 && !loading && <p className={styles.empty}>{emptyText}</p>}
      </div>
    </div>
  );
}
