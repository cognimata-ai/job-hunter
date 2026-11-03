import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ProblemError } from "../api/client";
import en from "../i18n/en.json";
import { ProblemBox } from "./ProblemBox";

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

  it("names a network failure as such", () => {
    render(<ProblemBox error={new TypeError("Failed to fetch")} />);
    expect(screen.getByRole("alert").textContent).toContain(en["problem_box.network"]);
  });
});
