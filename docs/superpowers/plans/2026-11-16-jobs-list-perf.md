# Jobs List Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring every page of `tests/perf/test_jobs_list_perf.py` under the spec's 300 ms p95 at 50,000 cards, so the roadmap's M4 and M5 rows can be marked complete. The budget is never relaxed.

**Architecture:** Two query-level fixes in `backend/src/jobhunter/jobs/`, both within the list's read snapshot:
1. The M5 purge-fence conjunct is added only when a fence is open.
2. The keyword and radius pages stop repeating their costly per-row work.

Results stay identical: every existing list, facets and fence test stays green unchanged.

**Tech Stack:** Python 3.13, SQLAlchemy Core (async, psycopg), PostgreSQL 17 with `pg_trgm`.

**Spec:** `docs/superpowers/specs/2026-10-16-job-hunter-design.md`, revision 3.7. It sets the target: "p95 < 300 ms per list page and < 200 ms per detail at 50,000 jobs". The M4 plan's B29 and the M5 plan's D21 are the designs these fixes touch.

## Findings this plan starts from

These were measured on 2026-11-16 on the session machine, with the perf test alone, `REPEATS` 20:

| page | M4 merge `f39ef5d` | `main` `23b7337` |
|---|---|---|
| `keyword_entwickler` | 1,056 ms | 1,646 ms |
| `berlin_30_km` | 362 ms | 3,376 ms |
| every other page, and facets | under 300 ms | under 300 ms |

**Root cause 1, the M5 regression.**
- The only SQL difference between the two commits is M5's `NOT_FENCED` conjunct (`jobs/_sql.py`, D21):
  ```
  (NOT EXISTS (SELECT 1 FROM purge_fences f WHERE f.released_at IS NULL) OR NOT is_fenced(j.id))
  ```
- `is_fenced` is a `LANGUAGE sql STABLE` function the planner cannot see into. In `jobs_list.count_unscored`, the conjunct skews the row estimates.
- The planner then joins `user_job_actions` by a sequential scan per outer row, instead of probing `user_job_actions_pkey`. On the Berlin page that is 7,837 scans and 52.8 M rows removed by the join filter, 8.3 s under `EXPLAIN ANALYZE`. M4's plan for the same statement probes the pkey.

**Root cause 2, already in M4.**
- The keyword filter's title branch, `TITLE_JOBS` (`:qf <% kt.title_norm`), runs as a sequential scan of `jobs` that computes word similarity for every title. That costs about 130–176 ms per evaluation.
- It is evaluated in every statement of a first page: `count`, `count_unscored`, each page segment, and the deferred-member InitPlan.
- The Berlin page was already just over budget at M4, 362 ms.

## Global Constraints

- **The budget.** p95 < 300 ms per list page at 50,000 cards, measured by `tests/perf/test_jobs_list_perf.py` exactly as it stands. Never change `P95_LIMIT_S`, `REPEATS`, `CARDS` or the requests.
- **Results unchanged.** Every list, facets, cursor and fence test stays green without edits to its assertions. A fenced job is still never a card.
- **Snapshot.** The list and facets routes read in one read-only `REPEATABLE READ` snapshot (`db.snapshot.read_snapshot`). Anything read once per request must be read on that connection, so it agrees with every statement of the request.
- **Schema changes.** Prefer query changes. A new index is acceptable only as an expand-only migration `0013`, under the compatibility-floor rule (`db/migrate.py`, spec §19.6), with the floor unchanged.
- **The usual M5 constraints apply:**
  - `env -u` of the six real-key variables;
  - no paid calls;
  - never read `.secrets/` or `.fixtures-raw/`;
  - gitleaks before any push;
  - commit scope `m5`.

## Review Focus

1. **A fence opens while a page is being read.** Expected: the page agrees with its snapshot. A fence committed before the snapshot hides the job; one committed after does not, exactly as today's per-statement InitPlan does within the same snapshot.
2. **A source or global fence is open.** Every fenced job is absent from the cards, the counts and the facets.
3. **A keyword that matches many titles, and one that matches none.** Same results as today, and the request stays within budget.
4. **A deferred duplicate matches the keyword or the radius while its representative does not.** It still surfaces its group card as today, B2.
5. **The perf numbers are measured with nothing else running on the machine.**

---

### Task P1: The fence conjunct only when a fence is open

**Files**
- Modify:
  - `backend/src/jobhunter/jobs/listing.py`: `_filter`, `select_page`, and the facets path through `card_set`;
  - `backend/src/jobhunter/jobs/_sql.py`: the `NOT_FENCED` comment and module docstring;
  - `backend/src/jobhunter/privacy/fences.py`: a reader `any_fence_open(conn) -> bool`, built on `NO_OPEN_FENCE_SQL`.
- Tests:
  - `tests/integration/test_jobs_listing.py` or the file that holds the list's fence tests;
  - a unit test of the rendered filter.

**Produces**
- **One read per request.** `any_fence_open(conn: AsyncConnection) -> bool` is read once per list request and once per facets request, on the snapshot connection.
- **The filter.** `_filter` takes the result.
  - With no open fence, the rendered SQL holds no `is_fenced` and no `purge_fences` reference. It is byte-identical to M4's for the same query.
  - With one open, it holds `NOT is_fenced(j.id)` alone. The `NO_OPEN_FENCE_SQL OR` prefix is redundant then: the snapshot already showed a fence.
- **Out of scope.** The export's own `_NOT_FENCED` (`privacy/export.py`) is not on a perf path and stays as it is.
- **Docs.** The M5 plan's D21 wording is history. This plan's text and the `_sql.py` docstring describe the new rule.

**Steps**
- [ ] **Failing tests.**
  - The filter rendered with `fence_open=False` holds neither `is_fenced` nor `purge_fences`; with `True` it holds `NOT is_fenced(j.id)`.
  - An integration test: with a `jobs` fence open on one card's job, that job is absent from the page, from `counts` and from the facets. After the fence is released, it is back.
  - Keep the existing fence tests.
- [ ] **Implement.**
- [ ] **Gates.**
  - `tests/unit` with `tests/property`, and the list, facets and fence integration files;
  - `uv run --group docparse pytest -W error -m perf tests/perf/test_jobs_list_perf.py -s`. Record every page's p95. Berlin should be back near M4's 362 ms.
- [ ] **Commit:** `perf(m5): the jobs list adds the fence filter only while a fence is open`.

### Task P2: The keyword and radius pages within budget

**Files**
- `backend/src/jobhunter/jobs/listing.py` and `_sql.py`.
- A migration `0013` only under the Global Constraints' schema rule.
- Tests next to the existing list tests.

**Produces**
- `keyword_entwickler` and `berlin_30_km` p95 < 300 ms in `tests/perf/test_jobs_list_perf.py`, and every other page still under 300 ms.
- **Investigation first.** Run `EXPLAIN (ANALYZE, BUFFERS)` on each statement a first keyword page and a first Berlin page run. Capture the statements with a SQLAlchemy `before_cursor_execute` listener in a local probe test; never commit the probe. Report where the time goes.
- **Then the smallest change that meets the budget.** Candidates, in order of preference:
  - (a) Read the keyword's matching sets once per request, in the snapshot, and bind them into the request's statements as arrays (`= ANY(:ids)`). That covers the title job ids, and the full-text revision ids if they cost too. Each set is read once, not per statement.
  - (b) Steer the title branch to its GIN index, `jobs_title_trgm`.
  - (c) The same once-per-request idea for the radius filter's matches, if the Berlin page is still over budget after P1.
- **Report.** The investigation's numbers, and why the chosen change is sufficient.

**Steps**
- [ ] **Probe:** measure and report, as above.
- [ ] **Failing tests** for the new shape. For example, a keyword request runs the title match once. The perf test itself is the budget gate.
- [ ] **Implement.**
- [ ] **Gates.**
  - unit with property;
  - the list, facets and keyword integration tests;
  - the whole integration suite in quarters;
  - `tests/perf` in full, 4 passed, run with nothing else on the machine.
- [ ] **Commit,** one per change.

### Task P3: Acceptance, the roadmap and the pull request

- **Local gates.** Lint, mypy, `uv lock --check`, the unit, integration and crash chunks, `tests/perf` in full (4 passed), and gitleaks.
- **Roadmap.** In `docs/superpowers/plans/2026-10-16-job-hunter-roadmap.md`, mark M4 and M5 `complete`.
  - M4: CI green on PR #6, `smoke_m4.py` 23 of 23 on a fresh E2E stack on 2026-11-16, and `tests/perf` passing after this plan.
  - M5: CI green on PR #6.
- **Pull request** to `main`, with CI green on its head.

## Rulings made with this plan

- **JP-1.** The fence fix reads "is any fence open" once per request, in the list's snapshot. It does not restructure the predicate in SQL. Within one `REPEATABLE READ` snapshot this is equivalent to today's per-statement InitPlan, and with no fence open it restores M4's exact SQL.
  - Cost if wrong: a plan shape that still degrades while a fence is open, which is rare and bounded by the purge's duration.
- **JP-2.** The keyword and radius fixes are chosen after measurement, within the Global Constraints. No index is added unless a query change cannot meet the budget.
