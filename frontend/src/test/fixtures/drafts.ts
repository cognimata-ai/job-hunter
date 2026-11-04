// The CV section's drafts (plan M4 Task 27, resolution i, Ruling T27-d).
//
// The text-review drafts are the server's real answers: `drafts.json` holds, per M2 text-review shape, a
// synthetic raw CV and the states its review goes through, each the edit set the client sends and what
// `PUT …/text` then `GET /drafts/{id}` answer (the stripped text, the strip counts, the stored edit set and the
// review map). `tests/unit/test_cv_fixture_drafts.py` rebuilds every state through the backend's own functions,
// so a change of the stripping rules fails there. This module gives the tests a state's `DraftOut` parts and the
// edit set that reaches it, never the raw text.
//
// The other drafts (a ready profile with its issues, a failed draft, a schema mismatch) are written here.
import type { components } from "../../api/schema";
import fixtures from "./drafts.json";

type DraftOut = components["schemas"]["DraftOut"];
type SegmentOut = components["schemas"]["SegmentOut"];
type SpanEditIn = components["schemas"]["SpanEditIn"];
type TextEditsOut = components["schemas"]["TextEditsOut"];
type ConfirmedProfile = components["schemas"]["ConfirmedProfile"];
type ValidationIssueOut = components["schemas"]["ValidationIssueOut"];
type RescoreEstimateOut = components["schemas"]["RescoreEstimateOut"];

interface ReviewState {
  span_edits: SpanEditIn[] | null;
  stripped_text_preview: string;
  strip_report: Record<string, number>;
  edits: TextEditsOut;
  segments: SegmentOut[];
}

/** A text-review shape of `drafts.json`. */
export type ReviewShape = keyof typeof fixtures;

const STATES = Object.fromEntries(
  Object.entries(fixtures).map(([shape, fixture]) => [shape, fixture.states as unknown as ReviewState[]]),
) as Record<ReviewShape, ReviewState[]>;

/** A draft as `GET /api/profile/drafts/{id}` answers it: in `text_review`, with nothing stripped. */
export function draftOut(overrides: Partial<DraftOut> = {}): DraftOut {
  return {
    status: "text_review",
    draft: null,
    validation_issues: [],
    strip_report: null,
    stripped_text_preview: null,
    reviewed_text_sha256: null,
    failure_code: null,
    repairs: [],
    edits: { restored: [], removed: [] },
    segments: [],
    recomputed_total_years: null,
    ...overrides,
  };
}

/** State `index` of the review of `shape`, in `text_review`. */
export function reviewDraft(shape: ReviewShape, index = 0, overrides: Partial<DraftOut> = {}): DraftOut {
  const state = STATES[shape][index];
  return draftOut({
    stripped_text_preview: state.stripped_text_preview,
    strip_report: state.strip_report,
    edits: state.edits,
    segments: state.segments,
    ...overrides,
  });
}

/** The complete edit set the client sends to reach state `index` of `shape` (`PUT …/text {span_edits}`). */
export function sentEdits(shape: ReviewShape, index: number): SpanEditIn[] {
  const edits = STATES[shape][index].span_edits;
  if (edits === null) {
    throw new Error(`state ${String(index)} of ${shape} is a stored set, which no request sends`);
  }
  return edits;
}

/** A synthetic profile (Max Mustermann's): a senior backend engineer with two roles, two skills, two languages. */
export function profile(overrides: Partial<ConfirmedProfile> = {}): ConfirmedProfile {
  return {
    summary: "Backend engineer building Python services.",
    target_titles: [
      { title: "Backend Engineer", lang: "en" },
      { title: "Softwareentwickler Backend", lang: "de" },
    ],
    seniority: "senior",
    total_years_experience: 6.5,
    roles: [
      {
        title: "Senior Backend Engineer",
        employer: "Beispiel GmbH",
        start: "2021-03-01",
        end: "present",
        seniority: "senior",
        domain: "fintech",
        highlights: ["Built the payment API"],
      },
      {
        title: "Backend Developer",
        employer: null,
        start: "2018-01-01",
        end: "2021-02-01",
        seniority: "mid",
        domain: null,
        highlights: [],
      },
    ],
    skills: [
      { name: "Python", category: "programming_language", level: "expert", years: 6, evidence: "Python seit 2018" },
      { name: "PostgreSQL", category: "tool", level: "advanced", years: null, evidence: "PostgreSQL" },
    ],
    languages: [
      { code: "de", code_other: null, cefr: "native", evidence: "Muttersprache Deutsch" },
      { code: "en", code_other: null, cefr: "C1", evidence: "Business English" },
    ],
    education: [{ level: "master", field: "Informatik", institution: "Beispiel-Universität", year: 2017 }],
    certifications: [],
    domains: ["fintech"],
    user_entered: { work_authorization_de: "unspecified", security_clearances: [] },
    ...overrides,
  };
}

/** A ready draft of `profile()`, with its issues. */
export function readyDraft(issues: ValidationIssueOut[] = [], overrides: Partial<DraftOut> = {}): DraftOut {
  return reviewDraft("hyphen_phone", 0, {
    status: "ready",
    draft: profile(),
    validation_issues: issues,
    reviewed_text_sha256: "ab".repeat(32),
    recomputed_total_years: 6.4,
    ...overrides,
  });
}

/** A failed draft: its code and the repairs the server offers, in its order. */
export function failedDraft(failureCode: string, repairs: DraftOut["repairs"]): DraftOut {
  return draftOut({ status: "failed", failure_code: failureCode, repairs });
}

/** §15.7's confirm modal example: profile v5, 1,180 ads, $22.44 at 40 %, 211 in a batch, ~969 online at 53 a
 * day, $38.20 of $100 this month. */
export function rescoreEstimate(overrides: Partial<RescoreEstimateOut> = {}): RescoreEstimateOut {
  return {
    targets: 1180,
    est_usd_online: "62.120000",
    est_usd_batch: "31.060000",
    month_spend: "38.200000",
    cap: "100.00",
    bulk_budget_usd: "22.440000",
    bulk_share: "0.40",
    batch_targets: 211,
    online_targets: 969,
    online_per_day: 53,
    next_version_no: 5,
    ...overrides,
  };
}
