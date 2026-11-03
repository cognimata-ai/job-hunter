import { describe, expect, it } from "vitest";
import { safeRedirect } from "./session";

describe("safeRedirect", () => {
  it("safeRedirect accepts same-origin paths only", () => {
    for (const hostile of [
      "//evil.example",
      "//evil.example/settings",
      "https://evil.example",
      "http://localhost:8080/settings",
      "javascript:alert(1)",
      "/\\evil.example",
      "\\\\evil.example",
      "/\t/evil.example",
      "settings",
      "",
      undefined,
    ]) {
      expect(safeRedirect(hostile), String(hostile)).toBe("/");
    }
  });

  it("never sends a sign-in back to the login page", () => {
    for (const login of ["/login", "/login?redirect=%2Fsettings", "/login#x", "/login/"]) {
      expect(safeRedirect(login), login).toBe("/");
    }
  });

  it("keeps a same-origin path with its search and hash", () => {
    expect(safeRedirect("/")).toBe("/");
    expect(safeRedirect("/settings#budget")).toBe("/settings#budget");
    expect(safeRedirect("/?sort=posted&source=lever&source=ashby")).toBe("/?sort=posted&source=lever&source=ashby");
    expect(safeRedirect("/loginhelp")).toBe("/loginhelp");
  });
});
