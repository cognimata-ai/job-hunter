import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { components } from "../api/schema";
import en from "../i18n/en.json";
import { renderInProviders } from "../test/app";
import { ContestedTab } from "./ContestedTab";

type ContestedOut = components["schemas"]["ContestedOut"];

const CONTESTED: ContestedOut = {
  rounds: [
    {
      round: 1,
      triggers: ["T2", "T3"],
      verdict: "revise",
      outcome: "revise",
      issues: [
        { criterion: "skills", issue: "level_too_high", suggested_level: 2 },
        { criterion: "mandatory_language:de", issue: "fact_rejected", suggested_level: null },
      ],
    },
    { round: 2, triggers: ["T0"], verdict: "revise", outcome: "incomplete", issues: [] },
  ],
  rejected_drafts: [
    {
      round: 0,
      level_diff: [
        { criterion: "skills", from_level: 3, to_level: 2 },
        { criterion: "domain", from_level: 2, to_level: null },
      ],
    },
    { round: 1, level_diff: [] },
  ],
  nulled_criteria: ["domain"],
};

function section(name: string): HTMLElement {
  return screen.getByRole("region", { name });
}

describe("ContestedTab", () => {
  it("shows each rejected draft as a level diff against the final draft", () => {
    renderInProviders(<ContestedTab contested={CONTESTED} />);
    const drafts = section(en["contested.rejected"]);
    const [first, second] = within(drafts).getAllByRole("listitem", { name: /./ });
    expect(within(first).getByText(en["contested.draft"].replace("{{round}}", "0"))).toBeTruthy();
    expect(within(first).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      `${en["criterion.skills"]}: 3 → 2`,
      `${en["criterion.domain"]}: 2 → —`,
    ]);
    expect(within(second).getByText(en["contested.same_levels"])).toBeTruthy();
  });

  it("shows the rounds in order with their triggers, verdict, outcome and issues per criterion", () => {
    renderInProviders(<ContestedTab contested={CONTESTED} />);
    const rounds = section(en["contested.rounds"]);
    const [one, two] = within(rounds).getAllByRole("article");
    expect(within(one).getByRole("heading").textContent).toBe(en["contested.round"].replace("{{round}}", "1"));
    expect(one.textContent).toContain(`${en["critic_trigger.T2"]}, ${en["critic_trigger.T3"]}`);
    expect(one.textContent).toContain(en["critic_verdict.revise"]);
    expect(one.textContent).toContain(en["round_outcome.revise"]);
    expect(within(one).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      `${en["criterion.skills"]}: ${en["critic_issue.level_too_high"]} (${en["contested.suggested"].replace("{{level}}", "2")})`,
      `mandatory_language:de: ${en["critic_issue.fact_rejected"]}`,
    ]);
    expect(within(one).getByText("mandatory_language:de").tagName).toBe("CODE"); // a fact id, verbatim
    expect(two.textContent).toContain(en["round_outcome.incomplete"]);
    expect(within(two).getByText(en["contested.no_issues"])).toBeTruthy();
  });

  it("lists the nulled criteria", () => {
    renderInProviders(<ContestedTab contested={CONTESTED} />);
    expect(within(section(en["contested.nulled"])).getByText(en["criterion.domain"])).toBeTruthy();
  });

  it("an uncontested result says so", () => {
    renderInProviders(<ContestedTab contested={null} />);
    expect(screen.getByText(en["contested.none"])).toBeTruthy();
    expect(screen.queryByRole("region", { name: en["contested.rounds"] })).toBeNull();
  });
});
