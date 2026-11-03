import { describe, expect, it } from "vitest";
import { checkDist, findViolations } from "../../scripts/check-dist-csp.mjs";

const GOOD_INDEX = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <title>Job Hunter</title>
    <script type="module" crossorigin src="/assets/index-a1b2.js"></script>
    <link rel="modulepreload" crossorigin href="/assets/vendor-c3d4.js">
    <link rel="stylesheet" crossorigin href="/assets/index-e5f6.css">
  </head>
  <body>
    <div id="root"></div>
  </body>
</html>`;
// A committed build the CSP would refuse: an inline script and a style attribute in its index.html, and a
// data: font in a stylesheet two directories down (only .html and .css, so neither ESLint nor tsc reads it).
// Through a variable: Vite rewrites the literal `new URL("…", import.meta.url)` as a served asset's URL.
const HERE = import.meta.url;
const BAD_DIST = new URL("./fixtures/bad-dist/", HERE);

function check(index: string, more: { path: string; text: string }[] = []) {
  return findViolations([{ path: "index.html", text: index }, ...more]);
}

describe("check-dist-csp", () => {
  it("passes a build with only external scripts and stylesheets", () => {
    expect(check(GOOD_INDEX, [{ path: "assets/index-e5f6.css", text: "body{margin:0}" }])).toEqual([]);
  });

  it.each([
    ["an inline script", "<script>window.x = 1</script>"],
    ["an inline module script", '<script type="module">import "/a.js"</script>'],
    ["a style element", "<style>body{color:red}</style>"],
    ["a style attribute", '<div style="color:red"></div>'],
    ["an inline event handler", '<img src="/a.png" onerror="alert(1)">'],
    ["an inline event handler in upper case", '<body ONLOAD="go()">'],
  ])("fails %s", (_name, markup) => {
    const violations = check(GOOD_INDEX.replace('<div id="root"></div>', markup));
    expect(violations).toHaveLength(1);
    expect(violations[0]).toMatch(/^index\.html: /);
  });

  it.each([
    ["a data: font in a stylesheet", "assets/index.css",
      '@font-face{font-family:x;src:url(data:font/woff2;base64,d09GMgAB) format("woff2")}'],
    ["a quoted data: font", "assets/index.css", "@font-face{src:url('data:application/font-woff;base64,AAAA')}"],
    ["a data: font in a script", "assets/index.js", 'const f="data:font/ttf;base64,AAEAAA"'],
  ])("fails %s", (_name, path, text) => {
    expect(check(GOOD_INDEX, [{ path, text }])).toEqual([expect.stringMatching(new RegExp(`^${path}: `))]);
  });

  it("allows a data: image (img-src allows data:)", () => {
    expect(check(GOOD_INDEX, [{ path: "assets/a.css", text: ".x{background:url(data:image/png;base64,iVBOR)}" }]))
      .toEqual([]);
  });

  describe("checkDist", () => {
    it("walks the build's directories and reports each violation, the nested stylesheet's included", () => {
      const violations = checkDist(BAD_DIST);
      expect(violations).toHaveLength(3);
      expect(violations).toEqual(expect.arrayContaining([
        expect.stringMatching(/^index\.html: an inline <script>/),
        expect.stringMatching(/^index\.html: a style attribute on <div>/),
        "assets/nested/x.css: a data: font (font-src 'self')",
      ]));
    });

    it("reports a build without index.html as a violation, not a crash", () => {
      expect(checkDist(new URL("./assets/", BAD_DIST))).toEqual([
        "index.html: missing (no build to serve)",
        "nested/x.css: a data: font (font-src 'self')",
      ]);
      expect(checkDist(new URL("./no-such-build/", BAD_DIST))).toEqual(["index.html: missing (no build to serve)"]);
    });
  });
});
