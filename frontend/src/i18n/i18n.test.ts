import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import codes from "./codes.json";
import de from "./de.json";
import en from "./en.json";
import i18n, { browserLanguage, initI18n, useServerText } from "./index";

const PLACEHOLDER = /\{\{\s*(\w+)\s*\}\}/g;

function placeholders(text: string): string[] {
  return [...text.matchAll(PLACEHOLDER)].map((m) => m[1]).sort();
}

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("the catalogues", () => {
  it("en and de have the same keys", () => {
    const enKeys = Object.keys(en).sort();
    const deKeys = Object.keys(de).sort();
    expect(deKeys.filter((k) => !enKeys.includes(k)), "only in de").toEqual([]);
    expect(enKeys.filter((k) => !deKeys.includes(k)), "only in en").toEqual([]);
  });

  it("every catalogue code has a key in both", () => {
    const missing: string[] = [];
    // `order` is no namespace: its lists hold namespaces' codes in their backend order
    const namespaces = Object.entries(codes).filter(([namespace]) => namespace !== "order") as [string, string[]][];
    for (const [namespace, list] of namespaces) {
      for (const code of list) {
        for (const [lang, table] of [["en", en], ["de", de]] as const) {
          if (!(`${namespace}.${code}` in table)) {
            missing.push(`${lang}: ${namespace}.${code}`);
          }
        }
      }
    }
    expect(missing).toEqual([]);
  });

  it("has no empty text, and the same placeholders in both languages", () => {
    const german = new Map<string, string>(Object.entries(de));
    for (const [key, text] of Object.entries(en)) {
      const translated = german.get(key) ?? "";
      expect(text.trim(), key).not.toBe("");
      expect(translated.trim(), key).not.toBe("");
      expect(placeholders(translated), key).toEqual(placeholders(text));
    }
  });
});

describe("the catalogue's orders", () => {
  it("each lists exactly its namespace's codes", () => {
    expect([...codes.order.seniority].sort()).toEqual(codes.seniority);
  });
});

describe("initI18n", () => {
  it("resolves a dotted alert code in both languages", async () => {
    initI18n("en");
    expect(i18n.t("alert.batch.retrieve_failing")).toBe(en["alert.batch.retrieve_failing"]);
    await i18n.changeLanguage("de");
    expect(i18n.t("alert.batch.retrieve_failing")).toBe(de["alert.batch.retrieve_failing"]);
    expect(de["alert.batch.retrieve_failing"]).not.toBe(en["alert.batch.retrieve_failing"]);
  });

  it("never reads a dot as a path or a colon as a namespace, nor escapes an interpolated value", () => {
    initI18n("en");
    expect(i18n.options.keySeparator).toBe(false);
    expect(i18n.options.nsSeparator).toBe(false);
    // a code with a colon of its own resolves as one key (with nsSeparator ":" it would look in namespace "alert.x")
    // i18next keeps the bundled table itself (not a copy), so the key is deleted from it afterwards
    const table = en as Record<string, string>;
    i18n.addResource("en", "translation", "alert.x:y", "An alert with a colon");
    expect(table["alert.x:y"]).toBe("An alert with a colon");
    try {
      const { result } = renderHook(() => useServerText());
      expect(result.current("alert", "x:y", "fallback")).toBe("An alert with a colon");
      expect(result.current("alert", "x:z", "fallback <b>")).toBe("fallback <b>");
    } finally {
      delete table["alert.x:y"];
    }
    expect(i18n.exists("alert.x:y")).toBe(false);
    expect(i18n.t("login.throttled", { minutes: "<5>" })).toContain("<5>"); // React escapes, not i18next
  });

  it("sets the document's language and follows a change", async () => {
    initI18n("de");
    expect(document.documentElement.lang).toBe("de");
    await i18n.changeLanguage("en");
    expect(document.documentElement.lang).toBe("en");
  });
});

describe("useServerText", () => {
  it("translates a known code and falls back to the server text, then to the code", async () => {
    initI18n("de");
    const { result, rerender } = renderHook(() => useServerText());
    expect(result.current("problem", "stale_base_version", "Base version is stale")).toBe(
      de["problem.stale_base_version"],
    );
    expect(result.current("banner", "something_new", "Something new")).toBe("Something new");
    expect(result.current("banner", "something_new")).toBe("something_new");
    await i18n.changeLanguage("en");
    rerender();
    expect(result.current("problem", "stale_base_version")).toBe(en["problem.stale_base_version"]);
  });
});

describe("browserLanguage", () => {
  it("is de for a German browser and en for any other", () => {
    expect(browserLanguage("de-DE")).toBe("de");
    expect(browserLanguage("de")).toBe("de");
    expect(browserLanguage("DE-at")).toBe("de");
    expect(browserLanguage("en-GB")).toBe("en");
    expect(browserLanguage("fr-FR")).toBe("en");
    expect(browserLanguage("")).toBe("en");
  });
});
