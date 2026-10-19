# Contract fixtures

Scrubbed recordings of every source (spec §18.4) and the variants derived from them (plan Deviation 14). The
contract tests (`tests/contract/test_*.py`), the adapter tests of Tasks 9–11, the fixture transport of fake mode
(`JOBHUNTER_FAKE_SOURCES=1`, `jobhunter.sources.fixtures.ROUTES`) and the crash and end-to-end tests read them.

**Raw recordings are never committed.** They live only in the gitignored `.fixtures-raw/` of the machine that
recorded them. Every file here was written by the scrubber and passes `tests/contract/test_fixture_pii.py`; each
was also reviewed by hand for recruiter and contact names, signatures and phone numbers in free text (the
review and every scrubber fix it led to are recorded in the Task 6 report).

## How the tree is made

```bash
uv run python scripts/spikes/record_sources.py <source> [--phase N]   # live, budgeted: .fixtures-raw/
uv run jobhunter fixtures scrub .fixtures-raw tests/contract/fixtures
uv run python scripts/spikes/derive_variants.py tests/contract/fixtures \
    --jsearch-items-path data.jobs --jsearch-cursor-path data.cursor --arbeitnow-window 73
```

The scrub and the derivation are deterministic: run again on the same recordings they write the same bytes.
Pseudonyms are numbered per source directory (`[person:n]`, `[email:n]`, `[phone:n]`, `[user:n]`, `[token:n]`),
so one value has one placeholder in every file of its source: the cursor of `jsearch/search_v2_page1`
(`data.cursor`) is the `cursor` that `search_v2_page2` echoes in `parameters.cursor`.

Each case is `<case>.json` (or `.xml`, `.html`, `.txt` for a body that is not JSON) with `<case>.headers.json`
(`{"status", "headers"}`, the §5.2 header allowlist only) and, for a recording, `<case>.meta.json` (the URL
without its query, the non-secret parameters, `fetched_at`, the duration and the body's size and hash).

## JSearch envelope (V9)

`search-v2` puts the items at **`data.jobs`** and the cursor at **`data.cursor`** (`docs/spikes/V9.md`). At the
end of the results the cursor key is present with **`null`** (`search_v2_today`, recorded), so "no cursor" is
`data.cursor` missing or `null`. The response also echoes a sent cursor in `parameters.cursor`.

## Trims

A trim drops whole items and never edits one, so every identity relationship of the kept items survives; the
count-mismatch and pagination cases are variants of their own (ruling (e)).

| File | Kept | Of | Adjusted |
|---|---|---|---|
| `arbeitnow/page1.json` | items 73–102 (0-based) | 300 | nothing (`meta` is as recorded: `per_page` 300, `to` 300) |
| `ats/greenhouse_getyourguide.json` | the first 15 jobs | 70 | `meta.total` 70 → 15 |
| `arbeitnow/page2.json` | the first 30 items | 300 | nothing (`meta` as recorded) |
| `ats/smartrecruiters_deliveryhero_list.json` | the first 10 postings | 100 (`totalFound` 938) | `totalFound` 938 → 10 |

The Arbeitnow window deviates from "the first 30 items" of the plan: the recorded page is three blocks of 100
items (newest first within each block, `created_at` jumps back up at items 100, 150 and 200), and its first 30
items are all German with no empty location, so the Task 10 contract (an `OutOfScope("not_germany")` and an
empty location among the 30) could not hold on them. Items 73–102 are the first contiguous run of 30 that has
German (21), foreign (3) and empty (6) locations; they cross the block boundary at item 100, as the feed does.

## Files

Provenance is `recorded <spike> <UTC date>` or `derived from <file>: <exact change>`. *Items* is the length of
the item array (`-`: no item array). *Rejected* is the number of items the adapter rejects by the item contract
of Tasks 9–11 (a missing native id, or a title that is not a string): **0 in every file**. The quality gate
(`short`, `garbage`) and `OutOfScope` are not rejections. The adapters of Tasks 9–11 confirm these counts.

| File | Status | Items | Rejected | Provenance |
|---|---|---|---|---|
| `jsearch/search_legacy_404` | 404 | - | - | recorded V9 phase 1, 2026-10-18 (`GET /search`, `x-rapidapi-proxy-response: true`) |
| `jsearch/search_v2_page1` | 200 | 10 | 0 | recorded V9 phase 1, 2026-10-18 (`date_posted=week`; cursor set) |
| `jsearch/search_v2_today` | 200 | 0 | 0 | recorded V9 phase 1, 2026-10-18 (`date_posted=today`; `data.cursor: null`) |
| `jsearch/search_v2_page2` | 200 | 9 | 0 | recorded V9 phase 1, 2026-10-18 (the page-1 cursor; 0 of 9 `job_uid` on page 1) |
| `jsearch/search_v2_page2_reused` | 200 | 9 | 0 | recorded V9 phase 2, 2026-10-18 (the page-1 cursor again, 2.01 h later; 7 of 9 `job_uid` of `search_v2_page2`; not routed) |
| `jsearch/search_v2_empty` | 200 | 0 | 0 | derived from `search_v2_page1`: `data.jobs` → `[]`, `data.cursor` → `null` (the recorded end form) |
| `jsearch/search_v2_429` | 429 | - | - | derived from `search_v2_page1`: body → `{"message": "Too many requests"}`, status 429, `x-ratelimit-requests-remaining: 0` |
| `jsearch/search_v2_drift` | 200 | 10 | 0 | derived from `search_v2_page1`: the `status` key removed |
| `jsearch/search_v2_html` | 200 | - | - | derived: a hand-written HTML error page (`502 Bad Gateway`), `content-type: text/html; charset=utf-8`, the headers of `search_v2_page1` |
| `serpapi/account_before` | 200 | - | - | recorded V3, 2026-10-18 (free `account.json`) |
| `serpapi/google_jobs_hl_en` | 200 | 10 | 0 | recorded V3, 2026-10-18 (`engine=google_jobs`, `q=software engineer Berlin`, `gl=de`, `hl=en`) |
| `serpapi/account_after` | 200 | - | - | recorded V3, 2026-10-18 (free `account.json`, 130 s after the search) |
| `serpapi/google_jobs_empty` | 200 | 0 | 0 | derived from `google_jobs_hl_en`: `jobs_results`, `serpapi_pagination` and `filters` removed, `error: "Google hasn't returned any results for this query."` added |
| `serpapi/google_jobs_drift` | 200 | 10 | 0 | derived from `google_jobs_hl_en`: the `search_metadata` key removed |
| `adzuna/search_p1_rpp100` | 200 | 50 | 0 | recorded V18, 2026-10-18 (`results_per_page=100`; `count` 516) |
| `adzuna/search_p1_days1` | 200 | 10 | 0 | recorded V18, 2026-10-18 (`results_per_page=50`, `max_days_old=1`) |
| `adzuna/search_p1_distance10` | 200 | 50 | 0 | recorded V18, 2026-10-18 (`distance=10`; `count` 516) |
| `adzuna/search_p1_distance50` | 200 | 50 | 0 | recorded V18, 2026-10-18 (`distance=50`; `count` 539) |
| `adzuna/search_empty` | 200 | 0 | 0 | derived from `search_p1_rpp100`: `results` → `[]` (`count` kept: a page beyond the end) |
| `adzuna/search_401` | 401 | - | - | derived: a hand-written Adzuna `AUTH_FAIL` exception body, status 401, the headers of `search_p1_rpp100` |
| `adzuna/adzuna_reingest_pair` | 200 | 50 | 0 | derived from `search_p1_rpp100` (ruling P7): the last result replaced by a copy of the first under the new `id` 5933514069 (the first `id` + 1, the first one not on the page), that id in its `redirect_url` path, and a fresh `adref` token `[token:170]`, one above the highest token of the source (the scrubbed `adref` stands for a token that embeds the ad id); the replaced last result was `5926922653`; one ad ingested twice |
| `arbeitnow/page1` | 200 | 30 | 0 | recorded V8 phase 1, 2026-10-18 (`page=1`), trimmed (above) |
| `arbeitnow/page1_remaining5` | 200 | 30 | 0 | derived from `page1`: `x-ratelimit-remaining: 5` |
| `arbeitnow/page2` | 200 | 30 | 0 | recorded V8 phase 2, 2026-10-18 (`page=2`), trimmed (above) |
| `arbeitnow/page1_phase3` … `page1_phase6` | 200 | - | - | recorded V8 phases 3–6, 2026-10-18 (`page=1`, 40/60/90/120 min after phase 1): the headers and the meta only, no body |
| `arbeitnow/page_truncated` | 200 | - | - | derived from `page1`: its JSON cut just inside the first item of `data` (a `.txt` body, `content-type: application/json`) |
| `arbeitnow/page_end` | 200 | 0 | 0 | derived from `page1`: `data` → `[]`, `links.next` → `null` (the recorded form of a missing link, as `prev` and `last`) |
| `ats/greenhouse_getyourguide` | 200 | 15 | 0 | recorded ATS, 2026-10-18 (`content=true`), trimmed (above) |
| `ats/greenhouse_404` | 404 | - | - | recorded ATS, 2026-10-18 (slug `jobhunter-nonexistent-board`) |
| `ats/greenhouse_count_mismatch` | 200 | 15 | 0 | derived from `greenhouse_getyourguide`: `meta.total` 15 → 16 |
| `ats/lever_leverdemo` | 200 | 10 | 0 | recorded ATS, 2026-10-18 (`api.lever.co`, `skip=0&limit=100`) |
| `ats/lever_eu_leverdemo` | 200 | 100 | 0 | recorded ATS, 2026-10-18 (`api.eu.lever.co`: the EU site `leverdemo` exists, so this is a real `lever_eu_<site>`; a full page) |
| `ats/lever_eu_end` | 200 | 0 | 0 | derived from `lever_eu_leverdemo`: body `[]` (the page after a full page) |
| `ats/lever_404` | 404 | - | - | derived: hand-written `{"ok": false, "error": "Document not found"}` |
| `ats/ashby_n8n` | 200 | 42 | 0 | recorded ATS, 2026-10-18 (`includeCompensation=true`) |
| `ats/ashby_404` | 404 | - | - | derived: hand-written `{"success": false, "error": "Job board not found"}` |
| `ats/personio_everphone` | 200 | 8 | 0 | recorded ATS, 2026-10-18 (`/xml`) |
| `ats/personio_entity` | 200 | 8 | 0 | derived from `personio_everphone`: a DTD declaring the entity `expanded` after the XML declaration, `&expanded;` in the first `<name>` (defusedxml must refuse it) |
| `ats/personio_404` | 404 | - | - | derived: hand-written HTML `Not Found` page |
| `ats/recruitee_bunq` | 200 | 22 | 0 | recorded ATS, 2026-10-18 (`/api/offers/`) |
| `ats/recruitee_404` | 404 | - | - | derived: hand-written `{"error": "Not found"}` |
| `ats/smartrecruiters_deliveryhero_list` | 200 | 10 | 0 | recorded ATS, 2026-10-18 (`limit=100&offset=0`), trimmed (above) |
| `ats/smartrecruiters_deliveryhero_detail_1` | 200 | 1 | 0 | recorded ATS, 2026-10-18 (the first posting of the list, `744000156183719`) |
| `ats/smartrecruiters_deliveryhero_detail_2` | 200 | 1 | 0 | recorded ATS, 2026-10-18 (the second posting, `744000156094379`) |
| `ats/smartrecruiters_list_paged` | 200 | 10 | 0 | derived from `smartrecruiters_deliveryhero_list`: `totalFound` 10 → 150 (the pagination rule continues with `offset=100`) |
| `ats/smartrecruiters_list_offset100` | 200 | 0 | 0 | derived from `smartrecruiters_deliveryhero_list`: `offset` → 100, `content` → `[]`, `totalFound` → 150 (the end) |
| `ats/smartrecruiters_404` | 404 | - | - | derived: hand-written `{"message": "Company not found"}` |
| `location_labels.tsv` | - | 172 rows | - | hand-labelled while reviewing (V17, below) |

**Rejected in the fixture set the end-to-end test is served** (JSearch `search_v2_page1` and `search_v2_page2`,
Adzuna `adzuna_reingest_pair` and `search_empty`, Arbeitnow `page1`, `page2` and `page_end`, Greenhouse
`greenhouse_getyourguide`, Recruitee `recruitee_bunq`): **0**, so `FIXTURE_REJECTED = 0` (ruling P7).

## Fake-mode routes

`jobhunter.sources.fixtures.ROUTES`, first match wins: JSearch `search-v2` with any `cursor` → `search_v2_page2`,
with `date_posted=today` → `search_v2_today`, otherwise → `search_v2_page1`, and the legacy `/search` →
`search_legacy_404`; SerpApi `account.json` → `account_before`, `search.json` → `google_jobs_hl_en`; Adzuna page
1 → `adzuna_reingest_pair`, any later page → `search_empty`; Arbeitnow `page=1` → `page1`, `page=2` → `page2`, any
later page → `page_end`; the six boards by host and slug (Lever EU `leverdemo`: `skip=0` → `lever_eu_leverdemo`, later →
`lever_eu_end`; SmartRecruiters `DeliveryHero`: the list, the two recorded details by id), and an unknown slug
of each ATS (a Personio or Recruitee subdomain by the `*.` host wildcard) → that ATS's `_404`. A SmartRecruiters
detail of a posting whose detail was not recorded is answered `smartrecruiters_404`. Every page-2 cursor is
served `search_v2_page2`, so a traversal that continues from page 2 is served page 2 again (no new `job_uid`).

## Location labels (V17)

`location_labels.tsv` (tab-separated, `source string country kind`) holds one row per distinct location string
of the fixture bodies, labelled by hand: the independent ground truth of `test_v17_gazetteer_coverage.py`. The
strings are, per source: JSearch `job_location`; SerpApi `jobs_results[].location`; Adzuna
`location.display_name`; Arbeitnow `data[].location`; Greenhouse `location.name` and `offices[].name`; Lever
`categories.location` and `categories.allLocations[]`; Ashby `location`, `secondaryLocations[].location` and
`address.postalAddress.addressLocality`; Personio `<office>`; Recruitee `location` and `city`; SmartRecruiters
`location.fullLocation` and `location.city`. `source` is the source, or the ATS for `ats/` files. `country` is
the ISO 3166-1 alpha-2 code of the place named, or `-` for `remote`, `country_wide` (a whole country, or
"anywhere") and `unknown` (a region such as a US state or "Europe", a fictional place, or a text that names no
place). `kind` is `city` (one place), `multi` (several), `country_wide`, `remote` or `unknown`. A label is never
edited to fit the parser.
