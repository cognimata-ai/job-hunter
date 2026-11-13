import { act, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import doc from "../../../docs/PRIVACY.md?raw";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { fakeApi, json, problem, requestsTo } from "../test/api";
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
    // the operator's fields as the document has them, once the server answered that it has no operator file
    expect(await within(section).findByText("[set at deployment: operator name]", { exact: false })).toBeTruthy();
    expect(requestsTo(api, "GET /api/privacy/operator")).toHaveLength(1);

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

  it("puts the operator file's values in place of the markers, as text, read once for the session", async () => {
    const api = fakeApi({ signedIn: true });
    api.operator = {
      operator: "Test Operator",
      contact: "operator@example.org",
      controller: "Test <b>Controller</b>",
      vps_provider: "Test **VPS** Provider",
      s3_provider: "Test S3 $& Provider",
    };
    const app = renderApp("/settings#privacy-statement");
    const section = await statement();
    const operator = await within(section).findByText("Test Operator", { exact: false });
    const list = operator.closest("ul");
    expect(list?.previousElementSibling?.previousElementSibling?.id).toBe("privacy-statement-operator");
    expect([...(list?.querySelectorAll("li") ?? [])].map((li) => li.textContent)).toEqual([
      "Operator: Test Operator",
      "Contact address: operator@example.org",
      "Controller: Test <b>Controller</b>",
      "VPS provider: Test **VPS** Provider",
      "S3 backup provider: Test S3 $& Provider",
    ]);
    // the values are text: no element of theirs, no marker left, and the labels stay bold
    expect(section.querySelectorAll("b")).toHaveLength(0);
    expect(list?.querySelectorAll("strong")).toHaveLength(5);
    expect(section.textContent).not.toContain("set at deployment");
    // section 1's controller and section 10's contact still point to the Operator section
    expect(section.textContent).toContain("is named under Operator at the end of this statement");

    // read once for the session: back on the page after another, the values are not read again
    await act(async () => {
      await app.router.navigate({ to: "/" });
    });
    await act(async () => {
      await app.router.navigate({ to: "/settings", hash: "privacy-statement" });
    });
    expect(await within(await statement()).findByText("Test Operator", { exact: false })).toBeTruthy();
    expect(requestsTo(api, "GET /api/privacy/operator")).toHaveLength(1);
    // a read of the session, which a poll would not be: it keeps the session alive like any page's reads
    expect(requestsTo(api, "GET /api/privacy/operator")[0]?.headers.get("X-JH-Background")).toBeNull();
  });

  it("shows a loading note, not the markers, while the values are read", async () => {
    const api = fakeApi({ signedIn: true });
    let answer: (response: Response) => void = () => undefined;
    api.routes.set("GET /api/privacy/operator", () => new Promise<Response>((resolve) => { answer = resolve; }));
    renderApp("/settings#privacy-statement");
    const section = await statement();
    const notes = await within(section).findAllByText(en["privacy_statement.operator_loading"]);
    expect(notes).toHaveLength(5);
    expect(notes.every((note) => note.getAttribute("lang") === "en")).toBe(true);
    expect(section.textContent).not.toContain("set at deployment");
    // once answered, the values, and no note left
    await act(async () => {
      answer(json({ ...api.operator, operator: "Test Operator" }));
      await Promise.resolve();
    });
    expect(await within(section).findByText("Test Operator", { exact: false })).toBeTruthy();
    expect(within(section).queryAllByText(en["privacy_statement.operator_loading"])).toHaveLength(0);
    // the other four are null: their markers, as a server without an operator file has them
    expect(section.textContent).toContain("[set at deployment: contact address]");
  });

  it("says the values are not available when the read fails, in the UI's language", async () => {
    const api = fakeApi({ signedIn: true });
    api.uiLanguage = "de";
    api.routes.set("GET /api/privacy/operator", () => problem(503, "internal"));
    renderApp("/settings#privacy-statement", { retryDelay: 0 });
    const section = await screen.findByRole("region", { name: de["privacy_statement.title"] });
    const notes = await within(section).findAllByText(de["privacy_statement.operator_unavailable"]);
    expect(notes).toHaveLength(5);
    // the note is the UI's German, inside the English document: marked as German
    expect(notes.every((note) => note.getAttribute("lang") === "de" && note.closest("[lang=en]") !== null)).toBe(true);
    expect(section.textContent).not.toContain("set at deployment");
    expect(requestsTo(api, "GET /api/privacy/operator").length).toBeGreaterThan(1); // retried first
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
