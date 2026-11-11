import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import doc from "../../../docs/PRIVACY.md?raw";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { fakeApi } from "../test/api";
import { renderApp } from "../test/app";
import { readText } from "../test/readText.mjs";
import { parsePrivacyDoc } from "./privacyDoc";
import { StatementBlocks } from "./PrivacyStatement";

afterEach(async () => {
  vi.restoreAllMocks();
  await i18n.changeLanguage("en");
});

const HERE = import.meta.url;

async function statement(): Promise<HTMLElement> {
  return screen.findByRole("region", { name: en["privacy_statement.title"] });
}

describe("PrivacyStatement", () => {
  it("renders the statement without raw HTML", async () => {
    const api = fakeApi({ signedIn: true });
    renderApp("/settings#privacy-statement");
    const section = await statement();
    expect(section.id).toBe("privacy-statement");
    // the document is English in every UI language
    const body = section.querySelector("div[lang=en]");
    expect(body).not.toBeNull();

    // its `##` sections, in order, as headings a level below the statement's own, each with its anchor
    const sections = doc.split("\n").filter((line) => line.startsWith("## ")).map((line) => line.slice(3));
    expect(sections).toHaveLength(11);
    const headings = within(section).getAllByRole("heading", { level: 3 });
    expect(headings.map((h) => h.textContent)).toEqual(sections);
    expect(headings[0]?.id).toBe("privacy-statement-1-purpose-and-controller");
    expect(headings.at(-1)?.id).toBe("privacy-statement-operator");
    // the contents link to them
    const contents = within(section).getByRole("navigation", { name: en["privacy_statement.contents"] });
    const links = within(contents).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual(sections);
    expect(links.map((a) => a.getAttribute("href"))).toEqual(headings.map((h) => `/settings#${h.id}`));

    // the three tables with their heads, bold as <strong>, code spans as <code>
    const tables = within(section).getAllByRole("table");
    expect(tables).toHaveLength(3);
    expect(within(tables[0]).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "Third party",
      "What it receives",
    ]);
    expect(within(tables[1]).getAllByRole("columnheader").map((th) => th.textContent)).toEqual([
      "Data",
      "Location",
      "Retention",
      "Purge mechanism",
    ]);
    expect(within(section).getByText("Purpose.").tagName).toBe("STRONG");
    expect(within(section).getAllByText("DELETE EVERYTHING").every((el) => el.tagName === "CODE")).toBe(true);
    expect(within(section).getAllByText("PURGE ADZUNA")[0]?.tagName).toBe("CODE");
    // the licence URI is text in a code span, never a link out
    expect(within(section).getByText("https://creativecommons.org/licenses/by/4.0/").tagName).toBe("CODE");
    expect(section.querySelector('a[href^="http"]')).toBeNull();
    // the operator's fields as the document has them
    expect(within(section).getByText("[set at deployment: operator name]", { exact: false })).toBeTruthy();

    // no Markdown left in the text, no element the document could not have made
    const text = body?.textContent ?? "";
    expect(text).toContain("Job Hunter is a single-user application");
    for (const raw of ["**", "`", "|---", "## ", "| "]) {
      expect(text, raw).not.toContain(raw);
    }
    expect(section.querySelectorAll("script, style, iframe, img, object, embed, [style]")).toHaveLength(0);
    expect(api.calls.filter((call) => call.includes("PRIVACY"))).toEqual([]); // bundled, never fetched

    // the component builds React elements, never HTML from a string
    const source = readText(new URL("./PrivacyStatement.tsx", HERE));
    expect(source).not.toMatch(/dangerouslySetInnerHTML|innerHTML|outerHTML|insertAdjacentHTML|DOMParser/);
    expect(source).toContain("docs/PRIVACY.md?raw");
  });

  it("shows HTML in the document as text", () => {
    const blocks = parsePrivacyDoc(
      ['<img src="x" onerror="alert(1)">', "", "- <b>bold?</b> and **<i>this</i>**", "", "| <a> | b |", "|---|---|", "| `<script>` | x |"].join("\n"),
    );
    const { container } = render(<StatementBlocks blocks={blocks} />);
    expect(container.querySelectorAll("img, b, i, a, script")).toHaveLength(0);
    expect(container.textContent).toContain('<img src="x" onerror="alert(1)">');
    expect(within(container).getByText("<i>this</i>").tagName).toBe("STRONG");
    expect(within(container).getByText("<script>").tagName).toBe("CODE");
  });

  it("says in German that the statement is English", async () => {
    const api = fakeApi({ signedIn: true });
    api.uiLanguage = "de";
    renderApp("/settings#privacy-statement");
    const section = await screen.findByRole("region", { name: de["privacy_statement.title"] });
    await waitFor(() => {
      expect(within(section).getByText(de["privacy_statement.english_only"])).toBeTruthy();
    });
    expect(within(section).getAllByRole("heading", { level: 3 })[0]?.textContent).toBe("1. Purpose and controller");
  });
});
