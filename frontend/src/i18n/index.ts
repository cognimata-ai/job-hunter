/**
 * The UI's strings (spec §15.9, D39): English (the default) and German, bundled (no backend loads them).
 *
 * Key scheme: one flat table per language. Every key is a whole string, `keySeparator: false`, so a dot is
 * part of the key and never a path: a server code is looked up as `"<namespace>.<code>"`, and a code that has
 * dots of its own (`alert.batch.retrieve_failing`) is still one key. `nsSeparator: false`, so a `:` in a code
 * is never read as a namespace either. The namespaces of the server's codes are the keys of `codes.json` (but
 * `order`, which lists the seniority codes in their ladder's order);
 * `useServerText` translates a code and falls back to the server's own text for a code the catalogue lacks.
 *
 * The language is the browser's on the login page (`browserLanguage`) and `settings.display.ui_language`
 * once signed in (`AppShell`); `document.documentElement.lang` follows every change.
 */
import i18n from "i18next";
import { useCallback } from "react";
import { initReactI18next, useTranslation } from "react-i18next";
import codes from "./codes.json";
import de from "./de.json";
import en from "./en.json";

export type Language = "en" | "de";
export type TranslationKey = keyof typeof en;
/** A namespace of `codes.json`: its keys but `order`, which lists some namespaces' codes in their backend order. */
export type CodeNamespace = Exclude<keyof typeof codes, "order">;

declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "translation";
    resources: { translation: typeof en };
    keySeparator: false;
    nsSeparator: false;
  }
}

/** `de` for a browser whose language starts with `de`, else `en`. */
export function browserLanguage(language: string = navigator.language): Language {
  return language.toLowerCase().startsWith("de") ? "de" : "en";
}

/** The `Intl` locale of a UI language: day-month order and a 24-hour clock in both. */
export function intlLocale(language: string): string {
  return language === "de" ? "de-DE" : "en-GB";
}

function followDocumentLanguage(language: string): void {
  document.documentElement.lang = language;
}

/** Initialises i18next in `lang` (synchronously: the tables are bundled); later calls change the language. */
export function initI18n(lang: Language): void {
  if (i18n.isInitialized) {
    void i18n.changeLanguage(lang);
    return;
  }
  i18n.on("languageChanged", followDocumentLanguage);
  void i18n.use(initReactI18next).init({
    resources: { en: { translation: en }, de: { translation: de } },
    lng: lang,
    fallbackLng: "en",
    supportedLngs: ["en", "de"],
    keySeparator: false,
    nsSeparator: false,
    interpolation: { escapeValue: false }, // React escapes what it renders
    returnNull: false,
    initAsync: false,
  });
  followDocumentLanguage(i18n.language);
}

/** The translation of a server code (`<namespace>.<code>`), else `fallback` (the server's text), else the
 * code itself. */
export function useServerText(): (ns: CodeNamespace, code: string, fallback?: string) => string {
  const { t } = useTranslation();
  return useCallback(
    (ns: CodeNamespace, code: string, fallback?: string) => {
      const key = `${ns}.${code}`;
      return i18n.exists(key) ? t(key as TranslationKey) : (fallback ?? code);
    },
    [t],
  );
}

export default i18n;
