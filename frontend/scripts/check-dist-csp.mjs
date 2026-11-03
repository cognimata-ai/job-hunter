// The build's CSP check (spec §16.6: "The SPA build emits no inline scripts or styles"), run by
// `npm run build` after `vite build`. The api serves `dist/` under `script-src 'self'; style-src 'self';
// font-src 'self'`, so the build fails on what that policy would refuse: in an HTML file, a <script> without
// `src` (or with a body), a <style> element, a `style` attribute or an inline event handler (`on…=`); in any
// built text file, a `data:` URL of a font.
//
//   node scripts/check-dist-csp.mjs [dist-directory]   (default: ../dist from this file)
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const TEXT_FILE = /\.(html|css|js|mjs|json|svg|txt|map)$/i;
const TAG = /<([a-z][\w-]*)\b([^>]*)>/gi;
const SCRIPT = /<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi;
const STYLE_ELEMENT = /<style\b/i;
const SRC_ATTRIBUTE = /\ssrc\s*=/i;
const STYLE_ATTRIBUTE = /\sstyle\s*=/i;
const EVENT_HANDLER = /\son[a-z]+\s*=/i;
const DATA_FONT = /data:\s*(font\/|application\/(x-)?font|application\/vnd\.ms-fontobject)/i;

/**
 * The CSP violations of a build's files.
 * @param {{ path: string, text: string }[]} files the built files, each with its path relative to `dist/`
 * @returns {string[]} one line per violation, `<path>: <what>`
 */
export function findViolations(files) {
  /** @type {string[]} */
  const found = [];
  for (const { path, text } of files) {
    if (/\.html$/i.test(path)) {
      for (const [, attributes = "", body = ""] of text.matchAll(SCRIPT)) {
        if (!SRC_ATTRIBUTE.test(attributes) || body.trim() !== "") {
          found.push(`${path}: an inline <script> (script-src 'self' needs src= and no body)`);
        }
      }
      if (STYLE_ELEMENT.test(text)) {
        found.push(`${path}: a <style> element (style-src 'self')`);
      }
      for (const [tag, name = "", attributes = ""] of text.matchAll(TAG)) {
        if (STYLE_ATTRIBUTE.test(attributes)) {
          found.push(`${path}: a style attribute on <${name}> (style-src 'self'): ${tag}`);
        }
        if (EVENT_HANDLER.test(attributes)) {
          found.push(`${path}: an inline event handler on <${name}> (script-src 'self'): ${tag}`);
        }
      }
    }
    if (DATA_FONT.test(text)) {
      found.push(`${path}: a data: font (font-src 'self')`);
    }
  }
  return found;
}

/**
 * Every text file under `dir`, with its path relative to it.
 * @param {string} dir
 * @returns {{ path: string, text: string }[]}
 */
function readBuild(dir) {
  return readdirSync(dir, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && TEXT_FILE.test(entry.name))
    .map((entry) => {
      const full = join(entry.parentPath, entry.name);
      return { path: relative(dir, full), text: readFileSync(full, "utf8") };
    });
}

function main() {
  const dir = process.argv[2] ?? fileURLToPath(new URL("../dist/", import.meta.url));
  const files = readBuild(dir);
  if (!files.some((file) => file.path === "index.html")) {
    console.error(`check-dist-csp: no index.html in ${dir}`);
    process.exitCode = 1;
    return;
  }
  const violations = findViolations(files);
  for (const violation of violations) {
    console.error(`check-dist-csp: ${violation}`);
  }
  if (violations.length > 0) {
    process.exitCode = 1;
    return;
  }
  console.log(`check-dist-csp: ${String(files.length)} files, no inline script, style or data: font`);
}

if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main();
}
