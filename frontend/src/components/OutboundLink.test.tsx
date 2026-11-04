import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import en from "../i18n/en.json";
import { OutboundLink } from "./OutboundLink";

describe("OutboundLink", () => {
  it("links an http or https URL in a new tab with rel", () => {
    render(
      <>
        <OutboundLink href="https://boards.example.com/jobs/5?a=b">Open ad</OutboundLink>
        <OutboundLink href="HTTP://example.com/x">Plain http</OutboundLink>
      </>,
    );
    const secure = screen.getByRole("link", { name: `Open ad ${en["link.new_tab"]}` });
    expect(secure.getAttribute("href")).toBe("https://boards.example.com/jobs/5?a=b");
    expect(secure.getAttribute("target")).toBe("_blank");
    expect(secure.getAttribute("rel")).toBe("noopener noreferrer nofollow");
    expect(screen.getByRole("link", { name: `Plain http ${en["link.new_tab"]}` })).toBeTruthy();
  });

  it("renders a javascript: or data: URL, or what is no absolute URL, as plain text", () => {
    const { container } = render(
      <>
        <OutboundLink href="javascript:alert(1)">Script</OutboundLink>
        <OutboundLink href="data:text/html,<b>x</b>">Data</OutboundLink>
        <OutboundLink href="/relative/path">Relative</OutboundLink>
        <OutboundLink href="not a url">Junk</OutboundLink>
      </>,
    );
    expect(container.querySelectorAll("a")).toHaveLength(0);
    for (const text of ["Script", "Data", "Relative", "Junk"]) {
      expect(screen.getByText(text).tagName, text).toBe("SPAN");
    }
    expect(container.textContent).not.toContain(en["link.new_tab"]); // nothing opens, so nothing says so
  });
});
