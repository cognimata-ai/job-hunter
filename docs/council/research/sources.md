# Job-ad data sources for a German-market job matcher — verified findings

Research date: **2026-10-16** (all live calls made between 09:43 and 09:57 UTC from this container).
Scope: one user, one CV, scheduled collection every N hours, cross-source dedup, LLM fit-scoring.
Convention: everything not marked **UNVERIFIED** was either read in the provider's own docs (cited) or observed in a live response saved under `research/samples/`. "Observed" means measured in our sample (small n; treat percentages as indicative, not statistics).

---

## 1. Summary comparison

| Source | Auth | Quota / rate limit | Results / page | Full description? | Posted date | Remote flag | Salary | Stable ID | DE coverage | ToS notes |
|---|---|---|---|---|---|---|---|---|---|---|
| **JSearch** (OpenWeb Ninja via RapidAPI) | `x-rapidapi-key` header | Free BASIC: **200 req/month** (header `x-ratelimit-requests-limit: 200`); **1 credit per page of ≤10 jobs** | 10 | **Yes** (observed 2.4–4.4k chars, 10/10) | Relative string (`"vor 2 Tagen"`) **plus derived** `job_posted_at_timestamp` / `_datetime_utc` (day- or hour-truncated) | `job_is_remote` bool (+ `work_from_home` filter) | Fields exist; **0/10 filled** in DE sample | `job_uid` (= Google `htidocid`). **`job_id` is NOT stable** (embeds a session blob) | Good (Google for Jobs DE: LinkedIn, StepStone, XING, Indeed, Join, Arbeitnow, …) | Third-party Google scraper; RapidAPI terms |
| **SerpApi Google Jobs** | `api_key` param | Free: **250 searches/month**; account.json reports `account_rate_limit_per_hour: 250` (pricing page says 50/h) | 10 (`next_page_token`) | **Mostly** (9/10 ≥2k chars; 1/10 only 56 chars) | **Relative only** (`"vor 4 Tagen"` in `extensions`) | With `hl=de`: only the string `"Homeoffice"` in `extensions`; `detected_extensions` **absent** | String in `extensions` (2/10: `"60.000 € bis 80.000 € pro Jahr"`) | Google `htidocid` inside base64-JSON `job_id` | Same index as JSearch | Each page = 1 search; **empty results are billed**; cached (≤1 h, identical params) free |
| **Adzuna** | `app_id`+`app_key` params | Default **25/min, 250/day, 1000/week, 2500/month** | **max 50** (100 silently capped to 50) | **No: truncated to 500 chars** (50/50 exactly 500, ending in `…`) | Absolute ISO `created` (= Adzuna ingestion time) | None | `salary_min/max` (8/50) + `salary_is_predicted` | `id` (but **same ad re-ingested under new ids**: 4 copies observed) | Strong (country `de`) | Permissible use: publishing listings / personal research; **commercial orgs: 14-day trial then licence**; attribution required |
| **BA Jobsuche** (`rest.arbeitsagentur.de`) | Static header `X-API-Key: jobboerse-jobsuche` | **None documented** | `size` (≤100 per community docs, UNVERIFIED) | Search: none. `jobdetails`: **full for BA-native ads** (6,103 chars), **short summary for partner/external ads** (304 chars) | `datumErsteVeroeffentlichung` (date), `aenderungsdatum` (local timestamp) | `homeofficemoeglich` bool, `homeofficetyp`, `homeofficeprozent` | `gehaltsspanneVon/Bis` (1/17) | `referenznummer` (refnr) | **Largest German DB** | **Unofficial, reverse-engineered API**; BA portal terms forbid robots and using interfaces "zum Zweck der Datensammlung und -auswertung" |
| **Arbeitnow** | None | `x-ratelimit-limit: 50` (window UNVERIFIED) | **300** | **Yes** (HTML; median 6.4k chars) | Unix `created_at` | `remote` bool | None | `slug` (numeric suffix) | Germany-heavy but Europe-wide (London, Amsterdam, Zurich…) | "free public API … please do not abuse … linking back"; site ToS applies |
| **Remotive** | None | **"max. 4 times a day"**; ">2x per minute will be blocked" | all (`limit`) | Yes (HTML) | ISO `publication_date` (no TZ) | All remote; `candidate_required_location` | Free-text (7/14) | `id` | Low (14 jobs returned; remote/EU-eligible) | **No redistribution to third-party sites; must link back + credit Remotive; 24 h delay** |
| **Greenhouse** board | None | Not documented | all jobs in one response | Yes (`content=true`, HTML-escaped) | ISO `first_published`, `updated_at` | No dedicated field (location name only) | `pay_transparency` only on job endpoint | `id` | Per company (user list) | Public GET, no auth |
| **Lever** postings | None (`api.lever.co` / **`api.eu.lever.co`**) | Not documented for GET (POST: 2 req/s) | `skip`/`limit` | Yes (`descriptionPlain` etc.) | Epoch-ms `createdAt` | `workplaceType` on-site/remote/hybrid | `salaryRange` (optional) | `id` (UUID) | Per company | EU instance must be queried separately |
| **Ashby** | None | Not documented | all | Yes (`descriptionPlain`, `descriptionHtml`) | ISO `publishedAt` | `isRemote`, `workplaceType` | `includeCompensation=true` | `id` (UUID) | Per company | — |
| **SmartRecruiters** | None for public postings (observed) | 10 req/s typical, 8 concurrent (generic SmartAPI limits) | `limit`/`offset` | **List: no description**; per-posting `ref` needed | ISO `releasedDate` | `location.remote`, `location.hybrid` | No | `id`, `uuid` | Per company (e.g. Delivery Hero 942 postings) | — |
| **Recruitee** | None (`/api/offers/`) | Not documented | all | Yes (`description` + `requirements`) | `created_at`, `published_at` (UTC strings) | `remote`, `hybrid`, `on_site` | `salary` object | `id`, `guid` | Per company | — |
| **Personio** XML | None (`{co}.jobs.personio.de/xml`) | Not documented | all | Yes (HTML `jobDescriptions` blocks; 5.7–12k chars) | `createdAt` (ISO) | No field observed | `salaryInformation` (1/8) | `id` | Very common among German SMEs | — |
| **Workable** | Official API needs employer token; public widget endpoint unofficial | Widget endpoint returned **HTTP 429 on first call** | — | UNVERIFIED | UNVERIFIED | UNVERIFIED | — | `shortcode` (UNVERIFIED) | Per company | Unofficial endpoint |
| Jooble | API key on request | UNVERIFIED | UNVERIFIED | Snippet (third-party reports) | UNVERIFIED | — | — | — | DE available | Official page unreachable from here (HTTP 403) |
| Careerjet | Publisher API key (Basic auth) | UNVERIFIED | — | **Excerpt** ("Job description excerpt") | — | — | — | — | DE locale | Requires end-user `user_ip` + `user_agent` per call (publisher-site model) |
| LinkedIn / Indeed / StepStone / XING | No job-seeker search API | — | — | — | — | — | — | — | — | LinkedIn + Indeed ToS explicitly ban scraping; see §10 |

---

## 2. JSearch (OpenWeb Ninja, via RapidAPI)

**Docs:** OpenAPI spec `https://openwebninja.s3.us-east-1.amazonaws.com/portal/openapi/jsearch.yaml` (linked as `docsUrl` from `https://www.openwebninja.com/api/jsearch/docs`); `https://www.openwebninja.com/api/jsearch/llms.txt` ("Last updated: 2026-05-27"); RapidAPI listing `https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch`.

### Endpoints (current)
RapidAPI listing routes (extracted from the listing page's embedded data): `/search-v2` ("Job Search"), `/job-details`, `/estimated-salary`, `/company-job-salary`.

- **The legacy `/search` route is gone, not merely aliased.** Our first call to `GET https://jsearch.p.rapidapi.com/search` returned `HTTP 404 {"message":"Endpoint '/search' does not exist"}` with `x-rapidapi-proxy-response: true` (answered by the RapidAPI gateway) and **no `x-ratelimit-*` headers**. The following successful call showed `x-ratelimit-requests-remaining: 199` of 200, which **proves the gateway 404 was not billed**. (The RapidAPI README prose still says `GET /search`; it is stale.) Old third-party copies of the RapidAPI docs describe `/search` with `page` 1–50 and the note "requests for more than one page and up to 10 pages are charged x2 and requests for more than 10 pages are charged 3x". That pricing note is **obsolete**, see billing below.
- The native OpenWeb Ninja base URL is `https://api.openwebninja.com/jsearch` with header `x-api-key` (a separate subscription; not tested).

### `/search-v2` parameters (from the OpenAPI spec)
`query` (required, free text; put title + location in it), `cursor` (from `data.cursor`), `num_pages` (1–20, default 1), `country` (ISO-3166 alpha-2, default `us`; **must be `de`** for Germany), `language` (ISO-639; empty = country's primary language; our echo showed `language: "de"`), `date_posted` (`all|today|3days|week|month`), `work_from_home` (bool), `employment_types` (`FULLTIME,CONTRACTOR,PARTTIME,INTERN`), `job_requirements` (`under_3_years_experience,more_than_3_years_experience,no_experience,no_degree`), `radius` (km → Google `lrad`, "not strictly followed"), `exclude_job_publishers` (comma list), `fields` (projection).
`page` is **not** a `/search-v2` parameter (cursor pagination replaced it).

### Billing / quota (verified)
- RapidAPI billing item text: *"A call to any endpoint is one request. For search requests, each page returned by the API counts as a single request credit (up to 10 job listings)."* The spec says the same for `num_pages`: *"Each page (containing up to 10 results) returned by the API consumes one request credit."* So **cursor pagination costs 1 request per page**, and `num_pages=N` costs N.
- `/job-details`: batching up to 20 ids, *"each Job Id in a batch request is counted as a request"*.
- Plans (RapidAPI listing data): BASIC $0 / **200 per month**; PRO $25 / 10,000; ULTRA $75 / 50,000; MEGA $150 / 200,000.
- Observed quota headers (`samples/jsearch_search_v2.headers.txt`):
  `x-ratelimit-requests-limit: 200`, `x-ratelimit-requests-remaining: 199`, `x-ratelimit-requests-reset: 2675242` (s ≈ 30.96 days, i.e. a monthly window anchored to subscription date, not the calendar month), plus RapidAPI's global `x-ratelimit-rapid-free-plans-hard-limit-limit: 500000` / `-remaining: 499999` / `-reset: 2675242`. The RapidAPI docs define the `x-ratelimit-requests-*` trio and say the gateway returns **429** when limits are hit.
- Per-second limit for BASIC: **UNVERIFIED**.

### Live sample (`samples/jsearch_search_v2.json`)
Request: `query=software engineer in Berlin, Germany&country=de&date_posted=week` → 10 jobs, `data.cursor` present (opaque base64 protobuf-like string).
- **Publishers observed (`job_publisher`):** LinkedIn 2, Trabajo.org 2, Stepstone 1, JobLeads 1, Join 1, Arbeitnow.com 1, BeBee 1, Learn4Good 1.
- **IDs:** `job_id` = base64(`"<google htidocid>:<276-byte session blob>"`). It is **not stable** across searches and cannot serve as a dedup key. `job_uid` = the bare Google `htidocid` (e.g. `UMsYA14AEpQuse3eAAAAAA==`), and it **matched SerpApi's `htidocid` for the same ads (3/10 overlap)**. Stability of `htidocid` across days: UNVERIFIED (same-day only).
- **Dates:** `job_posted_at` is localized (`"vor 15 Stunden"`, `"vor 2 Tagen"`). `job_posted_at_timestamp`/`_datetime_utc` are **derived** from it: "vor 2 Tagen" → `2026-10-14T00:00:00Z` (day-truncated); "vor 15 Stunden" → `2026-10-15T18:00:00Z` (hour-truncated) for a request at 09:47Z. Precision is therefore ≈1 day.
- **Description:** full text, 2,376–4,384 chars, no truncation marker; `job_highlights` was `{}` for all 10; `job_benefits`, `job_onet_soc`, salary fields all null in this DE sample.
- **Location quality:** `job_location` contains localized UI text glued to the publisher, e.g. `"Berlin     •  über Stepstone"`, `"Beliebiger Ort   •  über LinkedIn"`. `job_city`/`job_country` were filled for only 2/10. Location must be parsed or geocoded.
- **apply_options:** exactly **1 per job** in `/search-v2` (vs up to 8 in SerpApi for the same ads). Whether `/job-details` returns more: UNVERIFIED.
- `job_employment_types` normalized (`FULLTIME`, `PARTTIME`) even when the label is German (`"Vollzeit und Teilzeit"`).
- JSearch apply links came **without** `utm_*` (already cleaned), e.g. `https://www.stepstone.de/stellenangebote--AI-Native-Software-Engineer-m-w-d-Berlin-Vitaservices-GmbH-Co-KG--14607704-inline.html`.

---

## 3. SerpApi — Google Jobs (`engine=google_jobs`)

**Docs:** `https://serpapi.com/google-jobs-api` (+ `.md`), `https://serpapi.com/google-jobs-listing-api`, `https://serpapi.com/account-api`, `https://serpapi.com/pricing` (+ `.md`), `https://serpapi.com/search-archive-api`, `https://serpapi.com/google-jobs-countries` (Germany `de` supported).

### Parameters
`q` (required), `location` / `uule` (mutually exclusive), `google_domain`, `gl`, `hl`, `next_page_token` (*"Up to 10 results are returned per page"*; `start` discontinued by Google), `lrad` (km, "Does not strictly limit"), `uds` (filter tokens taken from the response's `filters[]`), deprecated `chips` and `ltype`, and SerpApi params `no_cache`, `async`, `output` (`json|html|md`), `json_restrictor`, `zero_trace` (enterprise).

### Billing (verified in docs + account endpoint)
- *"Only successful searches are counted … Cached, errored, and failed searches are not … responses with 100 results or empty result sets will both count as 1 search."* (pricing FAQ). Each `next_page_token` page is a separate search.
- Cache: *"A cache is served only if the query and all parameters are exactly the same. Cache expires after 1h. Cached searches are free."*
- Account API: *"Account API is free of charge, and using it will not be counted toward your monthly quota."* We verified this: 6 calls left usage unchanged. Fetching an archived search's `raw_html_file` (no key needed) also did not change usage.
- Free plan per pricing page: 250 searches/mo, 50/h throughput. Our account.json shows `searches_per_month: 250`, **`account_rate_limit_per_hour: 250`** (discrepancy with pricing page; harmless at our volumes). `plan_renewal_date: 2026-11-08`.
- **Observed lag:** right after search #2, account.json showed `this_hour_searches: 2` but `this_month_usage: 1`. A minute later it showed `this_month_usage: 2`, `plan_searches_left: 248`. Budget checks should read `this_hour_searches` or allow for lag.

### Google Jobs Listing engine (`google_jobs_listing`)
Per its docs: *"the following fields are no longer available: salaries, salaries_comparison, apply_options, and similar_jobs. Currently, the only available field is ratings."* It costs a normal search (same caching rules). **It has no value for this app.**

### Live attempts
1. `q=Software Engineer&location=Berlin,Berlin,Germany&gl=de&hl=de` → **0 jobs**, `"error": "Google hasn't returned any results for this query."`, `search_information.jobs_results_state: "Fully empty"`, yet `filters[]` were returned. **Billed** (250→249). The archived raw Google HTML (`samples/serpapi_google_jobs_attempt1_raw_google.html`) shows Google itself said *"Es gibt anscheinend keine „Jobs"-Ergebnisse für dieses Thema"*, so this was **not a SerpApi parse error**. Google URL used: `…q=Software+Engineer&uule=w+CAIQICIVQmVybGluLEJlcmxpbixHZXJtYW55&hl=de&gl=de&udm=8`.
2. `q=Software Engineer Berlin&gl=de&hl=de` (no `location`) → **10 jobs** + `serpapi_pagination.next_page_token`. Billed (→248).
   **Conclusion (n=1, confounded because `q` also changed):** the city-level `location`/`uule` parameter produced an empty Google Jobs result, while putting the city in `q` worked. JSearch, which also puts the location in the query text, worked too. Treat "use location-in-query, not `location`" as the default and verify before relying on `location`.

### Fields (attempt 2, `samples/serpapi_google_jobs_attempt2.json`)
Per job: `title`, `company_name`, `location`, `via`, `share_link`, `source_link`, `job_title`, `extensions`, `description`, `apply_options[]` (`title`, `link`), `job_id`, `thumbnail` (5/10).
- **With `hl=de` there is no `detected_extensions` object** (0/10). Only raw German `extensions` arrive: `"vor 4 Tagen"`, `"Vollzeit"`, `"Homeoffice"`, `"Freiw. Altersvorsorge"`, `"60.000 € bis 80.000 € pro Jahr"` (note the NBSP before €). The docs' `detected_extensions` (`posted_at`, `schedule_type`, `work_from_home`, `salary`) appear in English examples. Whether `hl=en&gl=de` yields `detected_extensions` for German ads is UNVERIFIED.
- `job_id` = base64 JSON: `{"job_title":…,"company_name":…,"htidocid":"UMsYA14AEpQuse3eAAAAAA==","gl":"de","hl":"de","fc":"<session token>"}`. Extract `htidocid` for dedup.
- `via` observed: Stepstone 2, LinkedIn, Stolzberger GmbH (employer site), Join, IT-Jobs.de, XING, Indeed, Trabajo.org, JobLeads.
- Description length: 56, 2,086–4,374 chars. One description (Instaffo) ended with repeated literal `"null"` lines (garbage).
- `filters[]` offered `uds` tokens for `Homeoffice`, `Jobtyp` (Vollzeit/Teilzeit/Vertrag/Praktikum), `Veröffentlicht` (Gestern / Letzte 3 Tage / Letzte Woche / Letzter Monat). The date filter's `q` becomes `"Software Engineer Berlin seit gestern"` + `uds`. Date filtering therefore needs a token from a prior (billed) response. Token reusability across runs: UNVERIFIED.
- All apply links carry `utm_campaign=google_jobs_apply&utm_source=google_jobs_apply&utm_medium=organic`.
- **apply_options contamination (important for dedup):** 48 apply options across 10 jobs (8,8,1,8,8,1,7,4,2,1). In at least 2 of 10 jobs, **≥7 options point to different employers' jobs** (URL slug names another company/role). The KORSCH AG listing links Jobtensor "Embedded-Software-Engineer-IoT-Security", BeBee "humanetics-europe-gmbh" and StudySmarter "recare-deutschland-gmbh". The Stolzberger listing links StudySmarter "personio/staff-software-engineer-money-domain", Blockchain Jobs Board "moonpay" and netzundwerke/Jobilize "5g-standalone-core". Mirror/spam publishers also appear ("Jobs For Stevenage Fans", "SaluteMyJob", "Top County Careers").

---

## 4. Adzuna (`/v1/api/jobs/de/search/{page}`)

**Docs:** `https://developer.adzuna.com/overview`, `https://developer.adzuna.com/docs/search`, Swagger `https://developer.adzuna.com/swagger/spec/test2.json` (OpenAPI 3.1), `https://developer.adzuna.com/docs/terms_of_service`.

- **Params (spec):** `app_id`, `app_key`, `results_per_page`, `what`, `what_and`, `what_phrase`, `what_or`, `what_exclude`, `title_only`, `where`, `distance` (km, default 5), `location0..7`, `max_days_old`, `category` (tag from `/categories`), `sort_dir` (`up|down`), `sort_by` (`default|hybrid|date|salary|relevance`), `salary_min`, `salary_max`, `salary_include_unknown`, `full_time`, `part_time`, `contract`, `permanent`, `company`. Country enum includes `de`.
- **results_per_page max = 50 (observed):** requesting 100 silently returned 50 (`count: 113`). The docs do not state the max.
- **Rate limits (ToS):** *"25 hits per minute, 250 hits per day, 1000 hits per week, 2500 hits per month"*, "We may vary these from time to time". No rate-limit headers in responses.
- **Description: truncated.** Spec: *"The details of the advertisement, truncated to 500 characters."* Docs: *"we currently only provide a snipped of the job description"*. Sample: 50/50 descriptions are exactly 500 chars ending in `…`.
- **redirect_url:** `https://www.adzuna.de/land/ad/<id>?se=…&utm_medium=api&utm_source=<APP_ID>&v=…`. **It embeds your `app_id` as `utm_source`** (redacted in samples). Fetching two of them (website, not API) landed on **adzuna.de's own detail page** showing the full description and an "Auf diesen Job bewerben" button, not a direct employer URL. The spec says to use this URL "to comply with Adzuna's terms and conditions".
- **Fields:** `id`, `adref` (JWT-like), `title`, `description`, `created` (ISO UTC), `redirect_url`, `latitude`, `longitude`, `location{display_name, area[]}`, `category{tag,label}`, `company{display_name}`, `salary_min/max`, `salary_is_predicted` ("0"/"1"), `contract_time` (27/50), `contract_type` (9/50).
- **Data quality:** category `unknown` 24/50, `it-jobs` 15/50. **Intra-source duplicates:** "AI-Native Software Engineer (m/w/d) – Vitaservices" appears with 4 different ids (`created` 10-14T11:47, 17:43, 20:20, 10-16T03:48). `created` is the Adzuna ingestion time, not the employer's posting date.
- **Cross-links:** SerpApi apply_options contain `https://www.adzuna.de/details/5926833612` and `/details/5928754022`. These are the same ids as in the Adzuna API sample, so the Adzuna id is extractable from Google results.
- **ToS:** Permissible use = *"Publishing Adzuna ad listings; Publishing Jobsworth salary estimates; Personal research"*. *"Any other use of the Adzuna API by a commercial, government or academic organisation … is permitted subject to a 14 day trial period … After the trial period ends, a licence agreement may be required."* Attribution is required when publishing (an "Adzuna" label of at least 116×23 px, linked). *"Upon termination … immediately remove all … data acquired from Adzuna"*. Multiple accounts = misuse. A single-user personal tool plausibly fits "personal research"; if operated by a company (the user's email domain is a company), the 14-day-trial clause may apply. **This needs a human decision.**

---

## 5. Bundesagentur für Arbeit — Jobsuche API

**Docs:** community spec `https://github.com/bundesAPI/jobsuche-api` (README + `openapi.yaml`, v2.1.0), mirrored at `https://jobsuche.api.bund.dev/openapi.yaml`.

### Why the original call got 403 (diagnosed, verified)
Same header `X-API-Key: jobboerse-jobsuche`:
| Path | Result |
|---|---|
| `/pc/v4/jobs?was=Python&size=1` | **403**, 1-byte body |
| `/pc/v4/app/jobs?was=Python&size=1` | **403** (the README still lists it as an option) |
| **`/pc/v6/jobs?was=Python&size=1`** | **200** JSON, `X-API-BLOCKED: 0` |
| `/pc/v4/jobdetails/{base64(refnr)}` | **200** |

So the cause is the **endpoint version**: search moved to `/pc/v6/jobs`. Auth, User-Agent and OAuth were not involved: plain curl UA and the static key worked. The README now documents `/pc/v6/jobs` as the search path. The OAuth token endpoint `https://rest.arbeitsagentur.de/oauth/gettoken_cc` is reported as 403 in issue #60 (Jun 2025) and is not needed.

### Search `/pc/v6/jobs`
Params (spec): `was`, `wo`, `berufsfeld`, `page` (1-based), `size`, `arbeitgeber`, `veroeffentlichtseit` (0–100 days; **works**: facet counts matched), `zeitarbeit`, `angebotsart` (1 Arbeit, 2 Selbständigkeit, 4 Ausbildung, 34 Praktikum), `befristung` (1;2), `arbeitszeit` (`vz;tz;snw;ho;mj`, where `ho` = Homeoffice), `behinderung`, `corona`, `umkreis` (km), `pav`.
Response keys: `ergebnisliste[]`, `maxErgebnisse`, `page`, `size`, `woOutput` (resolved place + coordinates), `facetten` (counts by befristung, berufsfeld, arbeitsort, veroeffentlichtseit, …).
**The v6 schema differs from the published spec** (issue #69, Aug 2026): `ergebnisliste` (not `stellenangebote`), `referenznummer` (not `refnr`), `stellenangebotsTitel`, `firma`, `datumErsteVeroeffentlichung`, `stellenlokationen`, `aenderungsdatum`.
Observed item fields: `referenznummer`, `stellenangebotsTitel`, `firma`, `hauptberuf`, `alleBerufe`, `stellenlokationen[{adresse{plz,ort,region,land},breite,laenge}]`, `datumErsteVeroeffentlichung`, `aenderungsdatum`, `veroeffentlichungszeitraum`, `eintrittszeitraum`, `arbeitszeitVollzeit` and other `arbeitszeit*` booleans, `homeofficemoeglich`, `homeofficetyp` (`ANGABE_IN_PROZENT`, `NACH_VEREINBARUNG`), `homeofficeprozent`, `verguetungsangabe`, `artDerVerguetung`, `gehaltsspanneVon/Bis`, `vertragsdauer`, `befristungInMonaten`, `externeURL` (4/17), `arbeitgeberKundennummerHash`, `entfernung`, `chiffrenummer`. **No description in search results.**
Sample: `was=Software Engineer&wo=Berlin&umkreis=25&veroeffentlichtseit=7&size=25` → `maxErgebnisse: 17` (facets: 131 for "alle"). Volume for an English title query is modest. German titles (e.g. "Softwareentwickler") would need separate queries.

### Details `/pc/v4/jobdetails/{base64(refnr)}`
Example: `18251-33767-S` → `MTgyNTEtMzM3NjctUw==` (standard base64 with `=` padding, used as-is in the path).
- BA-native/partner-integrated ad (Zalando, `allianzpartnerName: "Zalando SE"`): `stellenangebotsBeschreibung` **6,103 chars, full**.
- External job-board ad (`externeURL` → gute-jobs.de, `allianzpartnerName: "Wollmilchsau GmbH"`): description **304 chars**, a summary only. Full text sits behind `externeURL`, which carries `utm_source=jsba-premium…`.
- Detail fields: `stellenangebotsBeschreibung`, `allianzpartnerName/Url`, `istPrivateArbeitsvermittlung`, `istArbeitnehmerUeberlassung` (temp agency), `istBetreut`, `istBehinderungGefordert`, plus search fields. **One request per job** is needed for descriptions.

### Limits & terms
- No rate limit is documented. Responses carry `X-API-BLOCKED: 0`, which suggests server-side blocking exists. Issue #60 reports "sporadic" 403s on regular polling. Community issue titles mention max `size=100` (#23) and a 100-page limit (#14); the details are UNVERIFIED.
- **Legal status:** the README says BA *"bietet … bis heute keine offizielle API an"*, and issue #39 calls it the *"reverse-engineered Frontend-API"*. BA portal terms (`https://www.arbeitsagentur.de/nutzungsbedingungen`, §2a(3)) prohibit *"Robots, Webspider oder ähnliche Technologien einzusetzen sowie bestehende Kommunikations- bzw. Programmierschnittstellen entgegen dem von der BA beabsichtigten Zweck zu verwenden und so Inhalte des Portals … zum Zweck der Datensammlung und -auswertung auszulesen"*. A scheduled personal crawler is arguably within this prohibition. **This is a policy risk for a human to accept or reject.**
- Personal data note: some descriptions contain recruiter names, emails and phones (also true of the other sources).

---

## 6. Arbeitnow (`https://www.arbeitnow.com/api/job-board-api`)

Docs: `https://www.arbeitnow.com/blog/job-board-api`; Postman collection `https://documenter.getpostman.com/view/18545278/UVJbJdKh`.
- No key. Params documented: `page`, `visa_sponsorship=true|false`. No keyword or location filters are documented, so it is a **firehose to filter client-side**.
- `meta.per_page: 300`; `links.next` for pagination (`last: null`). The response for page 1 was 2.4 MB.
- `meta.info`: *"Jobs are updated every hour and order by the `created_at` timestamp."* `meta.terms`: *"This is a free public API for jobs, please do not abuse. I would appreciate linking back to the site. By using the API, you agree to the terms of service present on Arbeitnow.com"*.
- Headers: `x-ratelimit-limit: 50`, `x-ratelimit-remaining: 48` (window UNVERIFIED), `cache-control: private, max-age=432000`, `cf-cache-status: HIT`, `age: 1415`. The response is CDN-cached, so polling more often than about hourly gains nothing.
- Fields: `slug`, `company_name`, `title`, `description` (HTML, full: 685–53,989 chars, median 6,356), `remote` (bool; 14/300 true), `url` (arbeitnow page), `tags`, `job_types` (free-text mixed DE/EN: "Full Time", "Unbefristeter arbeitsvertrag", "berufserfahren"), `location` (free text; 18/300 empty), `created_at` (unix seconds).
- Coverage: page 1 spanned ~37 h (2026-10-14 20:32 → 10-16 09:20 UTC). Locations: London 31, Berlin 24, Amsterdam 23, Zurich 15, Paris 12, Munich 11. It is Europe-wide, ATS-sourced (per the blog). Arbeitnow ads also show up inside Google for Jobs (JSearch publisher "Arbeitnow.com"; SerpApi apply option `arbeitnow.com/jobs/companies/roboservice-gmbh/…-194025`).

## 7. Remotive (`https://remotive.com/api/remote-jobs`)

Docs: `https://github.com/remotive-com/remote-jobs-api` (README); legal notice is embedded in every response (`0-legal-notice`).
- Params: `category`, `company_name`, `search`, `limit`.
- **Rate guidance (verbatim):** *"you only need to GET Remotive job data through this API a couple of times a day (we advise max. 4 times a day) … excessive requests (more than 2x per minute) will be blocked."*
- **ToS:** *"Please do not submit Remotive jobs to third Party websites, including … Google Jobs, LinkedIn Jobs. Please link back to the URL found on Remotive AND mention Remotive as a source … Jobs displayed are delayed by 24 hours."* The private paid API "starting budget is $5k/mo".
- Sample (`category=software-dev&limit=50`): only **14 jobs** (`total-job-count: 14`). Categories returned were mixed (Software Development 6, Writing 2, Marketing 1, …), so the filter appears ineffective or the public feed is tiny (cause UNVERIFIED). Fields: `id`, `url`, `title`, `company_name`, `company_logo`, `category`, `tags`, `job_type`, `publication_date` (no TZ), `candidate_required_location` (e.g. "Worldwide", "Americas, Europe, Israel"), `salary` (7/14), `description` (HTML, 2.6–36k chars). 13/14 were EU/worldwide-eligible. **Low value for a German on-site/hybrid search.**

---

## 8. Company ATS public boards (need a user-maintained company list)

| ATS | URL pattern (GET, no auth) | Tested with | Result | Description | Date field | Remote/hybrid | Notes |
|---|---|---|---|---|---|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | `getyourguide` | 200, 71 jobs (53 Berlin), 666 KB | Full HTML-escaped `content` (3.3–9k chars) | `first_published`, `updated_at` (ISO with offset) | none (location name) | Docs: *"authentication is not required for any GET endpoints"*. Fields incl. `language`, `departments`, `offices`, `requisition_id`, `metadata`. Rate limit undocumented. |
| Lever | `https://api.lever.co/v0/postings/{site}?mode=json` **and** `https://api.eu.lever.co/v0/postings/{site}?mode=json` | `leverdemo` | 200 | `description(Plain)`, `lists`, `additional` | `createdAt` (epoch ms) | `workplaceType` | EU-hosted customers are only on the `api.eu` instance. `country` ISO code. POST limit 2/s; GET undocumented. |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true` | `n8n` | 200, 41 jobs, 966 KB | `descriptionPlain` (4.9–11.6k chars) + HTML | `publishedAt` | `isRemote`, `workplaceType` | `address.postalAddress`, `secondaryLocations`, `employmentType`, `compensation`. |
| SmartRecruiters | `https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=&offset=` | `DeliveryHero` | 200, `totalFound: 942` | **Not in list**; follow each `ref` (`…/postings/{id}`) | `releasedDate` | `location.remote/hybrid/hybridDescription` | `language`, `industry`, `function`, `experienceLevel`, `typeOfEmployment`. Generic SmartAPI limits: 10 req/s, 8 concurrent. Unauthenticated public list works (observed); docs mention API-key/OAuth for *internal* postings. |
| Recruitee | `https://{co}.recruitee.com/api/offers/` | `bunq` (NL) | 200, 22 offers | `description` + `requirements` (HTML) | `created_at`, `published_at`, `updated_at` | `remote`, `hybrid`, `on_site` | `country_code`, `city`, `employment_type_code`, `salary{min,max,period,currency}`, `careers_url`. |
| Personio | `https://{co}.jobs.personio.de/xml` | `ecosia` → 404; `everphone` → 200 | 8 positions | `jobDescriptions/jobDescription{name,value(CDATA HTML)}` 5.7–12k chars | `createdAt` | none observed | Root `<workzag-jobs>`; `office`, `department`, `employmentType`, `seniority`, `schedule`, `yearsOfExperience`, `occupation(Category)`, `keywords`, `salaryInformation` (1/8). The docs list fewer fields than observed. |
| Workable | Official: `https://{sub}.workable.com/spi/v3/jobs` with a **Bearer token from the employer** (not usable by a job seeker). Unofficial: `https://apply.workable.com/api/v1/widget/accounts/{sub}` | `workable` | **429** on first call | UNVERIFIED | UNVERIFIED | UNVERIFIED | Unofficial and rate-limited. Avoid it, or treat it as best-effort. |

Other German ATS seen in the wild (softgarden, Join, SuccessFactors, Workday) were not tested. Join links appeared in Google results (`join.com/companies/{co}/{id}-slug`).

---

## 9. Other Germany-relevant sources (short)

- **Jooble:** key on request (`https://jooble.org/api/about`). The official page returned HTTP 403 to curl, and WebFetch could not resolve the host. Method (POST JSON `{keywords, location}` to `https://jooble.org/api/{key}`), limits and terms are **UNVERIFIED**; third-party guides say the description is a snippet.
- **Careerjet:** `https://search.api.careerjet.net/v4/query`, Basic auth with publisher API key (from a publisher account). **`user_ip` and `user_agent` of the end user are required.** It is built for publisher websites, not back-end crawlers. Response `description` is a *"Job description excerpt"*. Source: `https://www.careerjet.com/partners/api`.
- **GermanTechJobs:** no official API or feed found. `https://germantechjobs.de/api` returns a JS shell; only third-party scrapers exist. **Not usable without scraping.**
- **EURES:** no official public job-search API documentation found (only third-party Apify scrapers describing an internal "jv-search" endpoint). **UNVERIFIED / not recommended.**

## 10. LinkedIn / Indeed / StepStone / XING

- **LinkedIn:** the Job Posting API is for approved partners to *post* jobs. Microsoft Learn: *"We are currently not accepting new partnerships for LinkedIn's Job Posting API"* and *"use of these APIs is restricted to those developers approved by LinkedIn"* (`https://learn.microsoft.com/en-us/linkedin/talent/job-postings/api/overview`). There is no job-seeker search API. The User Agreement prohibits using *"software, devices, scripts, robots … (such as crawlers …) to scrape or copy the Services"* (`https://www.linkedin.com/legal/user-agreement`).
- **Indeed:** ToS (`https://www.indeed.com/legal`): *"Use any automated system (bots, scrapers, spiders, AI or Agentic AI) to access, data-mine … without Indeed's express written permission … You may not crawl, scrape, extract data from…"*. The public Publisher Job Search API is no longer available: Arbeitnow's blog says it *"is no longer maintained and it requires a published account which we cannot obtain anymore"*. `docs.indeed.com` returned 403 here, so an official deprecation notice is UNVERIFIED.
- **StepStone:** no public job-seeker API found. `https://www.stepstone.de/agb/` returned **HTTP 403 "Zugriff verweigert"** to curl (bot protection), so the AGB scraping clause is **UNVERIFIED**.
- **XING:** `https://dev.xing.com/` currently lists only Plugins (Share button, Login with XING), Partners/"Job Integration" (employer-side posting) and "Apply With XING". There is no job-search REST API. The XING terms page is JS-rendered/unfetchable, so its scraping clause is **UNVERIFIED**.
- Practical route: these boards' ads already reach us **indirectly** via Google for Jobs (JSearch/SerpApi `via`/`job_publisher` = LinkedIn, Stepstone, XING, Indeed observed) and via Adzuna/BA partner feeds.

---

## 11. Cross-cutting answers

### 11.1 Cross-source dedup: what links the same ad (from real samples)
Same-day samples, query "Software Engineer" + Berlin. Matches found by normalized title+company:

| Ad | JSearch | SerpApi | Adzuna | BA | Linking evidence |
|---|---|---|---|---|---|
| AI-Native Software Engineer – Vitaservices | `job_uid UMsYA14AEpQuse3eAAAAAA==` | `htidocid UMsYA14AEpQuse3eAAAAAA==` | 4 ids (5929759988, 5927949151, 5927683940, 5927230714) | – | same htidocid; same StepStone id `14607704` in both Google sources |
| Senior Full-Stack Engineer (AI-Native) – Acquirepad | `h7J-BlyEfi36-WT2AAAAAA==` | same | – | – | htidocid; LinkedIn id `4480111992` |
| Web Software Engineer – Accenture | `vJAfN66NGqhkJUM9AAAAAA==` | same | – | – | htidocid |
| Software Engineer – Prüf- & Messtechnik – Stolzberger | – | `QL8BL-PHYgEy_CL_AAAAAA==` | 5926833612 | – | SerpApi apply option `adzuna.de/details/5926833612` |
| Robotics Software Engineer (Senior) – RoboService | – | `3vI4_cvA9d1EaKdMAAAAAA==` | 5928754022 | – | apply option `adzuna.de/details/5928754022`; Join `16831124`; XING `158579394`; Arbeitnow `194025` |
| Software Engineer, AI model supply chain… – prefix.dev | `0jARKEP3yvf0sKPUAAAAAA==` | – | 5928024699 | – | title+company only (JSearch apply = Arbeitnow `467200`) |
| Software Engineer Plattformen & Anwendungen – KORSCH | – | `5L7w7_PaZlhxQu--AAAAAA==` | 5928305437, 5927948501 | – | title+company; StepStone `14607992` |
| Senior Software Engineer – ZEOS – Zalando | – | – | 5927925394 | 18251-33767-S | title+company ("Zalando" vs "Zalando SE") |
| Security Engineer, Product Security – Delivery Hero | – | – | 5926922653 | 20390-JR0086459-S | title+company ("Delivery Hero" vs "Delivery Hero SE") |

Usable keys:
1. **Google `htidocid`**: JSearch `job_uid` ≡ SerpApi `job_id`→base64-JSON→`htidocid`. This is an exact JSearch↔SerpApi key (3/10 overlap here). Never use JSearch `job_id` (it contains a session blob) or the full SerpApi `job_id` (contains `fc` session token).
2. **Publisher job IDs parsed from canonical URLs**: `stepstone.de/stellenangebote--…--{id}-inline.html`, `linkedin.com/jobs/view/…-{id}`, `indeed.com/viewjob?jk={id}`, `xing.com/jobs/…-{id}`, `adzuna.de/details/{id}` (= Adzuna API `id`), `arbeitnow.com/jobs/companies/{co}/…-{id}`, `join.com/companies/{co}/{id}-…`. `it-jobs.de/…_ers-{base64}` decodes to a **BA refnr** (`MTIwMTYtMTAwMDU1NDg2NDUtUyAx` → `12016-10005548645-S 1`). **Only trust the primary link (`job_apply_link`, the `via` option); see the contamination warning in §3.**
3. **Normalized title + company (+ city)** is required for BA↔others and Adzuna↔others. Needed normalizations seen: gender tags `(m/w/d)`, `(w/m/d)`, `(all genders)`, `(*)`, `(f/m/x)`, `(w_m_d)`, `(m|w|d)`; legal forms `GmbH`, `SE`, `AG`, `GmbH & Co. KG`, `PLC`; case (`vitaservices` vs `Vitaservices`); abbreviations (`Sr.` vs `Senior`: Seqana appeared as "Sr. Software Engineer (Data & Platform)" in SerpApi vs "Senior Software Engineer (Data & Platform)" in Adzuna); soft hyphens (`System­administrator` with U+00AD in Adzuna). Confirm with description similarity, which is only possible where full text exists (not Adzuna).
4. **Within-source duplicates** exist (Adzuna 4× and 2× copies; same employer posting identical titles at multiple boards), so dedup must run intra-source as well.

**Tracking params to strip for canonical URLs:** `utm_campaign=google_jobs_apply`, `utm_source=google_jobs_apply`, `utm_medium=organic` (all SerpApi apply links); BA `externeURL` `utm_source=jsba-premium&utm_medium=…&utm_campaign=jsba-premium&utm_term=…`; Adzuna `redirect_url` `utm_medium=api&utm_source=<APP_ID>` plus `se=`/`v=` (Adzuna redirect URLs are per-click tokens and should not be used as canonical URLs at all); generic `utm_*`, `gclid`, `fbclid`. Also lowercase the host, drop `www.`, drop the fragment and trailing `/`.

### 11.2 Posted-date reliability
- **Absolute, from source of truth:** ATS boards (Greenhouse `first_published`, Lever `createdAt`, Ashby `publishedAt`, SmartRecruiters `releasedDate`, Recruitee `published_at`, Personio `createdAt`), BA `datumErsteVeroeffentlichung` (date only) + `aenderungsdatum`, Arbeitnow `created_at` (when Arbeitnow ingested), Remotive `publication_date` (+24 h delay).
- **Absolute but ingestion time:** Adzuna `created` (re-ingests produce new ids and new dates).
- **Relative / derived:** SerpApi (German relative strings only, with `hl=de`); JSearch (relative string + derived timestamp truncated to day/hour).
- Recommendation: store `first_seen_at` (our crawl time) for every source and treat provider dates as hints.

### 11.3 Full descriptions for LLM scoring
Full: JSearch, SerpApi (mostly; 1/10 was 56 chars), BA details (native ads only), Arbeitnow, Remotive, Greenhouse, Lever, Ashby, Recruitee, Personio, SmartRecruiters (per-posting call).
Snippet only: Adzuna (500 chars), BA search (none), BA details for external-board ads (~300 chars), Careerjet (excerpt), Jooble (snippet, UNVERIFIED).
Also add a quality gate for very short or garbage text (e.g. 56-char description; literal `"null"` lines).

### 11.4 Attribute availability
| Attribute | Structured in |
|---|---|
| Remote/hybrid/on-site | Lever `workplaceType`; Ashby `isRemote`/`workplaceType`; SmartRecruiters `remote`/`hybrid`; Recruitee `remote`/`hybrid`/`on_site`; BA `homeofficemoeglich`/`homeofficetyp`/`homeofficeprozent`; Arbeitnow `remote`; JSearch `job_is_remote` (remote only, no hybrid); SerpApi only `"Homeoffice"` string (hl=de); Adzuna, Greenhouse, Personio: none |
| Ad language | Greenhouse `language`, SmartRecruiters `language`, Recruitee `language_code` (null in sample); all others → detect from text |
| Salary | Adzuna `salary_min/max` (+predicted flag), BA `gehaltsspanne*`, Lever `salaryRange`, Ashby `compensation`, Recruitee `salary`, Personio `salaryInformation`; JSearch fields (null in DE sample); SerpApi string; Remotive string |
| Employment type | JSearch `job_employment_types` (normalized); Adzuna `contract_time/type` (sparse); BA `arbeitszeit*`, `befristung`, `vertragsdauer`; Lever `commitment`; Ashby `employmentType`; SR `typeOfEmployment`; Recruitee `employment_type_code`; Personio `employmentType/schedule/seniority`; Arbeitnow free text; SerpApi German string |
| Industry/category | Adzuna `category` (48% `unknown`); BA `hauptberuf`/`alleBerufe` (+`berufsfeld` facet); SR `industry`/`function`; Personio `occupation(Category)`; Arbeitnow `tags`; Remotive `category`; JSearch `job_onet_soc` (null in DE sample) |

### 11.5 Monthly budget math (JSearch 200 + SerpApi 250)
Both bill **1 unit per 10-result page**; SerpApi also bills **empty** pages. Month ≈ 30 days.

| Schedule | Runs/month | JSearch pages/run (200) | SerpApi pages/run (250) | Max Google-for-Jobs listings/month (pre-dedup) |
|---|---|---|---|---|
| every 6 h | 120 | **1** (120 used, 80 spare) | **2** (240 used, 10 spare) | 3 pages/run → 360 pages ≈ 3,600 slots |
| every 12 h | 60 | 3 (180, 20 spare) | 4 (240, 10 spare) | 420 pages ≈ 4,200 |
| every 24 h | 30 | 6 (180, 20 spare) | 8 (240, 10 spare) | 420 pages ≈ 4,200 |

Notes: (a) Both APIs read the **same Google for Jobs index** (3/10 identical htidocids even with different query wording), so slots overlap heavily and real unique yield is lower. (b) A 6-hourly cadence buys freshness, not volume. With `date_posted=today` (JSearch) or the "Gestern" `uds` filter (SerpApi), one page per run may cover all new ads for a narrow query, but a broad query will not fit. (c) Keep a reserve (≈10%) for retries and the occasional `/job-details`, which costs 1 request per job id. (d) The quota resets on subscription anniversaries (JSearch reset ≈ 2026-11-16 from headers; SerpApi `plan_renewal_date: 2026-11-08`), not calendar months. (e) SerpApi's 1-h cache never helps a ≥6 h schedule.

---

## 12. Design implications

1. **Primary sources (free, high volume, full text, legally cleaner):** company ATS boards (Greenhouse/Lever-global+EU/Ashby/Personio/Recruitee/SmartRecruiters) for a user-curated employer list, plus **Arbeitnow** (hourly feed, 300/page, filter client-side). **BA Jobsuche** has the largest German coverage and works today via `/pc/v6/jobs`, but it is unofficial, its schema drifts from the docs, details cost 1 call/job, and **BA's terms prohibit robots / data collection via interfaces**. It should be a conscious, documented opt-in.
2. **Secondary/discovery sources (quota-bound):** JSearch and SerpApi are two paid windows onto the **same** Google for Jobs index. Pick one as the main discovery channel (JSearch gives normalized employment type, a derived timestamp and the `job_uid` key directly; SerpApi gives richer `apply_options` and filter tokens). Use the other as a fallback. Put the location **in the query text**. With SerpApi, a `location` param returned empty but billed results.
3. **Adzuna** for breadth (2,500 calls/month, 50/page), with only 500-char snippets. LLM scoring needs full text, so Adzuna items are useful mainly as discovery or a dedup signal unless matched to a full-text source. Do **not** fetch adzuna.de pages to get full text (ToS risk). Resolve the "personal research" vs "commercial 14-day trial" question first. Never persist `redirect_url` as canonical (it contains the `app_id`).
4. **Skip:** Remotive (tiny, no-redistribution clause, mostly irrelevant for German on-site roles), Google Jobs Listing engine (ratings only), Careerjet (needs end-user IP/UA, excerpt only), GermanTechJobs/EURES/LinkedIn/Indeed/StepStone/XING direct (no API, or ToS bans scraping).
5. **Dedup keys (ordered):** (a) `(source, native_id)` for idempotent upserts, with JSearch `job_uid`, never `job_id`; (b) cross-source exact keys: Google `htidocid`, then publisher IDs parsed from **canonicalized primary apply URLs** (StepStone/LinkedIn/Indeed/XING/Adzuna/Arbeitnow/Join/BA-refnr). **Do not union-merge on every `apply_options` entry**: Google attaches unrelated jobs (observed ≥7 wrong links in 2/10 SerpApi results); (c) fuzzy `normalize(title)+normalize(company)+city` with German gender-tag and legal-form stripping, confirmed by description similarity (MinHash/SimHash) over a 30–45-day window; (d) intra-source dedup (Adzuna re-ingests).
6. **Quota guardrails:** read `x-ratelimit-requests-remaining` (JSearch) on every response and SerpApi `account.json` (free) before each run; persist the counters; hard-stop below a reserve. Treat empty SerpApi results as spent budget. RapidAPI gateway 404s (bad route) are not billed, but upstream errors may be (UNVERIFIED). Log query→page→cost for audit.
7. **Parsing/localization:** German relative dates (`vor N Stunden/Tagen`, `vor 1 Tag`, `Gestern`), German schedule words (`Vollzeit`, `Teilzeit`, `Homeoffice`), salary strings with NBSP and `€ … pro Jahr`, JSearch `job_location` strings like `"Berlin • über Stepstone"` / `"Beliebiger Ort"`. Consider `hl=en` on SerpApi to get `detected_extensions` (UNVERIFIED for DE).
8. **Data-quality risks:** schema drift (BA v6 vs docs; JSearch `/search` removal; SerpApi field removals in `google_jobs_listing`). Spam/mirror publishers ("Jobs For Stevenage Fans", "SaluteMyJob"). Garbage or very short descriptions. Category `unknown`. BA external ads lack full text. Possible sporadic BA 403s. Workable 429. Use contract tests built from the fixtures in `samples/`, and alert on field-presence drops.
9. **Privacy:** job descriptions in the fixtures contain recruiters' names, emails and phone numbers (public ad content). Consider scrubbing before committing fixtures to a public repo.

---

## 13. Call log (exact quota consumption)

| Source | Calls | Billed | Quota after |
|---|---|---|---|
| JSearch (RapidAPI) | 2: `/search` → gateway 404 (not billed, proven by header), `/search-v2` → 200 | **1** | `x-ratelimit-requests-remaining: 199` / 200, reset in 2,675,242 s |
| SerpApi | 2 searches (attempt 1 empty, attempt 2 = 10 jobs) + 6 Account API calls (free) + 1 archived raw-HTML fetch (no key) + 1 `locations.json` (no key) | **2** | `plan_searches_left: 248`, `this_month_usage: 2`, renewal 2026-11-08 |
| Adzuna API | 3 (search rpp=50, search rpp=100, categories) + 2 adzuna.de landing-page GETs (website, not API) | 3 API hits | not reported (Adzuna returns no quota headers); computed ≈2,497 of default 2,500/month, 247 of 250/day |
| BA | 3 diagnostic searches + 1 search + 2 jobdetails | – | n/a |
| Arbeitnow / Remotive | 1 / 1 | – | Arbeitnow `x-ratelimit-remaining: 48` of 50 |
| ATS | Greenhouse 1, Lever 1, Ashby 1, SmartRecruiters 1, Personio 2 (1×404), Recruitee 1, Workable 1 (429) | – | – |

## 14. Samples (`research/samples/`, keys redacted, email/account ids redacted)
`jsearch_search_v2.json` + `.headers.txt` (success), `jsearch_search_legacy_404.json` + `.headers.txt`, `serpapi_google_jobs_attempt1_location_empty.json` + `_raw_google.html` + `.headers.txt`, `serpapi_google_jobs_attempt2.json` + `.headers.txt`, `serpapi_account_{before,after,after_attempt2,final}.json`, `adzuna_de_search.json` + `.headers.txt`, `adzuna_de_categories.json`, `ba_jobsuche_v6_search.json`, `ba_jobdetails_v4_18251-33767-S.json` (full desc), `ba_jobdetails_v4_12336-a26f539j0456360-S.json` (external ad, short desc), `arbeitnow_page1.json` (2.4 MB) + headers, `remotive_software_dev.json`, `greenhouse_getyourguide.json`, `lever_leverdemo_global.json`, `ashby_n8n.json`, `smartrecruiters_deliveryhero.json`, `recruitee_bunq.json`, `personio_everphone.xml`.
Analysis scripts: `scratchpad/scripts/overlap.py`, `scratchpad/scripts/applyopts.py` (arg: samples dir).
