import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProblemError } from "../api/client";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { ProblemBox } from "./ProblemBox";

afterEach(async () => {
  await i18n.changeLanguage("en");
});

describe("ProblemBox", () => {
  it("shows the translated title, the detail as text and the request id, and retries", async () => {
    const onRetry = vi.fn();
    const error = new ProblemError({
      status: 409,
      code: "stale_base_version",
      title: "Base version is stale (server English)",
      detail: "<b>version 12</b> is active",
      requestId: "req-5f2c",
      errors: [],
    });
    render(<ProblemBox error={error} onRetry={onRetry} />);
    const box = screen.getByRole("alert");
    expect(box.textContent).toContain(en["problem.stale_base_version"]);
    expect(box.textContent).not.toContain("server English");
    expect(screen.getByText("<b>version 12</b> is active")).toBeTruthy();
    expect(box.querySelector("b")).toBeNull();
    expect(box.textContent).toContain("req-5f2c");
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("falls back to the server's title for a code the catalogue lacks, and has no Retry without onRetry", () => {
    const error = new ProblemError({ status: 418, code: "teapot", title: "I'm a teapot", requestId: null, errors: [] });
    render(<ProblemBox error={error} />);
    expect(screen.getByRole("alert").textContent).toContain("I'm a teapot");
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("in German shows a known code's translation only, never the server's English detail (§15.9)", async () => {
    await i18n.changeLanguage("de");
    const error = new ProblemError({
      status: 409,
      code: "conflict_state",
      title: "Conflict (server English)",
      detail: "the jobs changed while this request was being applied; send it again",
      requestId: "req-5f2c",
      errors: [],
    });
    render(<ProblemBox error={error} />);
    const box = screen.getByRole("alert");
    expect(box.textContent).toContain(de["problem.conflict_state"]);
    expect(box.textContent).not.toContain("server English");
    expect(box.textContent).not.toContain("the jobs changed");
    expect(box.textContent).toContain("req-5f2c"); // an id stays as it is
  });

  it("shows a code-shaped detail verbatim as code in every language, and hides a prose one in German", async () => {
    const refused = (detail: string) =>
      new ProblemError({ status: 409, code: "conflict_state", title: "Conflict", detail, requestId: null, errors: [] });
    for (const language of ["en", "de"]) {
      await i18n.changeLanguage(language);
      const { unmount } = render(<ProblemBox error={refused("not_releasable")} />);
      const box = screen.getByRole("alert");
      expect(box.textContent).toContain(language === "de" ? de["problem.conflict_state"] : en["problem.conflict_state"]);
      expect(within(box).getByText("not_releasable", { selector: "code" })).toBeTruthy();
      unmount();
    }
    render(<ProblemBox error={refused("the batch was reconciled meanwhile")} />); // German, prose
    expect(screen.getByRole("alert").textContent).not.toContain("reconciled");
  });

  it("in German falls back to the server's English title and detail for a code the catalogue lacks", async () => {
    await i18n.changeLanguage("de");
    const error = new ProblemError({
      status: 418, code: "teapot", title: "I'm a teapot", detail: "Short and stout", requestId: null, errors: [],
    });
    render(<ProblemBox error={error} />);
    const box = screen.getByRole("alert");
    expect(box.textContent).toContain("I'm a teapot");
    expect(screen.getByText("Short and stout")).toBeTruthy();
  });

  it("takes a caller's own title and note, and with codeDetailOnly never a prose detail", () => {
    const refused = (detail: string) =>
      new ProblemError({ status: 418, code: "teapot", title: "I'm a teapot", detail, requestId: "req-1", errors: [] });
    const { unmount } = render(<ProblemBox error={refused("Short and stout")} title="Our own words" note="Try again" codeDetailOnly />);
    const box = screen.getByRole("alert");
    expect(box.textContent).toContain("Our own words");
    expect(box.textContent).not.toContain("teapot");
    expect(box.textContent).not.toContain("Short and stout"); // English UI, prose detail: not shown
    expect(box.textContent).toContain("Try again");
    expect(box.textContent).toContain("req-1");
    unmount();
    render(<ProblemBox error={refused("eval_run_running")} title="Our own words" codeDetailOnly />);
    expect(within(screen.getByRole("alert")).getByText("eval_run_running", { selector: "code" })).toBeTruthy();
  });

  it("names a network failure as such", () => {
    render(<ProblemBox error={new TypeError("Failed to fetch")} />);
    expect(screen.getByRole("alert").textContent).toContain(en["problem_box.network"]);
  });
});
