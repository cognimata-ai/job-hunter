import { screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import de from "../i18n/de.json";
import en from "../i18n/en.json";
import i18n from "../i18n/index";
import { detailScore } from "../test/api";
import { renderInProviders } from "../test/app";
import { CriteriaTable } from "./CriteriaTable";

afterEach(async () => {
  await i18n.changeLanguage("en");
});

function table(): HTMLElement {
  return screen.getByRole("table", { name: en["drawer.criteria"] });
}

/** The cells of each body row, as text: the criterion (a row header), weight, level, basis and evidence. */
function rows(): string[][] {
  return within(table())
    .getAllByRole("row")
    .slice(1)
    .map((row) => Array.from(row.querySelectorAll("th, td")).map((cell) => cell.textContent));
}

describe("CriteriaTable", () => {
  it("lists the six LLM criteria, then the two deterministic ones, with their weights, levels and basis", () => {
    renderInProviders(<CriteriaTable score={detailScore()} />);
    const headers = within(table()).getAllByRole("columnheader").map((cell) => cell.textContent);
    expect(headers).toEqual([en["drawer.col.criterion"], en["drawer.col.weight"], en["drawer.col.level"], en["drawer.col.basis"], en["drawer.col.evidence"]]);
    expect(rows().map((cells) => cells.slice(0, 4))).toEqual([
      [en["criterion.role_fit"], "20", "4", en["basis.observed"]],
      [en["criterion.skills"], "25", "3", en["basis.observed"]],
      [en["criterion.experience_seniority"], "15", "3", en["basis.observed"]],
      [en["criterion.domain"], "10", "—", en["basis.unknown"]],
      [en["criterion.languages"], "10", "4", en["basis.observed"]],
      [en["criterion.education_certifications"], "5", "4", en["basis.unconstrained"]],
      [en["criterion.location_work_mode"], "10", "4", en["basis.observed"]],
      [en["criterion.employment_salary"], "5", "4", en["basis.observed"]],
    ]);
  });

  it("a weight or a level the result lacks reads —", () => {
    const score = detailScore();
    score.criteria[0] = { ...score.criteria[0], weight: null, level: null, basis: "unknown" };
    renderInProviders(<CriteriaTable score={score} />);
    expect(rows()[0].slice(0, 4)).toEqual([en["criterion.role_fit"], "—", "—", en["basis.unknown"]]);
  });

  it("quotes the evidence, the profile side and a structured field after ⇄", () => {
    const score = detailScore();
    score.criteria[4] = {
      ...score.criteria[4],
      evidence: [
        { subject: "languages", source_kind: "structured_field", field_name: "employment_types", quote: "Employment types: full_time", span_start: null, span_end: null },
      ],
    };
    renderInProviders(<CriteriaTable score={score} />);
    const [roleFit, skills, , domain, languages] = rows();
    expect(roleFit[4]).toBe(`“Backend services”“Backend Engineer” ⇄ ${en["drawer.side.profile"]} ${en["profile_section.roles"]}`);
    expect(skills[4]).toBe("“FastAPI”");
    expect(domain[4]).toBe("—");
    expect(languages[4]).toBe(`“Employment types: full_time” ⇄ ${en["drawer.side.structured"]} employment_types`);
    // the structured field's name is the server's, verbatim
    expect(within(table()).getByText("employment_types").tagName).toBe("CODE");
  });

  it("a deterministic criterion shows its inputs as key: value pairs", () => {
    const score = detailScore();
    score.det_criteria[0] = {
      ...score.det_criteria[0],
      level: null,
      basis: "unknown",
      inputs: { compat: { hybrid: 4, remote: null }, hybrid_band: 4, modes: ["hybrid", "remote"], known: false },
    };
    renderInProviders(<CriteriaTable score={score} />);
    const [, , , , , , location, employment] = rows();
    expect(location.slice(2, 4)).toEqual(["—", en["basis.unknown"]]);
    const inputs = within(within(table()).getAllByRole("row")[7]).getAllByRole("listitem").map((li) => li.textContent);
    expect(inputs).toEqual(["compat: hybrid 4, remote —", "hybrid_band: 4", "modes: hybrid, remote", "known: false"]);
    expect(employment[4]).toBe("et: 4sal: 4");
  });

  it("marks a corrected and a shortened criterion", () => {
    const score = detailScore({
      corrected_fields: ["criteria.skills.level"],
      shortened_fields: ["criteria.role_fit.ad_evidence", "summary_en"],
    });
    renderInProviders(<CriteriaTable score={score} />);
    const [roleFit, skills, seniority] = rows();
    expect(roleFit[0]).toBe(`${en["criterion.role_fit"]} ${en["badge.shortened"]}`);
    expect(skills[0]).toBe(`${en["criterion.skills"]} ${en["drawer.corrected"]}`);
    expect(seniority[0]).toBe(en["criterion.experience_seniority"]);
  });

  it("names the criteria and the basis in German", async () => {
    await i18n.changeLanguage("de");
    renderInProviders(<CriteriaTable score={detailScore()} />);
    expect(screen.getByRole("table", { name: de["drawer.criteria"] })).toBeTruthy();
    expect(screen.getByText(de["criterion.location_work_mode"])).toBeTruthy();
    expect(screen.getAllByText(de["basis.observed"]).length).toBeGreaterThan(0);
  });
});
