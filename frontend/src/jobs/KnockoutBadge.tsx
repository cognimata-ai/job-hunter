// The knockout badges of a card (spec §15.3, §11.4; plan M4 Task 22, Ruling h, B21): one per knockout that is
// `unmet` or `uncomparable` (a `met` one is not shown), red when it binds and amber otherwise, and the text says
// which: "⚑ Requires German C1 — you: B2 (unconfirmed)", where "(unconfirmed)" marks one that does not bind.
// Without a `profile_value` the "— you: …" part is left out; without a `requirement` the badge names the
// knockout's code. A `mandatory_language` requirement is "<iso> <level>" (`de C1`): its ISO code is named in
// the UI language ("German", "Deutsch"), and a code `Intl` does not know, or an ad's own name of a language,
// stays as it is.
import { useTranslation } from "react-i18next";
import { cx } from "../components/cx";
import { intlLocale, useServerText } from "../i18n/index";
import type { KnockoutOut } from "./api";
import styles from "./KnockoutBadge.module.css";

const SHOWN: ReadonlySet<string> = new Set(["unmet", "uncomparable"]);
const ISO_LANGUAGE = /^[a-z]{2,3}$/;

/** The knockouts a card shows, in the server's order. */
export function shownKnockouts(knockouts: readonly KnockoutOut[]): KnockoutOut[] {
  return knockouts.filter((knockout) => SHOWN.has(knockout.status));
}

/** The language of an ISO code in `language`'s words, `null` for a code `Intl` does not know. */
function languageName(code: string, language: string): string | null {
  if (!ISO_LANGUAGE.test(code)) {
    return null;
  }
  try {
    return new Intl.DisplayNames(intlLocale(language), { type: "language", fallback: "none" }).of(code) ?? null;
  } catch {
    return null; // a code that is no language tag: kept as it is
  }
}

/** A `mandatory_language` requirement with its ISO code named: "de C1" → "German C1". */
function namedLanguage(requirement: string, language: string): string {
  const [code = "", ...rest] = requirement.split(" ");
  const name = languageName(code, language);
  return name === null ? requirement : [name, ...rest].join(" ");
}

export function KnockoutBadge({ knockout }: { knockout: KnockoutOut }) {
  const { t, i18n } = useTranslation();
  const serverText = useServerText();
  const { code, requirement, profile_value: profileValue, binding } = knockout;
  const parts = [
    requirement === null
      ? t("card.knockout_named", { name: serverText("knockout", code) })
      : t("card.knockout_requires", {
          requirement: code === "mandatory_language" ? namedLanguage(requirement, i18n.language) : requirement,
        }),
  ];
  if (profileValue !== null) {
    parts.push(t("card.knockout_you", { value: profileValue }));
  }
  if (!binding) {
    parts.push(t("card.knockout_unconfirmed"));
  }
  return <span className={cx(styles.knockout, binding ? styles.binding : styles.unconfirmed)}>{parts.join(" ")}</span>;
}

/** The list of a card's shown knockouts; nothing when none is shown. */
export function KnockoutBadges({ knockouts }: { knockouts: readonly KnockoutOut[] }) {
  const { t } = useTranslation();
  const shown = shownKnockouts(knockouts);
  if (shown.length === 0) {
    return null;
  }
  return (
    <ul role="list" className={styles.list} aria-label={t("card.knockouts")}>
      {shown.map((knockout, i) => (
        <li key={`${knockout.code}-${String(i)}`}>
          <KnockoutBadge knockout={knockout} />
        </li>
      ))}
    </ul>
  );
}
