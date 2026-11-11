# Privacy statement

This statement describes the personal data a Job Hunter deployment processes: why, how it is minimised, who receives it, how long it is kept, and how you export or delete it. Job Hunter is a single-user application, and "you" in this statement is its one user.

## 1. Purpose and controller

- **Purpose.** You run Job Hunter for your own job search. On a schedule it collects German-market job ads from several sources, removes duplicates, and ranks the ads by how well they fit your active CV profile. It uses Anthropic's Claude to extract your profile from your CV and to score the ads. The app processes your data for that purpose only.
- **A single-user deployment.** One deployment serves one user. It has one account, no registration and no default credentials.
- **Controller.** The controller of the personal data processed here is named under Operator at the end of this statement. That section also names the operator who runs the deployment and gives the address for concerns.

## 2. Data categories

- **CV and profile** (personal data): the CV file you upload or the text you paste; its extracted text; the text with your contact details stripped, which you review and approve; the profile extracted from that text, which you edit and confirm; and the profile vectors computed from the profile.
- **Your actions and labels**: the jobs you mark as seen, saved, applied or hidden; your notes on jobs; your feedback on scores; and the golden cases you label for the evaluation of the scoring.
- **Job ads**: public ads collected from the sources and from the company watchlist. They may contain recruiter names and contact details. The scores, evidence and critic notes the scoring derives from an ad and your profile are kept with the ad.
- **Account and sign-in data**: your username; your password, as an Argon2id hash only; your TOTP secret, encrypted, when TOTP is on; your sessions, each stored as an HMAC of its token with the client address and a hash of the browser's user agent; and login attempts, each with the client address and an HMAC of the username, never the username itself.
- **Technical logs**: they hold no CV, profile, note or ad content and no secret. Sign-in and re-authentication events record the client address.
- **Operational records**: runs, quotas, the LLM spend ledger, alerts, backup records and deletion manifests. They hold ids, counts, times and hashes, not your content.

## 3. Processing and minimisation

- **Contact stripping and your review come first.** Before any CV text goes to an LLM or to the embedder, the app replaces e-mail addresses, phone numbers, URLs, street addresses, postal codes with their cities, date and place of birth, lines that state a protected attribute (such as marital status, nationality, religion, gender or children), and the name in the CV's header (and its later occurrences) with markers such as `[EMAIL]` and `[NAME]`. You then review the stripped text, with the replacements highlighted: you can mark further spans for removal and restore false positives. Only the text you approve is sent, and each call records its hash, which must match. The raw text never reaches the LLM or the embedder.
- **The stripping is best-effort; your review is the guarantee.** The stripper takes a name only from a header line that holds nothing but a name. A name on a mixed header line (with a job title, a degree, the word "Lebenslauf", or contact details on the same line) stays in the text, and so does contact data glued to other text (a URL glued to a word, an e-mail address glued to a URL). Check the text for these before you approve it.
- **After the extraction**, the profile is scanned again with the contact patterns. Any hit is removed and shown as a validation issue.
- **The native-PDF opt-in.** When the text extraction fails, or if you prefer it, you can choose to send the original PDF to Claude instead. The disclosure next to that choice reads: "The original file, including your name, contact details and photo, will be sent to Anthropic. Contact stripping cannot be applied." Choosing it is your explicit decision in place of the text review, and it is recorded per file. The resulting profile is still checked for contact details, and any found are removed.
- **No inference of protected attributes.** The extraction prompt forbids inferring or outputting age, date of birth, gender, nationality, citizenship, ethnicity, religion, marital or family status, health, disability, photo content or political views, and the profile has no field for any of them. Work authorisation and security clearances exist only as fields you enter yourself. Language skills are taken only from explicit statements in your CV.
- **Embeddings are computed locally.** The embedding model runs in the deployment's own embedder container, which has no route to the internet. No text goes to an embedding service.
- **Scoring.** Claude scores each ad against your confirmed profile, which holds no contact details. The ad text and the profile are sent, not your CV. The scores, their evidence and the critic's notes form the score history, which is kept as section 5 states.
- **Golden cases.** The ads you label for the evaluation of the scoring are kept until you delete them. While a case is active, its job, the job's version you labelled and the profile version it was labelled against are kept too, except by the Adzuna purge.

## 4. Third parties and transfers

These parties receive data from the app. The api, which serves the web interface, has no route to the internet: the worker (which collects ads and runs the scoring) and the backup service send what the table lists.

| Third party | What it receives |
|---|---|
| Anthropic (Claude API) | The CV text after contact stripping **and the user's review**, the confirmed profile, and ad text; the original PDF only on explicit opt-in. Anthropic's API data-retention terms apply |
| LangSmith EU | Trace metadata only, no inputs or outputs |
| RapidAPI/OpenWeb Ninja, SerpApi, Adzuna | Search queries |
| Arbeitnow and ATS hosts | GET requests |
| ntfy | Alert texts without PII |
| healthchecks.io | Pings only |
| The VPS provider, the S3 backup provider | Infrastructure (§23) |

The original PDF goes to Anthropic only when you opt in (section 3). LangSmith receives traces at its EU endpoint with their inputs and outputs hidden. The operator section below names the VPS provider and the S3 backup provider.

## 5. Retention

Retention is dependency-aware: a record is removed only after everything that refers to it is removed or detached, and an exemption protects the whole chain it depends on. The table is the retention table of Job Hunter's design specification, and its § references point there; the exports row states the rule the app follows.

| Data | Location | Retention | Purge mechanism |
|---|---|---|---|
| CV file (original) and raw CV text | `cv_store` (encrypted), `cv_files.raw_text_enc` | Files of the active and the previous profile version. Files of discarded or failed drafts and abandoned uploads: 30 days after the draft ended. | `retention_daily` / on confirm |
| Contact-stripped text | `cv_files.stripped_text` | Same as the CV file | Same |
| Draft content (`draft_json`, `reviewed_text`, and the `output_json` of the draft's `extract_profile` calls) | `profile_drafts`, `llm_calls` | 30 days after the draft is `confirmed`, `discarded` or `failed` (`content_purged_at`); the rows' ids, states and usage stay | `retention_daily` |
| Contact-stripped profile JSON | `profile_versions.profile_json` | While retained scores, retained feedback (`score_feedback.profile_version_id`) or active golden cases reference it. The row itself is deleted only by delete-everything; purging nulls its content | Profile purge (§20.2) |
| Query proposals (`items`, and the `output_json` of their `propose_queries` calls) | `query_proposals`, `llm_calls` | With the profile content; the proposal becomes `purged` | Profile purge |
| Profile vectors | `profile_vectors` + `embeddings` | With the profile content | Profile purge |
| Score history (targets, attempts, drafts, critic notes, llm_results, evidence, det instances, llm_calls) | DB | `retention.score_history_months` (default 12) after the target ended terminally, **except**: targets of the active tuple and everything they reference are never deleted by age (deleting one would make `missing_targets` re-create and re-score it); results referenced by an active golden case are kept; a target whose batch item belongs to a submission not yet resolved (`preparing`, `submit_started`, `submit_ambiguous`, `submitted`, `ended`, or `released` with `submit_started_at` younger than 29 days, the reconciler's clock) is kept with its items and calls until the submission is resolved; no age releases a submission (C3) | Target-set deletion (§20.2) |
| Superseded `det_results` instances | DB | With score history; the current instance follows its result | Same |
| Feedback | `score_feedback` | Until the user deletes it, or delete-everything. Its references are detached (`SET NULL`) when the result or job is removed; the score snapshot stays | User action |
| Golden cases | `golden_cases`, `eval_case_results`, their eval calls | Until the user deletes them (§14.2). While a case is active, **its job revision, the job and its profile version are exempt** from tombstoning and purges (the Adzuna purge excepted, §20.5). Tombstoning and the profile purge delete **inactive** cases they would otherwise orphan (§20.2) | User action (manifest) |
| Eval runs, their case results and calls (golden, canary, schema dry-run) | `eval_runs`, `eval_case_results`, `llm_calls` | 13 months after the run, except the latest completed run of each kind | `retention_daily` (calls cascade) |
| Raw provider payload bodies | `raw_responses.body_zstd` | 30 days, extended while a newer row references the body (§7.7) | `retention_daily` (body nulled) |
| `raw_items` | DB | 90 days; observations keep their provenance copy (`raw_item_id := NULL`) | `retention_daily` |
| `raw_responses` rows (metadata, body already nulled) | DB | With their fetch request (13 months) | Cascade |
| Runs, run attempts, fetch requests, score cycles, coverage intervals, operations, bulk operations | DB (metadata, no content) | 13 months (operations: 30 days after they finish) | `retention_daily` |
| Batch submissions (metadata, no content) | DB | 13 months after they ended (`reconciled`, `submit_rejected`, `abandoned`, or `released` with `submit_started_at` older than 29 days), once their items went with their targets; their bulk operation after them | `retention_daily` |
| ATS snapshots | DB | 30 days, except the latest complete one and unacknowledged mass-disappearance snapshots | `retention_daily` |
| Quota ledger, LLM spend ledger, `llm_budget`, `quota_windows` | DB (no content) | Indefinitely, so a restart can never exceed a window or the cap | — |
| `llm_tuples`, `execution_configs`, `evaluation_policies`, `model_policies` | DB (ids and settings, no personal content) | Indefinitely | — |
| Posting observations, jobs, revisions, lineage | DB | While the job is retained | With the job |
| Closed or stale jobs (not saved or applied, not referenced by an active golden case) | DB | 90 days after `jobs.availability_changed_at`, then **tombstoned**: hashed keys and user-action timestamps kept, content deleted | `retention_daily` + manifest |
| Saved or applied jobs | DB | Until the user clears saved/applied, or deletes everything | User action |
| Embeddings | `embeddings` | While a revision or profile vector references them | `retention_daily` (unreferenced rows) |
| LangGraph checkpoints | DB | 14 days after the run or cycle ends; a cycle's row goes only after its checkpoint thread | `retention_daily` (deletes threads) |
| procrastinate finished jobs | DB | 7 days | daily cleanup |
| Idempotency records | `api_idempotency` | 7 days | daily |
| Logs | Docker local driver | about 14 days (rotation) | Rotation |
| Sessions | DB | Expiry + 1 day | daily |
| Login attempts | DB | 30 days | daily |
| Alerts | DB | Resolved + 90 days | daily |
| Exports | `exports` volume | 7 days; deleted by every user-requested purge and by delete-everything, not by the nightly job tombstoning, so an export can hold content tombstoned after it was made until it expires | daily |
| Backups | S3 (restic) | Backup sets: 7 daily, 4 weekly, 3 monthly. Manifest snapshots: indefinitely | `restic forget/prune` |
| Backup sets, backup runs | DB | 13 months | daily |
| Deletion manifests, shipments, purge fences | DB + `manifests` volume + offsite | Indefinitely (ids and hashes only) | — |
| LangSmith traces | LangSmith EU | Per the LangSmith project retention setting (no I/O content) | LangSmith console |

The automatic purge of an old profile version's content deletes every export too, as every purge does except the nightly job tombstoning (section 6).

## 6. Deletion and export

**Export.** You can request an export of your data from the Privacy & data page. It is a zip archive with these files:

| File | What it holds |
|---|---|
| `profile_versions.json` | Your confirmed profile versions that are still kept, without your contact details, and the synthetic profile versions of the scoring's evaluation, each marked `"synthetic_eval": true` |
| `cv_files/` | The CV files you uploaded that still exist, decrypted (a pasted CV as a text file) |
| `settings_versions.json` | Every saved version of your settings |
| `jobs.json` | The job ads with their canonical fields, your actions and your notes |
| `scores.json` | Every published score, current and historical, with its evidence, the critic's notes and the versions it was made with |
| `feedback.json` | Your feedback on scores |
| `golden_cases.json` | The golden cases you labelled |
| `runs.json` | The collection runs with their reports |
| `README.txt` | What each file holds |

- **Encrypted at rest.** On the server the archive exists only encrypted with the data key (AES-256-GCM, in segments of 1 MiB). Its plaintext exists only in the worker's memory-backed `/tmp` while the archive is being built.
- **Download.** You can download it for 7 days, over TLS and while logged in, and each download is logged with the export's id. The downloaded zip itself is not encrypted or protected by a passphrase, so keep it safe. After the 7 days it is deleted.
- **Purges delete it.** Every privacy purge (delete everything, the Adzuna purge, deleting a golden case, and the automatic purge of an old profile version) deletes every existing export at once, because an export may hold the deleted data. The nightly tombstoning of old job ads does not, so an export can hold an ad tombstoned after the export was made, until the export expires. An export is not built while a purge is running: it waits, and fails if the purge has not finished within a day, so it never holds data a purge is deleting.
- **Never in a backup.** An export archive is never copied into a backup, so a restore does not bring it back.

**Delete everything.** On the Privacy & data page, delete everything removes all your content.

- **Confirmation.** You type `DELETE EVERYTHING` and re-enter your password and, when TOTP is on, a current code. A failed re-authentication counts as a failed login attempt (section 9).
- **What it deletes.** Your CV files and texts, your profile versions, drafts and vectors, every job ad with its history and every tombstone, every score, your feedback and golden cases, the evaluation runs, the query proposals and queries, the watchlist, the collection runs with their raw data, the settings versions, the resolved alerts, the export archives, the login attempts and lockouts, and every session that exists when it starts. Yours ends too: you are logged out at the end.
- **What it keeps.** The account with its TOTP enrolment, so you can still log in, and records that hold no content of yours: the quota and LLM spend ledgers, so a restart can never exceed a provider's window or the spending cap; the configuration, with the settings reset to the defaults; the backup records; and the deletion manifests with their shipments and fences (ids and hashes only). A few operational records without content stay too: the alerts open when it ran, its own operation and request record, the background jobs not yet finished, and the record of each export without its archive.
- **While it runs**, nothing new starts, and that stop survives a restart.

**The Adzuna purge** removes everything derived from Adzuna (section 7).

**Your other deletions.** Deleting a golden case removes it, with its evaluation results, under a deletion manifest. Deleting feedback removes it. Clearing saved or applied returns a job to normal retention. Everything else follows the retention table (section 5): for example, a closed or stale job you have not saved or applied is tombstoned 90 days after it closed (its content deleted; its hashed keys and your action timestamps kept, so a re-posted ad keeps its hidden or applied state), and an old profile version's content is purged once nothing retained refers to it.

**When a deletion is done.**

- **Manifests.** Every purge first records a deletion manifest, which holds ids, table and column names and key hashes, never content. The manifests are shipped to the offsite backup storage every 5 minutes.
- **Confirmed offsite.** A deletion counts as done only when the app shows it "confirmed offsite": its manifest is shipped, so no restore can bring the deleted data back.
- **Backups.** Older backup sets still hold the deleted data until they age out, within about 3 months (backup sets are kept as 7 daily, 4 weekly and 3 monthly ones).
- **Restore.** A restore replays the shipped manifests before the app starts again, so the manifest replay keeps deleted data from returning on a restore.

## 7. Adzuna

- **Personal-use terms.** Adzuna's job search API is used under Adzuna's personal-use terms. The app keeps its use within the terms' limits of 25 requests a minute, 250 a day, 1,000 a week and 2,500 a month, which the settings cannot exceed, and by default below them: 20, 225, 900 and 2,250, one request at a time. It stores Adzuna's 500-character description snippets as partial descriptions and never fetches Adzuna's detail pages.
- **Attribution.** Every ad from Adzuna is shown with the **Jobs by Adzuna** attribution badge, at least 116×23 px, linked to the ad's page at Adzuna. That link carries the app's Adzuna id, so it is used only for the badge and is never logged or exported.
- **What Adzuna receives**: search queries (section 4).
- **The Adzuna purge.** When the Adzuna agreement ends, the Adzuna purge removes Adzuna's data. You type `PURGE ADZUNA` and re-enter your password and, when TOTP is on, a current code. A failed re-authentication counts as a failed login attempt (section 9). The purge is refused while Adzuna is the only enabled source: enable another source first.
- **What the purge does.** It disables the Adzuna source and cancels the running collections. It deletes every Adzuna posting, the Adzuna keys, and the Adzuna fetch requests with their raw responses and items. It deletes the scores, evidence and deterministic results derived from Adzuna data, and every golden case that draws on it, active or not (the agreement overrides the exemption, and the app lists the deleted cases). It deletes every export. It rebuilds each affected job from its remaining postings, and deletes the jobs that had only Adzuna postings, without tombstones. The quota ledger keeps its rows, which hold no content.
- **Afterwards**, no Adzuna-derived text, salary or score remains in the database, in exports or in any future restore. The purge is done when the app shows it "confirmed offsite" (section 6). Adzuna stays disabled until a new settings version enables it again, and data collected after that is not covered by the earlier purge.

## 8. GeoNames

- **Attribution.** Contains data from GeoNames (geonames.org), CC BY 4.0 (`https://creativecommons.org/licenses/by/4.0/`).
- **Changes.** The data was changed: it was filtered, names were folded (accents removed, case-folded) and extended with umlaut transliterations, postal codes were attached to places, and the main name of Munich and Nuremberg was replaced by the German one.
- **Use.** The app resolves the locations of job ads with this offline gazetteer, which is built into the application image. No request goes to GeoNames while the app runs.

## 9. Security measures

In summary (section 16 of the design specification has the detail):

- **TLS.** The app is served over HTTPS only, with HSTS.
- **Browser.** A strict Content Security Policy allows scripts, styles and connections from the app's own origin only and forbids framing. Ad HTML is sanitised when it is collected and shown as text.
- **Passwords.** Passwords are stored as Argon2id hashes, are at least 12 characters long, and are not one of the 10,000 most common. TOTP is optional.
- **Sessions.** A 256-bit random token in a `__Host-` cookie (`Secure`, `HttpOnly`, `SameSite=Strict`), stored only as an HMAC. A session ends after 12 hours idle and after 30 days at most. Each login issues a new token, and a password change ends every session.
- **CSRF.** Every request that changes state must come from the app's own origin and carry a custom header. No cross-origin access is allowed.
- **Throttling.** 5 failed attempts within 15 minutes lock the address out for 15 minutes, and 20 within 1 hour lock everyone out for 1 hour and raise a critical alert. A failed re-authentication for delete everything or the Adzuna purge counts as a failed attempt.
- **The api without egress.** The api has no route to the internet, so a compromise of the internet-facing api exposes no provider key and cannot send data out directly. CV files are parsed in a container without any network.
- **Secrets.** Each service holds only the secrets it needs: the api holds no provider, Anthropic, LangSmith, alerting or backup credential. Secrets are never written to the database, logs, traces or images.
- **Encryption at rest.** The server's disk is encrypted with LUKS where the VPS provider offers it. The app encrypts CV files, raw CV texts, the TOTP secret and exports with AES-256-GCM under its data key. Backups are encrypted by restic.
- **The offline recovery kit.** The data key is never stored in the database or in backups. It is kept in the deployment's secret store and in an offline copy held by the user, the offline recovery kit.
- **Logs.** No log line holds a secret or any CV, profile, note or ad content. A redaction processor masks e-mail addresses, phone numbers and key-shaped strings, and the database never logs statement text or bind parameters.
- **LLM boundaries.** The LLM has no tools and no side effects. Ad text is passed to it as untrusted data, and suspected prompt injection is flagged.

## 10. How to report a concern

- **Contact.** Send any concern or request about how this deployment handles your personal data to the operator's contact address under Operator below. The operator sets it at deployment.
- **What you can do yourself.** The Privacy & data page offers the export, delete everything and the Adzuna purge, and shows the state of each deletion.

## Operator

The operator fills in these fields at deployment.

- **Operator:** [set at deployment: operator name]
- **Contact address:** [set at deployment: contact address]
- **Controller:** [set at deployment: controller name]
- **VPS provider:** [set at deployment: VPS provider]
- **S3 backup provider:** [set at deployment: S3 backup provider]
