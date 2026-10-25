# V4b: which generator wire schema for prompt-schema version 2 compiles (M3 pre-plan probe)

## Assumption

V4 found that the API rejects the §11.5 generator schema (prompt-schema version 1) with HTTP 400
`invalid_request_error`, "The compiled grammar is too large…" (`docs/spikes/V4.md`, M2 plan Deviation 33).
`messages.count_tokens` does not compile the grammar, so only a message call shows whether a schema compiles.

M3 must ship a generator schema that compiles. The M3 scoring research (A3 §3.6) drafted three candidate wire
schemas, and the user ruled on how to choose between them:
- probe compilation live on Sonnet before the M3 plan is finalised;
- the plan uses the smallest change that compiles, so A if both A and C compile.

## The candidates

`scripts/spikes/generator_v2_candidates.py` builds the three schemas from the v1 schema
(`llm/schemas/generator.py`, as `schemas.transformed` exports it):

- **A** is v1 with `criteria` as an array of one generic `$defs.criterion` item. The item gains a
  `criterion` key enum with the six §11.2 keys. Everything else is v1.
- **B** is A, plus two changes:
  - the remaining vocabulary enums become plain strings: `work_mode.value`, `remote_region.value`,
    `lang_code`, `cefr`, `seniority.value`, `employment_types.values[]`, `degree.level` and
    `profile_ref.section`;
  - the nullables become sentinels: `nullable_string` becomes a `string` (`""` for none) and
    `nullable_int` an `integer` (`-1` for none).

  Only `criterion`, `basis` and `level` stay constrained.
- **C** is A3 §3.6's full JSON: generic fact items (`code`, `value`, `qualifier`, `required`, `evidence`)
  and generic criterion items.

Each candidate is a fixed point of `anthropic.transform_schema`, the transform the gateway applies to a node
schema, so the request sends it byte for byte. The probe passed each one as the gateway passes a mirror, so
no `title` was added.

The counts come from the module's structural walk. Every count but the canonical bytes is taken on the
ref-expanded schema, with every `$ref` inlined. `tests/unit/test_generator_v2_candidates.py` pins them to
A3 §3.4's table, and the walk reproduces A3's rows of the four v1 schemas. All four rows below match A3
exactly.

| Schema | Canonical bytes | Ref-expanded bytes | Objects | Properties | Arrays | Enum positions (values) | `anyOf` unions | Free strings | Depth |
|---|---|---|---|---|---|---|---|---|---|
| generator v1 (§11.5) | 5,621 | 8,824 | 29 | 105 | 32 | 25 (154) | 11 | 42 | 5 |
| A | 5,386 | 5,359 | 18 | 65 | 23 | 11 (75) | 6 | 27 | 5 |
| B | 4,653 | 4,542 | 18 | 65 | 23 | 3 (14) | 1 | 35 | 5 |
| C | 1,713 | 1,613 | 4 | 19 | 5 | 4 (30) | 1 | 9 | 5 |
| *profile v1 (§12.4), accepted in V4* | 2,906 | 2,906 | 7 | 35 | 8 | 9 (56) | 11 | 17 | 4 |

All five schemas have 0 optional properties.

## Method

The probe is the spike `v2probe`, defined in `scripts/spikes/v4b_generator_v2_probe.py`. The budgeted
recorder sent it (`scripts/spikes/record_anthropic.py --spike v2probe`):
1. `--dry-run` first: it built the three requests offline, the payloads were all `ok`, and the budget line
   was in range.
2. Then `--live`, on 2026-10-25.

Each call has:
- model `claude-sonnet-5-5`, effort `low`, `max_tokens` 1,024 and purpose `generate`;
- one synthetic system block of 225 bytes;
- one synthetic user turn of 436 bytes: "Rate this synthetic job ad against this synthetic profile.", with an
  invented 3-line ad and an invented 3-line profile, and no names, employers or contact data;
- the candidate as `output_config.format.schema`.

Compilation depends on the schema only, so the output was not judged.

**The outcomes.** The spike's check decides each one:
- HTTP 200, with any stop reason, the requested model served and the candidate sent byte for byte, is
  **accepted**;
- HTTP 400 `invalid_request_error` with the grammar-too-large message is **rejected**;
- any other outcome stops the spike.

**The ladder** is the spike's own `wanted(case, verdicts)`, which the recorder asks before each send:
1. A is sent first.
2. B is sent only if A is rejected.
3. C is sent if B is rejected too, or once if A is accepted.
4. The ladder stops at the first accepted candidate.

That is at most 3 sends. The spike is raw-only (`RAW_ONLY`), so no recording is committed. The raw request,
the response and the check stay in the gitignored `.fixtures-raw/anthropic/v2probe/`.

## Observed result

| Candidate | Model | Answer | Verdict | Duration (sent to settled) | Usage in / out | Settled |
|---|---|---|---|---|---|---|
| A | `claude-sonnet-5-5` | **HTTP 400** `invalid_request_error` | **rejected** | 0.47 s | (not billed) | $0.000000 |
| B | `claude-sonnet-5-5` | **HTTP 400** `invalid_request_error` | **rejected** | 0.19 s | (not billed) | $0.000000 |
| C | `claude-sonnet-5-5` | **200** `end_turn` | **accepted** | 7.15 s | 1,481 / 937 | $0.012332 |

For both rejections the API's message was the one V4 recorded:

> The compiled grammar is too large, which would cause performance issues. Simplify your tool schemas or
> reduce the number of strict tools.

Checks for every call: the sent schema was byte-equal to the candidate (canonical JSON) and carried no
`title`. For C, the served model was the requested one. C's output was complete JSON, and every value is
allowed by C (`schema_dry_run.schema_violations`: no violation path). C's 937 output tokens fit within the
1,024 limit.

What this shows:
- **B is rejected like A.** Removing 8 of A's 11 enum positions and 5 of its 6 unions did not bring the
  grammar under the limit. B keeps A's 18 objects and 23 arrays, so this supports A3 §3.5's reading that
  the size comes from the objects, the arrays and their nesting, not from the enums or unions.
- **C is accepted.** It is the only candidate below the accepted profile schema on objects, properties,
  arrays and unions.
- **The rejections are fast** (under 0.5 s), as V4's was. **C's first call took 7.15 s**, which includes
  compiling a new grammar and streaming 937 tokens. That matters for V5's duration measurements (A3 G28).

## Verdict

Under the user's rule (the smallest change that compiles, so A if both A and C compile), the ladder gives:

- A: rejected;
- B: rejected;
- C: accepted.

**The M3 plan uses candidate C** as prompt-schema version 2's generator wire schema. The spec changes are
A3 §3.6's for proposal C:
- §11.4: a generator code column;
- §11.5: the schema and its limits;
- §11.6: stage 1, V1, V2, the V4 repairs and the new value repairs;
- §10.6 and §11.9: how `<draft>` is rendered;
- the role template, which documents the codes.

Compilation is proven on `claude-sonnet-5-5` only. The live schema dry run of prompt-schema version 2 must
still pass before activation (M2 carry-forward, plan L2033), and a model that has never run the dry run
stays unproven (A3 G30).

## Spend

| Candidate | Input bound (tokens) | Worst case (reserved before the send) | Settled |
|---|---|---|---|
| A / Sonnet | 8,687 | $0.031958 | $0.000000 (error response, not billed) |
| B / Sonnet | 7,954 | $0.030125 | $0.000000 (error response, not billed) |
| C / Sonnet | 5,014 | $0.022775 | $0.012332 |
| **V4b** | | **$0.084858** (3 messages) | **$0.012332** |

**The budget.** The user set it at **$10.00 actual in total, for every Anthropic spike, M2's included**.
Before each send the recorder adds three amounts and refuses the send when the sum is above $10.00:
- the settled cost of the done entries;
- the worst case of every entry that is not done;
- the next call's worst case.

M2's 8-message and $0.80 worst-case caps no longer apply. The per-spike call counts stay, with v2probe at 3.

The ledger after the run, `.fixtures-raw/anthropic-budget.json`, holds 10 entries: M2's 7 and V4b's 3. All
10 are done.

| | Amount |
|---|---|
| Settled, M2 (V13, V4, V19) | $0.106520 |
| Settled, V4b | $0.012332 |
| **Settled in total** | **$0.118852** |
| Worst case of entries not done | $0.000000 |
| **Remaining under $10.00** | **$9.881148** |
