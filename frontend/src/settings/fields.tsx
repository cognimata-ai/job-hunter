// The settings form's fields (plan M4 Task 28, resolution d): small inputs bound to a path of the draft
// (`useSettingsForm`). Each has a visible label and an `id` derived from its path (`fieldId`), and carries the
// refused fields it holds: `aria-invalid` and `aria-describedby` naming each issue in its section's dry-run panel
// (`useFieldIssues`). The bounds a field passes (`min`, `max`) only guide the input: the dry-run's errors are the
// check, never a second copy of the validator.
import * as Slider from "@radix-ui/react-slider";
import { type KeyboardEvent, useState } from "react";
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import styles from "./fields.module.css";
import { claims, fieldId, getAt, issueId, sectionOf, useSection, useSettingsForm } from "./useSettingsForm";

export interface FieldProps {
  /** The payload path the field writes (`schedule.interval_hours`). */
  path: string;
  label: string;
  /** A line below the field. */
  hint?: string;
}

/** The issues the field at `path` holds (its own path, and unless `exact` the paths inside it): whether it is
 * invalid, and their ids in its section's panel. */
export function useFieldIssues(path: string, { exact = false }: { exact?: boolean } = {}): {
  invalid: boolean;
  issueIds: string[];
} {
  const form = useSettingsForm();
  const section = useSection() ?? sectionOf(path);
  if (section === null) {
    return { invalid: false, issueIds: [] };
  }
  const issueIds = form.issues.flatMap((issue, i) => (claims(path, issue.path, exact) ? [issueId(section, i)] : []));
  return { invalid: issueIds.length > 0, issueIds };
}

/** The members of a closed set in the order its record lists them (the model's): `Record<T, …>` holds the record
 * to the set both ways, no member missing and none extra. */
export function closedSet<T extends string>(members: Record<T, unknown>): T[] {
  return Object.keys(members) as T[];
}

/** Ids joined for `aria-describedby`, `undefined` for none. */
export function describedBy(...ids: (string | null | undefined | false | readonly string[])[]): string | undefined {
  const joined = ids.flat().filter((id): id is string => typeof id === "string" && id !== "").join(" ");
  return joined === "" ? undefined : joined;
}

function Hint({ id, text }: { id: string; text: string | undefined }) {
  return text === undefined ? null : <p id={id} className={styles.hint}>{text}</p>;
}

/** What a field's text shows: the stored value, kept as typed while it means that value (`120,50` for
 * `"120.50"`), and replaced when the value changes from elsewhere (a rebase, a reapply). */
function useTyped(stored: string, toStored: (text: string) => string): [string, (text: string) => void] {
  const [text, setText] = useState(stored);
  const [seen, setSeen] = useState(stored);
  if (stored !== seen) {
    setSeen(stored);
    if (toStored(text) !== stored) {
      setText(stored);
    }
  }
  return [text, setText];
}

function decimalText(text: string): string {
  return text.trim().replace(",", ".");
}

/**
 * A number: an integer (`null` when emptied), or with `decimal` a JSON decimal string as the document stores
 * amounts and rates (`"100.00"`; a comma is taken as the decimal point). `unit` follows the field.
 */
export function NumberField({
  path,
  label,
  hint,
  min,
  max,
  step = 1,
  unit,
  decimal = false,
}: FieldProps & { min?: number; max?: number; step?: number; unit?: string; decimal?: boolean }) {
  const form = useSettingsForm();
  const id = fieldId(path);
  const hintId = `${id}-hint`;
  const { invalid, issueIds } = useFieldIssues(path);
  const value = getAt(form.draft, path);
  const stored = typeof value === "number" || typeof value === "string" ? String(value) : "";
  const toStored = (text: string) => (decimal ? decimalText(text) : text.trim());
  const [text, setText] = useTyped(stored, toStored);
  const write = (typed: string) => {
    setText(typed);
    const trimmed = toStored(typed);
    if (decimal) {
      form.set(path, trimmed);
    } else {
      form.set(path, trimmed === "" ? null : Number(trimmed));
    }
  };
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>{label}</label>
      <div className={styles.row}>
        <input id={id} className={cx(styles.input, styles.number)} value={text}
          type={decimal ? "text" : "number"} inputMode={decimal ? "decimal" : "numeric"}
          min={decimal ? undefined : min} max={decimal ? undefined : max} step={decimal ? undefined : step}
          aria-invalid={invalid || undefined} aria-describedby={describedBy(issueIds, hint !== undefined && hintId)}
          onChange={(event) => { write(event.target.value); }} />
        {unit !== undefined && <span className={styles.unit}>{unit}</span>}
      </div>
      <Hint id={hintId} text={hint} />
    </div>
  );
}

/** A short text (`type="time"` for a time of day, `HH:MM`). */
export function TextField({ path, label, hint, type = "text" }: FieldProps & { type?: "text" | "time" }) {
  const form = useSettingsForm();
  const id = fieldId(path);
  const hintId = `${id}-hint`;
  const { invalid, issueIds } = useFieldIssues(path);
  const value = getAt(form.draft, path);
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>{label}</label>
      <input id={id} type={type} className={cx(styles.input, type === "time" && styles.time)}
        value={typeof value === "string" ? value : ""}
        aria-invalid={invalid || undefined} aria-describedby={describedBy(issueIds, hint !== undefined && hintId)}
        onChange={(event) => { form.set(path, event.target.value); }} />
      <Hint id={hintId} text={hint} />
    </div>
  );
}

/** A checkbox of a boolean. */
export function ToggleField({ path, label, hint }: FieldProps) {
  const form = useSettingsForm();
  const id = fieldId(path);
  const hintId = `${id}-hint`;
  const { invalid, issueIds } = useFieldIssues(path);
  return (
    <div className={styles.toggle}>
      <input id={id} type="checkbox" checked={getAt(form.draft, path) === true}
        aria-invalid={invalid || undefined} aria-describedby={describedBy(issueIds, hint !== undefined && hintId)}
        onChange={(event) => { form.set(path, event.target.checked); }} />
      <label htmlFor={id}>{label}</label>
      <Hint id={hintId} text={hint} />
    </div>
  );
}

/**
 * A closed set as checkboxes, written in the order of `options` (the order the model keeps a set in, so a
 * reordering is no change). With `keepOne` (the server refuses an empty set) the last ticked box is disabled,
 * and says why.
 */
export function CheckboxSetField<T extends string>({
  path,
  label,
  hint,
  options,
  optionLabel,
  keepOne = false,
}: FieldProps & { options: readonly T[]; optionLabel: (value: T) => string; keepOne?: boolean }) {
  const { t } = useTranslation();
  const form = useSettingsForm();
  const id = fieldId(path);
  const hintId = `${id}-hint`;
  const lastId = `${id}-last`;
  const { invalid, issueIds } = useFieldIssues(path);
  const value = getAt(form.draft, path);
  const selected: readonly string[] = Array.isArray(value) ? (value as string[]) : [];
  const only = keepOne && selected.length === 1 ? selected[0] : null;
  return (
    <fieldset id={id} className={styles.group} aria-describedby={describedBy(issueIds, hint !== undefined && hintId)}>
      <legend className={styles.legend}>{label}</legend>
      <div className={styles.choices}>
        {options.map((option) => (
          <label key={option} className={styles.choice}>
            <input type="checkbox" checked={selected.includes(option)} disabled={option === only}
              aria-invalid={invalid || undefined} aria-describedby={option === only ? lastId : undefined}
              onChange={(event) => {
                form.set(path, options.filter((other) => (other === option ? event.target.checked : selected.includes(other))));
              }} />
            <span>{optionLabel(option)}</span>
          </label>
        ))}
      </div>
      {only !== null && <p id={lastId} className={styles.hint}>{t("filters.last_one")}</p>}
      <Hint id={hintId} text={hint} />
    </fieldset>
  );
}

/**
 * A native select of a closed set. `empty` adds a first option for no value: `null` (an open bound) or
 * `undefined` (the key left out, so its default applies).
 */
export function SelectField<T extends string>({
  path,
  label,
  hint,
  options,
  optionLabel,
  empty,
}: FieldProps & {
  options: readonly T[];
  optionLabel: (value: T) => string;
  empty?: { label: string; value: null | undefined };
}) {
  const form = useSettingsForm();
  const id = fieldId(path);
  const hintId = `${id}-hint`;
  const { invalid, issueIds } = useFieldIssues(path);
  const value = getAt(form.draft, path);
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>{label}</label>
      <select id={id} className={styles.select} value={typeof value === "string" ? value : ""}
        aria-invalid={invalid || undefined} aria-describedby={describedBy(issueIds, hint !== undefined && hintId)}
        onChange={(event) => {
          const picked = options.find((option) => option === event.target.value);
          form.set(path, picked ?? empty?.value);
        }}>
        {empty !== undefined && <option value="">{empty.label}</option>}
        {options.map((option) => <option key={option} value={option}>{optionLabel(option)}</option>)}
      </select>
      <Hint id={hintId} text={hint} />
    </div>
  );
}

/** A list of free strings as chips: typed and added with Enter or Add, each removed by its × button. An issue
 * of one entry (`…[2]`) marks its chip. */
export function TextListField({ path, label, hint, maxLength }: FieldProps & { maxLength?: number }) {
  const { t } = useTranslation();
  const form = useSettingsForm();
  const id = fieldId(path);
  const hintId = `${id}-hint`;
  const section = useSection() ?? sectionOf(path);
  const { invalid, issueIds } = useFieldIssues(path);
  const [text, setText] = useState("");
  const value = getAt(form.draft, path);
  const items: readonly string[] = Array.isArray(value) ? (value as string[]) : [];
  const add = () => {
    const entry = text.trim();
    if (entry !== "") {
      form.set(path, [...items, entry]);
      setText("");
    }
  };
  const itemInvalid = (index: number) => form.issues.some((issue) => claims(`${path}[${String(index)}]`, issue.path));
  return (
    <div className={styles.field}>
      <label htmlFor={id} className={styles.label}>{label}</label>
      {items.length > 0 && (
        <ul role="list" className={styles.chips} aria-label={label}>
          {items.map((item, index) => (
            <li key={`${String(index)}-${item}`} className={cx(styles.chip, itemInvalid(index) && styles.chipInvalid)}
              aria-describedby={section === null ? undefined : describedBy(
                form.issues.flatMap((issue, i) => (claims(`${path}[${String(index)}]`, issue.path) ? [issueId(section, i)] : [])),
              )}>
              <span className={styles.chipText}>{item}</span>
              <button type="button" className={styles.chipRemove} aria-label={t("fields.remove", { value: item })}
                onClick={() => { form.set(path, items.filter((_, other) => other !== index)); }}>
                <span aria-hidden="true">×</span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className={styles.row}>
        <input id={id} type="text" className={styles.input} value={text} maxLength={maxLength}
          aria-invalid={invalid || undefined} aria-describedby={describedBy(issueIds, hint !== undefined && hintId)}
          onChange={(event) => { setText(event.target.value); }}
          onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
            if (event.key === "Enter") {
              event.preventDefault();
              add();
            }
          }} />
        <button type="button" className={styles.button} onClick={add} disabled={text.trim() === ""}>{t("fields.add")}</button>
      </div>
      <Hint id={hintId} text={hint} />
    </div>
  );
}

/** A number on a slider (Radix Slider: its thumb is the `slider`), its value shown beside the label as
 * `valueText` reads it. */
export function RangeSlider({
  path,
  label,
  hint,
  min,
  max,
  step = 1,
  valueText,
}: FieldProps & { min: number; max: number; step?: number; valueText: (value: number) => string }) {
  const form = useSettingsForm();
  const id = fieldId(path);
  const hintId = `${id}-hint`;
  const { invalid, issueIds } = useFieldIssues(path);
  const stored = getAt(form.draft, path);
  const value = typeof stored === "number" ? stored : min;
  return (
    <div className={styles.field}>
      <div className={styles.sliderHead}>
        <span className={styles.label}>{label}</span>
        <span className={styles.value}>{valueText(value)}</span>
      </div>
      <Slider.Root className={styles.slider} min={min} max={max} step={step} value={[value]}
        onValueChange={(values) => {
          const next = values.at(0);
          if (next !== undefined) {
            form.set(path, next);
          }
        }}>
        <Slider.Track className={styles.track}>
          <Slider.Range className={styles.range} />
        </Slider.Track>
        <Slider.Thumb id={id} className={styles.thumb} aria-label={label} aria-valuetext={valueText(value)}
          aria-invalid={invalid || undefined} aria-describedby={describedBy(issueIds, hint !== undefined && hintId)} />
      </Slider.Root>
      <Hint id={hintId} text={hint} />
    </div>
  );
}
