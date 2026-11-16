# M5 plan, fix round 11: the plan author's response to Codex round 11

- **Review:** `docs/council/plan-reviews/m5-round11-codex.md` (REJECT; all round-10 findings resolved, 1 new MAJOR finding). This round follows the coordinator's ruling R11-1.
- **Plan:** `docs/superpowers/plans/2026-11-09-m5-operations-privacy-production.md`, edited in place on top of a388a2a (HEAD 23910e2 adds only the review). It now has 1,122 lines. Not committed.
- **References:** `P:n` is a line of the edited plan.

The finding holds against the code at HEAD: 1 fixed, 0 rebutted. M5-U1–U4 and M5-R1–R8 stay binding.

## Finding 1 (R11-1): on a fresh install, the queue's sequences are created after both advancement passes

What I verified:
- `MIGRATE_ORDER` (`db/migrate.py:38-46`) runs the advancement in steps 1 (`reconstruct_and_advance`) and 3 (`advance_again`).
- `queue_and_checkpointer`, step 5, then calls `_setup_queue` and `setup_checkpointer` (`:102-104`), which create procrastinate's tables, among them `procrastinate_jobs` with a `bigserial` id (`procrastinate/sql/schema.sql:65-66`), and the checkpointer's.
- On a first installation over a manifest volume, that sequence therefore starts at 1.
- Spec §19.3 (L6197-6203) puts the advancement in steps 1 and 3 only.

What changed:
- **Step 5 keeps its name and its place.** It now ends with the same idempotent `advance_identities(conn, await manifest_bounds(conn, manifest_dir))` over every table, once its schemas exist (Task 23, P:733). That is Task 23's function: identity and `serial` columns are moved past every manifest bound and every listed id. It runs before `seed` and `replay`, and nothing allocates an id between the schemas' creation and the pass.
- **Spec 3.7 item 14 gains the extra pass** at step 5 (L6201) and states that steps 1 and 3 cover `serial` columns and listed ids (P:938).
- **Deviation 38 records it** (P:1050).
- **Test:** `test_a_first_installation_advances_the_queue_past_listed_job_ids` in `test_migrate_paths.py` (P:736). It is Codex's scenario:
  - a fresh, empty database and a `manifests` volume whose delete-everything manifest lists `procrastinate_jobs` ids above 1;
  - `migrate`;
  - newly enqueued jobs get ids above the largest listed id;
  - a second `migrate` classifies the manifest `satisfied`, with no replay and no configuration change.

## Sweep: tables created outside alembic

Read from procrastinate's `schema.sql`, the checkpointer's setup statements, the migrations and the init script:

| Table | Id column | Named by a manifest? | Result |
|---|---|---|---|
| `procrastinate_jobs` | `bigserial` | Yes: delete-everything's frozen id list | **Finding 1, fixed.** Advanced at the end of step 5 (and in steps 1 and 3 when it already exists, as after a restore). |
| `procrastinate_events` | `bigserial` | No: it cascades with its job | No bound, so the pass leaves it as it is. Its ids can never meet a manifest. |
| `procrastinate_periodic_defers` | `bigserial` | No: procrastinate's own trigger unlinks it on a job's deletion | Nothing to do. |
| `procrastinate_workers` | identity | No | Nothing to do. |
| `checkpoints`, `checkpoint_writes`, `checkpoint_blobs` | none (text keys) | Yes, by the `thread` bound | Nothing to advance. Their bound is the owner's id (a run, an operation or a cycle), and those owner tables are alembic's, advanced in steps 1 and 3. |
| `checkpoint_migrations` | `v integer`, no sequence | No, kept | Nothing to do. |
| `alembic_version` | none | No | Nothing to do. |
| the extensions' objects (`vector`, `pg_trgm`, `unaccent`) and the `jobhunter_template` database (init script) | none in `public`'s application ids; the template is another database | No | Nothing to do. |

## Sweep: places that allocate an id between a schema's creation and its advancement

| Step | Allocates? | Result |
|---|---|---|
| step 2 `alembic` | No row is inserted: no migration contains an `INSERT INTO`. | Safe. |
| step 2, 0010's new identity columns on an upgrade (`api_idempotency.id`, `login_lockouts.generation`) | They number the existing rows. | Safe: no manifest written before 0010 can name those columns, and step 3 advances them before anything else. |
| step 4 `grants` | No rows. | Safe. |
| step 5 | Schema only. The checkpointer's `checkpoint_migrations` rows have no sequence. | Safe; the advancement now closes the step. |
| step 6 `seed` | Inserts rubric, prompt-schema, sources, the default execution config, evaluation policy and `ranking_state`. | Safe: all are alembic tables, advanced in step 3. |
| step 7 `replay` | Inserts rows. | Safe: it runs after every pass. |
| outside `migrate`: the worker and api | Start only after `migrate` completed (`depends_on`). | Safe. |
| outside `migrate`: `restore run` | Loads ids from the dump, then runs every step. | Safe. |
| outside `migrate`: the monthly restore test | Restores the dump, procrastinate's tables included, then runs `sequences`, `alembic` and `replay`, so step 1 advances them. | Safe. |

No further defect was found.

## Line count and sweep fixes

The plan now has 1,122 lines. The sweeps made 0 fixes beyond the finding itself.
