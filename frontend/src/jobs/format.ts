// How a job card shows its scores and dates (spec §15.3; plan M4 Task 22, Ruling c).
//
// Scores and the coverage are `DecimalOut` strings in plain notation ("82.40", "0.850"): they are rounded
// half-up on the string itself, never through a float, so a value just below one half never rounds up (a float
// can hold "82.49999999999999999" only as 82.5). Dates are compared as calendar dates in the display time zone.
import type { TFunction } from "i18next";
import { relativeText } from "../components/RelativeTime";
import i18n, { type CodeNamespace, intlLocale, type TranslationKey } from "../i18n/index";
import type { Availability } from "./search";

/** `useServerText`'s function: a server code's text, else the fallback, else the code. */
export type ServerText = (ns: CodeNamespace, code: string, fallback?: string) => string;

// A `DecimalOut` in plain notation: digits, then an optional point and digits (no sign, no exponent).
const DECIMAL = /^(\d+)(?:\.(\d*))?$/;
// The precisions of a posted date stated to the day at best (`sources/parsing/dates.py`'s `Precision`).
const APPROXIMATE: ReadonlySet<string> = new Set(["day", "relative_day", "ingest"]);
const DAY_MS = 86_400_000;

/** `dec`, a non-negative decimal string, rounded half-up to an integer on its digits: "82.5" → 83,
 * "82.4999999" → 82. Anything else (a sign, an exponent, an empty string) throws. */
export function scoreInt(dec: string): number {
  const match = DECIMAL.exec(dec);
  if (match === null) {
    throw new RangeError(`not a decimal string: ${JSON.stringify(dec)}`);
  }
  const [, whole = "0", fraction = ""] = match;
  return Number(whole) + (fraction !== "" && fraction[0] >= "5" ? 1 : 0);
}

/** A fraction from 0 to 1 (`dec`, a decimal string) as a whole percentage, half-up: the point moved two places
 * on the string, then `scoreInt` ("0.845" → 85, "1" → 100). */
export function percentInt(dec: string): number {
  const match = DECIMAL.exec(dec);
  if (match === null) {
    throw new RangeError(`not a decimal string: ${JSON.stringify(dec)}`);
  }
  const [, whole = "0", fraction = ""] = match;
  const padded = fraction.padEnd(2, "0");
  return scoreInt(`${whole}${padded.slice(0, 2)}.${padded.slice(2)}`);
}

/** The calendar date of `instant` in `tz`, as days since the epoch. */
function localDay(instant: Date, tz: string): number {
  const parts = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone: tz })
    .formatToParts(instant);
  const part = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((p) => p.type === type)?.value);
  return Date.UTC(part("year"), part("month") - 1, part("day")) / DAY_MS;
}

/** Whether `at` falls on `now`'s calendar date in `tz`. */
export function seenToday(at: string, now: Date, tz: string): boolean {
  return localDay(new Date(at), tz) === localDay(now, tz);
}

/**
 * A posted date, relative to `now`, in the UI language (`i18n.language`, which `t` translates into):
 * - `day`, `relative_day` and `ingest` (stated to the day at best) are whole calendar days in `tz`, with `≈`
 *   ("≈ yesterday", "≈ 3 days ago"); `ingest` (the date the ad was collected) adds "(ingest date)";
 * - `exact` and `hour` are elapsed time in the largest unit that fits, as `RelativeTime` shows it;
 * - `unknown` (no date) reads "date unknown".
 */
export function postedLabel(at: string | null, precision: string, now: Date, tz: string, t: TFunction): string {
  const date = at === null ? null : new Date(at);
  if (date === null || precision === "unknown" || Number.isNaN(date.getTime())) {
    return t("posted.unknown");
  }
  const locale = intlLocale(i18n.language);
  if (!APPROXIMATE.has(precision)) {
    return relativeText(date, now, locale);
  }
  const days = localDay(date, tz) - localDay(now, tz);
  const when = new Intl.RelativeTimeFormat(locale, { numeric: "auto" }).format(days, "day");
  return t(precision === "ingest" ? "posted.ingest" : "posted.approx", { when });
}

/**
 * Why a card fails its hard filter (spec §15.3, §11.1; B24): "Filtered: excluded keyword '<keyword>'" when an
 * excluded keyword is named, else "Filtered: <reasons>" in the `filter_reason` texts, else the `filtered_out`
 * state's name. The unscored card's chip and the scored card's badge both read it.
 */
export function filteredText(
  keyword: string | null,
  reasons: readonly string[],
  t: TFunction,
  serverText: ServerText,
): string {
  if (keyword !== null) {
    return t("chip.filtered_keyword", { keyword });
  }
  if (reasons.length > 0) {
    return t("chip.filtered_reasons", { reasons: reasons.map((r) => serverText("filter_reason", r)).join(", ") });
  }
  return serverText("target_state", "filtered_out");
}

/** The availabilities' texts: the Availability filter's (Task 23), and an ad's availability in the drawer. */
export const AVAILABILITY_LABELS = {
  active: "filters.availability.active",
  unknown: "filters.availability.unknown",
  stale: "filters.availability.stale",
  closed: "filters.availability.closed",
} as const satisfies Record<Availability, TranslationKey>;

/** An ad's availability in the Availability filter's words; a value outside them as the server sends it. */
export function availabilityText(availability: string, t: TFunction): string {
  const key = new Map<string, TranslationKey>(Object.entries(AVAILABILITY_LABELS)).get(availability);
  return key === undefined ? availability : t(key);
}

// The texts of the known work modes (`settings.model.WorkMode`), as a card names them.
const WORK_MODE_KEYS: ReadonlyMap<string, TranslationKey> = new Map([
  ["remote", "card.mode.remote"],
  ["hybrid", "card.mode.hybrid"],
  ["onsite", "card.mode.onsite"],
] as const);

/** A work mode's text, `null` for a code outside the known modes. */
export function workModeLabel(mode: string, t: TFunction): string | null {
  const key = WORK_MODE_KEYS.get(mode);
  return key === undefined ? null : t(key);
}

/** An ad's work modes ("remote / hybrid"), a code outside the known modes as it is; "work mode unknown" when
 * the ad does not state them. The card and the drawer show them so. */
export function workModesText(modes: readonly string[], known: boolean, t: TFunction): string {
  return known ? modes.map((mode) => workModeLabel(mode, t) ?? mode).join(" / ") : t("card.mode_unknown");
}
