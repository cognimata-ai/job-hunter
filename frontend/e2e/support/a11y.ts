// The accessibility guard (plan M4 Task 32, R3, §15.9): axe-core over the page (or a part of it) with the WCAG
// 2.0 A and AA, 2.1 AA and 2.2 AA rules. Every spec calls it on every page and drawer state it visits.
import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

export const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"];
const BLOCKING = new Set(["serious", "critical"]);

/** Fails on any `serious` or `critical` axe violation of the page (or of `include`, a CSS selector), listing
 * each rule with its impact and the selectors of its nodes. */
export async function expectNoSeriousA11yViolations(page: Page, include?: string): Promise<void> {
  let builder = new AxeBuilder({ page }).withTags(AXE_TAGS);
  if (include !== undefined) {
    builder = builder.include(include);
  }
  const { violations } = await builder.analyze();
  const blocking = violations
    .filter((violation) => BLOCKING.has(violation.impact ?? ""))
    .map(
      (violation) =>
        `${violation.id} (${violation.impact ?? "?"}): ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
    );
  expect(blocking, `serious or critical axe violations on ${new URL(page.url()).pathname}`).toEqual([]);
}
