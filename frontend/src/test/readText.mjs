// Reads a source file's text for a test that checks what jsdom does not apply (a CSS module's rules): vitest
// turns every `.css` import into an empty module, `?raw` included.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/**
 * The UTF-8 text of the file at `url`.
 * @param {URL} url
 * @returns {string}
 */
export function readText(url) {
  return readFileSync(fileURLToPath(url), "utf8");
}
