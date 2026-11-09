import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EvidenceHighlighter } from "./EvidenceHighlighter";

function marks(container: HTMLElement): (string | null)[] {
  return Array.from(container.querySelectorAll("mark")).map((mark) => mark.textContent);
}

function paragraphs(container: HTMLElement): (string | null)[] {
  return Array.from(container.querySelectorAll("p")).map((p) => p.textContent);
}

describe("EvidenceHighlighter", () => {
  it("highlights by code point after an emoji", () => {
    const { container } = render(<EvidenceHighlighter text="🚀 Python" lang="en" spans={[{ start: 2, end: 8 }]} />);
    expect(marks(container)).toEqual(["Python"]);
    expect(paragraphs(container)).toEqual(["🚀 Python"]);
  });

  it("overlapping spans merge", () => {
    const { container } = render(
      <EvidenceHighlighter text="FastAPI and PostgreSQL" lang="en"
        spans={[{ start: 0, end: 7 }, { start: 4, end: 11 }, { start: 12, end: 22 }]} />,
    );
    expect(marks(container)).toEqual(["FastAPI and", "PostgreSQL"]);
  });

  it("out-of-range spans are dropped", () => {
    const { container } = render(
      <EvidenceHighlighter text="Python" lang="en"
        spans={[{ start: 0, end: 7 }, { start: -1, end: 3 }, { start: 3, end: 3 }, { start: 2, end: 4 }]} />,
    );
    expect(marks(container)).toEqual(["th"]);
    expect(paragraphs(container)).toEqual(["Python"]);
  });

  it("sets lang", () => {
    const { container, rerender } = render(<EvidenceHighlighter text="Wir suchen Sie" lang="de" spans={[]} />);
    const root = container.firstElementChild;
    expect(root?.getAttribute("lang")).toBe("de");
    rerender(<EvidenceHighlighter text="Wir suchen Sie" lang={null} spans={[]} />);
    expect(container.firstElementChild?.hasAttribute("lang")).toBe(false); // the page's language applies
  });

  it("paragraphs split on single line breaks and keep offsets", () => {
    // one stored paragraph per line, joined by one "\n" (normalize/html.py), which counts as one code point
    const text = "Backend services in Python.\nWe use FastAPI and PostgreSQL.";
    const at = (word: string) => ({ start: text.indexOf(word), end: text.indexOf(word) + word.length }); // no astral code point
    const { container } = render(<EvidenceHighlighter text={text} lang="en" spans={[at("PostgreSQL")]} />);
    expect(at("PostgreSQL")).toEqual({ start: 47, end: 57 });
    expect(paragraphs(container)).toEqual(["Backend services in Python.", "We use FastAPI and PostgreSQL."]);
    const second = container.querySelectorAll("p")[1];
    expect(Array.from(second.querySelectorAll("mark")).map((m) => m.textContent)).toEqual(["PostgreSQL"]);
    expect(container.querySelectorAll("p")[0].querySelector("mark")).toBeNull();
  });

  it("an astral character in an earlier paragraph shifts no later span", () => {
    // "👋🏽" is two code points and four UTF-16 units: a UTF-16 offset would land two units early
    const text = "Hallo 👋🏽 Team.\nWir nutzen Python.";
    const start = Array.from(text).indexOf("P");
    expect(start).toBe(26);
    const { container } = render(<EvidenceHighlighter text={text} lang="de" spans={[{ start, end: start + 6 }]} />);
    expect(marks(container)).toEqual(["Python"]);
    expect(container.querySelectorAll("p")[1].querySelector("mark")?.textContent).toBe("Python");
  });

  it("a span across a line break is marked in each paragraph it touches", () => {
    const text = "in Python.\nWe use";
    const { container } = render(<EvidenceHighlighter text={text} lang="en" spans={[{ start: 3, end: 13 }]} />);
    expect(marks(container)).toEqual(["Python.", "We"]);
    expect(paragraphs(container)).toEqual(["in Python.", "We use"]);
  });

  it("renders markup as text", () => {
    const text = '<img src=x onerror="alert(1)"><b>Python</b>';
    const { container } = render(<EvidenceHighlighter text={text} lang="en" spans={[{ start: 33, end: 39 }]} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    expect(paragraphs(container)).toEqual([text]);
    expect(marks(container)).toEqual(["Python"]);
  });
});
