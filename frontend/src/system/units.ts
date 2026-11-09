// Durations and sizes as the System section shows them (plan M4 Task 31, resolution e): `Intl.NumberFormat`'s
// units in the UI language's locale (`2 mins 5 secs`, `58.7 MB`; `2 Min. 5 Sek.`, `58,7 MB`).
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { intlLocale } from "../i18n/index";

export interface Units {
  /** A duration in seconds, in its two largest units (`3 hrs 22 mins`); under a minute in seconds. */
  duration: (seconds: number) => string;
  /** A size in bytes, in the largest decimal unit below 1,000 of it (`58.7 MB`). */
  bytes: (bytes: number) => string;
}

const SIZES = ["byte", "kilobyte", "megabyte", "gigabyte", "terabyte"] as const;
const STEPS = [
  ["day", 86_400],
  ["hour", 3_600],
  ["minute", 60],
  ["second", 1],
] as const;

export function makeUnits(locale: string): Units {
  const unit = (name: string, fraction = 0) =>
    new Intl.NumberFormat(locale, { style: "unit", unit: name, unitDisplay: "short", maximumFractionDigits: fraction });
  const times = STEPS.map(([name, size]) => ({ size, format: unit(name) }));
  const sizes = SIZES.map((name, i) => unit(name, i === 0 ? 0 : 1));
  return {
    duration: (seconds) => {
      const total = Math.max(0, Math.round(seconds));
      const first = times.findIndex(({ size }) => total >= size);
      const major = first < 0 ? undefined : times.at(first);
      if (major === undefined) {
        return unit("second").format(0);
      }
      const count = Math.floor(total / major.size);
      const parts = [major.format.format(count)];
      const minor = times.at(first + 1);
      const rest = minor === undefined ? 0 : Math.floor((total - count * major.size) / minor.size);
      if (minor !== undefined && rest > 0) {
        parts.push(minor.format.format(rest));
      }
      return parts.join(" ");
    },
    bytes: (bytes) => {
      let value = Math.max(0, bytes);
      let index = 0;
      while (value >= 1000 && index < SIZES.length - 1) {
        value /= 1000;
        index += 1;
      }
      return (sizes.at(index) ?? unit("byte")).format(value);
    },
  };
}

/** The units of the UI language. */
export function useUnits(): Units {
  const { i18n } = useTranslation();
  const locale = intlLocale(i18n.language);
  return useMemo(() => makeUnits(locale), [locale]);
}
