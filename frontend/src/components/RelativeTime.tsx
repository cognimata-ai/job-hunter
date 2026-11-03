// A time as "2 hours ago" in the UI language, with the absolute time in the display time zone as its title.
import { useTranslation } from "react-i18next";
import { intlLocale } from "../i18n/index";

const UNITS: readonly (readonly [Intl.RelativeTimeFormatUnit, number])[] = [
  ["year", 365 * 86_400],
  ["month", 30 * 86_400],
  ["week", 7 * 86_400],
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
];
const NOW_WITHIN_S = 45;

export interface RelativeTimeProps {
  /** An ISO 8601 instant (the api's times are UTC) or a Date. */
  value: string | Date;
  /** The IANA time zone of the absolute time (`settings.display.timezone`). */
  tz: string;
  /** The reference instant; the current time by default. */
  now?: Date;
}

export function RelativeTime({ value, tz, now }: RelativeTimeProps) {
  const { i18n } = useTranslation();
  const date = typeof value === "string" ? new Date(value) : value;
  if (Number.isNaN(date.getTime())) {
    return null;
  }
  const locale = intlLocale(i18n.language);
  const seconds = (date.getTime() - (now ?? new Date()).getTime()) / 1000;
  const relative = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const unit = UNITS.find(([, size]) => Math.abs(seconds) >= size);
  const text =
    Math.abs(seconds) < NOW_WITHIN_S || unit === undefined
      ? relative.format(0, "second")
      : relative.format(Math.round(seconds / unit[1]), unit[0]);
  const absolute = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short", timeZone: tz }).format(date);
  return (
    <time dateTime={date.toISOString()} title={absolute}>
      {text}
    </time>
  );
}
