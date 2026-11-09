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

  it("never sends a sign-in back to the login page, in any case (the router matches paths so)", () => {
    for (const login of [
      "/login",
      "/login?redirect=%2Fsettings",
      "/login#x",
      "/login/",
      "/LOGIN",
      "/Login?x",
      "/LOGIN/",
      "/%4Cogin", // decodes to /Login
    ]) {
      expect(safeRedirect(login), login).toBe("/");
    }
  });

  it("refuses a path whose decoded form would leave the origin", () => {
    // `/%2F%2Fevil.example` is a path of this origin as it stands, but the router decodes it to
    // `//evil.example`, a protocol-relative URL: refused, `/`
    for (const hostile of ["/%2F%2Fevil.example", "/%2f%2fevil.example/settings", "/\\evil", "/%5Cevil", "/%E0%A4%A"]) {
      expect(safeRedirect(hostile), hostile).toBe("/");
    }
  });

  it("keeps a same-origin path with its search and hash", () => {
    expect(safeRedirect("/")).toBe("/");
    expect(safeRedirect("/settings#budget")).toBe("/settings#budget");
    expect(safeRedirect("/?sort=posted&source=lever&source=ashby")).toBe("/?sort=posted&source=lever&source=ashby");
    expect(safeRedirect("/loginhelp")).toBe("/loginhelp");
  });
});
