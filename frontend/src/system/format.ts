// How the header strip, the banners and the run report show numbers, amounts and times (plan M4 Task 21,
// Ruling j): `Intl` in the UI language's locale (`intlLocale`), times and dates in the display time zone
// (`settings.display.timezone`), never the browser's.
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { useDisplay } from "../app/display";
import { intlLocale } from "../i18n/index";

export interface Format {
  /** A count: `2,250` (en), `2.250` (de). */
  number: (n: number) => string;
  /** A USD amount (a `DecimalOut` string, parsed for display only): `$38.20` (en), `38,20 $` (de). */
  usd: (amount: string) => string;
  /** A day and month: `16 Nov` (en), `16. Nov.` (de). */
  dayMonth: (instant: string) => string;
  /** A day, month and 24-hour time: `4 Nov, 05:30`. */
  dayMonthTime: (instant: string) => string;
  /** A schedule slot: its time alone (`14:00`) on the same local day as `now`, else `dayMonthTime`. */
  slot: (instant: string, now?: Date) => string;
  /** A full date and time with seconds: `3 Nov 2026, 06:00:00` (en), `03.11.2026, 06:00:00` (de). */
  dateTime: (instant: string) => string;
}

export function makeFormat(locale: string, timeZone: string): Format {
  const numbers = new Intl.NumberFormat(locale);
  const dollars = new Intl.NumberFormat(locale, { style: "currency", currency: "USD", currencyDisplay: "narrowSymbol" });
  const clock = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" } as const;
  const time = new Intl.DateTimeFormat(locale, { ...clock, timeZone });
  const dayMonth = new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", timeZone });
  const dayMonthTime = new Intl.DateTimeFormat(locale, { day: "numeric", month: "short", ...clock, timeZone });
  const dateTime = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "medium", timeZone });
  const localDay = new Intl.DateTimeFormat("en-CA", { year: "numeric", month: "2-digit", day: "2-digit", timeZone });
  return {
    number: (n) => numbers.format(n),
    usd: (amount) => dollars.format(Number(amount)),
    dayMonth: (instant) => dayMonth.format(new Date(instant)),
    dayMonthTime: (instant) => dayMonthTime.format(new Date(instant)),
    slot: (instant, now = new Date()) => {
      const date = new Date(instant);
      return localDay.format(date) === localDay.format(now) ? time.format(date) : dayMonthTime.format(date);
    },
    dateTime: (instant) => dateTime.format(new Date(instant)),
  };
}

/** The formats of the UI language and the display time zone. */
export function useFormat(): Format {
  const { i18n } = useTranslation();
  const { timezone } = useDisplay();
  const locale = intlLocale(i18n.language);
  return useMemo(() => makeFormat(locale, timezone), [locale, timezone]);
}
