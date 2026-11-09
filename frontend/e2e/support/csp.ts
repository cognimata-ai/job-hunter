// The CSP guard (plan M4 Task 32, R1): an automatic fixture that records, in every page of the test's browser
// context, each `securitypolicyviolation` event (through an init script and a binding) and each console message
// of a CSP refusal, and fails the test at its teardown when any was recorded. The api serves the SPA under a
// strict policy (§16.6): no inline script or style, so a violation is a defect of the build or of a component.
import { test as base, expect } from "@playwright/test";

/** A console message of a refusal by the Content Security Policy. */
export const CSP_CONSOLE = /Content Security Policy|Refused to (apply|execute|load)/;
const BINDING = "__jhCspViolation";

export interface CspFixtures {
  cspGuard: undefined;
}

export const test = base.extend<CspFixtures>({
  cspGuard: [
    async ({ context }, use) => {
      const recorded: string[] = [];
      await context.exposeBinding(BINDING, (_source, report: unknown) => {
        recorded.push(`securitypolicyviolation: ${String(report)}`);
      });
      await context.addInitScript((binding: string) => {
        document.addEventListener(
          "securitypolicyviolation",
          (event) => {
            const send = (window as unknown as Record<string, ((report: string) => void) | undefined>)[binding];
            send?.(`${event.violatedDirective} blocked ${event.blockedURI} at ${event.sourceFile}:${String(event.lineNumber)}`);
          },
          true,
        );
      }, BINDING);
      context.on("console", (message) => {
        if (CSP_CONSOLE.test(message.text())) {
          recorded.push(`console ${message.type()}: ${message.text()}`);
        }
      });
      await use(undefined);
      expect(recorded, "Content Security Policy violations").toEqual([]);
    },
    { auto: true },
  ],
});
