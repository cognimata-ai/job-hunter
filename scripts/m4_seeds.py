"""The M4 seeds (plan M4 Task 32): the rows and the standing fake-LLM answers that the E2E scenarios need and
that a collection of the fixtures does not make. `scripts/e2e_setup.py` (steps 5, 7 and 8) and
`scripts/smoke_m4.py` share them; each is idempotent.

Each seed that reads or writes the database takes `run_sql`, a callable from one SQL string to its output as
`psql -At` prints it: the rows of a statement that returns rows, one per line, their fields joined by `|`,
and the command tag of each statement that returns none (`BEGIN`, `SET`, `COMMIT`). The default, `stack_sql`,
is `smoke_m0.psql`'s stdout: the bootstrap role in the running dev stack's `postgres` container. The SQL is
this module's constants (each tagged `/* m4_seeds.<name> */`); the only values interpolated into it are
integers. A text that may hold a line break or a `|` (a description) is read as one JSON line.

- `seed_merge_pair(run_sql=…) -> (group_id, representative_job_id, member_job_id) | None` (E9): nothing when
  an unreversed `merge_events` row of kind `user_confirmed_merge` exists (the confirm dissolves the group and
  deletes its membership rows, so no seed marker outlives it); else the active group this seed made
  (`evidence = {"e2e_seed": true}` on its membership rows) when there is one; else a new active group of two
  unmerged, ungrouped jobs of one company, the smallest `company_key` with two such jobs and no Adzuna
  posting (the lowest two job ids), the job with the longer description its representative: the rows
  `tests/support/m3.insert_group` and `tests/support/scoring.member_group` write (`duplicate_groups` with
  `status 'active'`, `election_reason 'initial'`; a `duplicate_group_members` row each;
  `jobs.duplicate_group_id`).
  The caller confirms it (`POST /api/duplicate-groups/{id}/confirm`), so E9 has a merge event to unmerge.
- `grounded_answers(job_revision_id, description_text, profile_quote, *, trigger_critic=True)` (pure): each
  canonical answer key (`fake.answer_key`, `<purpose>:<revision>:<round>`) to its SSE bytes, built with M3's
  test builders (`tests.support.llm.synthetic_sse`, `tests.support.validation.synthetic_wire_c` and
  `criterion_item`, `tests.support.scoring.synthetic_review` and `review_item`), each served by the seeded
  model policy's model of its purpose. `generate:<rev>:0` is a candidate C answer with no facts, every
  criterion `observed` at level 3 with one of the description's quotes (`grounded_quotes`) as its ad evidence
  and `profile_quote` (the profile's first skill name, section `skills`) as its profile evidence, and a
  `summary_en` of 61 words: the V1 issue `too_many_words` makes `DraftValidation.t1` true, so the mandatory
  trigger T1 sends the draft to the critic, while every criterion stays valid (a clean draft fires no
  trigger and publishes `accepted` without a critic). `critic:<rev>:1..3` is a review with verdict `revise`
  that asks for `DISPUTED_CRITERION` at level 2 (`level_too_high`), quoting the first quote;
  `revise:<rev>:1..2` is the generator answer with that criterion at level 2 and a summary within the
  limits. The routing runs critic, revise, critic, revise, critic and publishes `contested` after the
  second revision: three critic notes, two rejected drafts, the criterion's levels 3, 2, 2. With
  `trigger_critic=False` it is the generator answer alone, with a 40-word summary.
- `write_grounded_answers(job_id, *, run_sql=…) -> quotes` reads the job's current revision, its description
  and the active profile version's first skill name, and writes each answer of `grounded_answers` to the
  worker's keyed directory (`JOBHUNTER_FAKE_LLM_KEYED_DIR`, `deploy/compose.e2e.yaml`) under its
  `fake.keyed_answer_filename` (`write_keyed_answers`, one `docker compose exec -T worker sh -c …` for a set
  of answers: a tar unpacked into a staging directory of the keyed directory, then each file renamed into
  place, so a try reads the old answer or the new one, never a partial file). A file that is there with the
  same bytes is not written again (`grounded_files_present`).
- `grounded_job_id(run_sql=…) -> int` (E9's job, for the scenarios): the lowest grounded candidate
  (`GROUNDED_JOB_SQL`: a retained Greenhouse `getyourguide` job of completeness `full` whose description has
  three quotes) whose current revision has all six grounded answer files in the worker's keyed directory and
  a published result, of any tuple, whose description evidence is a non-empty subset of its grounded quotes.
  No target state is read, so the rule holds after a profile v2 changed the tuple; `find_grounded_job` is the
  same, `None` for none.
- `seed_rejected_item(run_sql=…) -> (raw_response_id, raw_item_id)` (E17: the fixtures reject no item): the
  ids of the seed when a fetch request with `spec ? 'm4_seed'` exists; else, in one transaction with
  `SET LOCAL session_replication_role = replica` (this seed only: it inserts a fetch request and its
  response, which reference each other), a copy of the newest succeeded Arbeitnow page-1 fetch request
  (`attempt_seq = 8`, `spec || {"m4_seed": true}`) that succeeded with a new `raw_responses` row referencing
  the original's body (`body_ref_id`, the same `body_sha256` and `item_count`, `fetched_at = now()`,
  `expires_at = now() + 30 days`, parsed), and one `raw_items` row for item 0 (the original item 0's
  `item_sha256`, `rejected` with `identity_error`). The three inserts are made only for a page with an item 0:
  when the newest page has none, nothing is written and the seed fails naming that page. The current parser
  reads that body, so a replay moves the item `rejected -> pending` and the replay's resolution links it.

Nothing here prints. A failure raises `smoke_m0.CheckFailed` naming ids, states and counts, never ad text, a
key or a secret.
"""

import copy
import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import smoke_m0  # scripts/ is the script's directory, the first entry of sys.path
from smoke_m0 import DOCKER_TIMEOUT_S, CheckFailed, stderr_tail
from smoke_m3 import RETAINED_JOB_SQL  # a current revision of a retained job (`jobs j`)

REPO: Final = Path(__file__).resolve().parents[1]
# The E2E stack (plan M4 Global Constraints): the dev stack, both fakes and the E2E overlay, one project.
DC: Final = [
    "docker",
    "compose",
    "--env-file",
    str(REPO / "deploy/images.lock"),
    "-f",
    str(REPO / "deploy/compose.dev.yaml"),
    "-f",
    str(REPO / "deploy/compose.fixtures.yaml"),
    "-f",
    str(REPO / "deploy/compose.fakellm.yaml"),
    "-f",
    str(REPO / "deploy/compose.e2e.yaml"),
]
# The command tags `psql -At` prints for a statement of a transaction script that returns no rows.
_COMMAND_TAGS: Final = frozenset({"BEGIN", "SET", "COMMIT", "ROLLBACK"})

SqlRunner = Callable[[str], str]


def stack_sql(sql: str) -> str:
    """`sql` as the bootstrap role on the dev stack (`smoke_m0.psql`): its stdout; a failure raises."""
    process = smoke_m0.psql(sql)
    if process.returncode != 0:
        raise CheckFailed(f"psql exit {process.returncode}: {stderr_tail(process)}")
    return process.stdout


def rows(output: str) -> list[list[str]]:
    """The rows of `run_sql`'s output: its non-empty lines without the command tags, split at `|`."""
    return [line.split("|") for line in output.splitlines() if line and line not in _COMMAND_TAGS]


def one_row(output: str, columns: int, *, what: str) -> list[str] | None:
    """The one row of `output` (`None` for none), checked to have `columns` fields."""
    found = rows(output)
    if not found:
        return None
    if len(found) != 1 or len(found[0]) != columns:
        raise CheckFailed(f"{what}: expected one row of {columns} fields, got {len(found)} row(s)")
    return found[0]


def json_line(output: str, *, what: str) -> Any:
    """The one JSON value `output` holds (a statement that selects one JSON text)."""
    found = [line for line in output.splitlines() if line and line not in _COMMAND_TAGS]
    if len(found) != 1:
        raise CheckFailed(f"{what}: expected one JSON line, got {len(found)}")
    return json.loads(found[0])


def ints(fields: list[str], *, what: str) -> list[int]:
    try:
        return [int(field) for field in fields]
    except ValueError:
        raise CheckFailed(f"{what}: expected integers, got {len(fields)} field(s)") from None


# ---- E9: the merge pair ------------------------------------------------------------------------------------

MERGE_DONE: Final = (
    "/* m4_seeds.merge_done */ SELECT count(*) FROM merge_events "
    "WHERE kind = 'user_confirmed_merge' AND reversed_by_event_id IS NULL"
)
SEED_MARKER: Final = '{"e2e_seed": true}'
SEEDED_GROUP: Final = f"""\
/* m4_seeds.seeded_group */ SELECT g.id, g.representative_job_id,
  min(m.job_id) FILTER (WHERE m.job_id <> g.representative_job_id)
FROM duplicate_groups g JOIN duplicate_group_members m ON m.group_id = g.id
WHERE g.status = 'active' AND g.id IN (SELECT group_id FROM duplicate_group_members
  WHERE evidence @> '{SEED_MARKER}'::jsonb)
GROUP BY g.id ORDER BY g.id LIMIT 1"""  # noqa: S608 (constants only)
SEED_MERGE_PAIR: Final = f"""\
/* m4_seeds.merge_pair */ WITH eligible AS (
  SELECT j.id, j.company_key, length(coalesce(r.description_text, '')) AS described
  FROM jobs j JOIN job_revisions r ON r.id = j.current_revision_id
  WHERE {RETAINED_JOB_SQL} AND j.duplicate_group_id IS NULL
), company AS (
  SELECT e.company_key FROM eligible e
  WHERE NOT EXISTS (SELECT 1 FROM jobs o JOIN postings p ON p.job_id = o.id
    JOIN source_instances s ON s.id = p.source_instance_id
    WHERE o.company_key = e.company_key AND s.source_id = 'adzuna')
  GROUP BY e.company_key HAVING count(*) >= 2 ORDER BY e.company_key LIMIT 1
), pair AS (
  SELECT e.id, e.described FROM eligible e JOIN company c ON c.company_key = e.company_key
  ORDER BY e.id LIMIT 2
), rep AS (
  SELECT id FROM pair ORDER BY described DESC, id LIMIT 1
), g AS (
  INSERT INTO duplicate_groups (representative_job_id, status, election_reason)
  SELECT id, 'active', 'initial' FROM rep RETURNING id
), m AS (
  INSERT INTO duplicate_group_members (group_id, job_id, evidence)
  SELECT g.id, pair.id, '{SEED_MARKER}'::jsonb FROM g CROSS JOIN pair RETURNING job_id
), u AS (
  UPDATE jobs SET duplicate_group_id = g.id FROM g WHERE jobs.id IN (SELECT id FROM pair) RETURNING jobs.id
)
SELECT g.id, (SELECT id FROM rep), (SELECT min(id) FROM pair WHERE id <> (SELECT id FROM rep)),
  (SELECT count(*) FROM m), (SELECT count(*) FROM u)
FROM g"""  # noqa: S608 (constants only)


def seed_merge_pair(run_sql: SqlRunner = stack_sql) -> tuple[int, int, int] | None:
    """E9's group (module docstring): `(group_id, representative_job_id, member_job_id)`, or `None` when a
    user-confirmed merge stands already."""
    (merged,) = ints(one_row(run_sql(MERGE_DONE), 1, what="merge events") or ["0"], what="merge events")
    if merged:
        return None
    seeded = one_row(run_sql(SEEDED_GROUP), 3, what="the seeded group")
    if seeded is not None:
        group, representative, member = ints(seeded, what="the seeded group")
        return group, representative, member
    made = one_row(run_sql(SEED_MERGE_PAIR), 5, what="the merge pair")
    if made is None:
        raise CheckFailed("no company has two unmerged, ungrouped jobs and no Adzuna posting")
    group, representative, member, members, grouped = ints(made, what="the merge pair")
    if (members, grouped) != (2, 2):
        raise CheckFailed(f"group {group}: {members} membership rows and {grouped} grouped jobs, not 2 and 2")
    return group, representative, member


# ---- E9: the grounded answers ------------------------------------------------------------------------------

QUOTE_MIN_CHARS: Final = 30
QUOTE_MAX_CHARS: Final = 160
QUOTES: Final = 3
_SENTENCE_END: Final = re.compile(r"(?<=[.!?])\s+")
_ELLIPSES: Final = ("…", "...")  # a quote with an ellipsis fails V3 (`quote_ellipsis`)
DISPUTED_CRITERION: Final = "role_fit"
DISPUTED_LEVEL: Final = 2
GENERATED_LEVEL: Final = 3
CRITIC_ROUNDS: Final = (1, 2, 3)
REVISIONS: Final = (1, 2)
# 61 words (`validator.SUMMARY_MAX_WORDS` is 60) in 312 characters (`SUMMARY_MAX_CHARS` is 450): T1's issue.
LONG_SUMMARY: Final = (
    "A synthetic summary of the fit: the role, the backend skills and the seniority of the profile match the "
    "ad, the domain is close, and the language needs are met. Each level rests on a quote of the ad and on a "
    "skill of the profile. This summary is exactly one word longer than the limit of sixty words, on purpose."
)
# 40 words: within every limit.
CLEAN_SUMMARY: Final = (
    "A synthetic summary of the fit: the role, the backend skills and the seniority of the profile match the "
    "ad, the domain is close, and the language needs are met. Each level rests on one quote of the job ad."
)
SUMMARY_DE: Final = "Eine erfundene Zusammenfassung: Rolle, Kenntnisse und Erfahrung passen zur Anzeige."


def grounded_quotes(description_text: str) -> list[str]:
    """The first `QUOTES` distinct sentences of 30 to 160 code points of the description, verbatim: each line
    split after `.`, `!` or `?` and white space, without an ellipsis. `ValueError` when it has fewer."""
    found: list[str] = []
    for line in description_text.split("\n"):
        for piece in _SENTENCE_END.split(line):
            sentence = piece.strip()
            if (
                QUOTE_MIN_CHARS <= len(sentence) <= QUOTE_MAX_CHARS
                and not any(mark in sentence for mark in _ELLIPSES)
                and sentence not in found
            ):
                found.append(sentence)
                if len(found) == QUOTES:
                    return found
    raise ValueError(f"the description has {len(found)} sentences of 30 to 160 code points, not {QUOTES}")


def builders() -> tuple[ModuleType, ModuleType, ModuleType]:
    """M3's test builders (`tests.support.llm`, `.scoring`, `.validation`), imported when first needed."""
    if str(REPO) not in sys.path:  # `tests` is a package of the repository root, not of scripts/
        sys.path.insert(1, str(REPO))
    from tests.support import llm, scoring, validation

    return llm, scoring, validation


def _policy_models() -> dict[str, str]:
    """The seeded model policy's model of each node (`settings_v1.json`): the model a keyed answer serves."""
    from jobhunter.db.seed import load_settings_v1

    nodes = load_settings_v1()["scoring"]["model_policy"]["nodes"]  # type: ignore[index, call-overload]
    return {purpose: str(node["model"]) for purpose, node in nodes.items()}


def grounded_answers(
    job_revision_id: int, description_text: str, profile_quote: str, *, trigger_critic: bool = True
) -> dict[str, bytes]:
    """Each answer key of E9's grounded scoring to its SSE bytes (module docstring); pure."""
    from jobhunter.llm.fake import answer_key

    llm, scoring, validation = builders()
    quotes = grounded_quotes(description_text)
    models = _policy_models()
    criteria = [
        validation.criterion_item(
            key,
            level=GENERATED_LEVEL,
            ad_evidence=(quotes[i % QUOTES],),
            profile_evidence=(("skills", profile_quote),),
        )
        for i, key in enumerate(validation.CRITERIA)
    ]
    generated: dict[str, Any] = validation.synthetic_wire_c(facts=[], criteria=criteria)
    generated["summary_en"] = LONG_SUMMARY if trigger_critic else CLEAN_SUMMARY
    generated["summary_de"] = SUMMARY_DE
    refs = {"job_revision_id": job_revision_id}

    def sse(purpose: str, body: Mapping[str, Any]) -> bytes:
        served: bytes = llm.synthetic_sse(dict(body), model=models[purpose])
        return served

    answers = {answer_key("generate", refs, 0): sse("generate", generated)}
    if not trigger_critic:
        return answers
    revised = copy.deepcopy(generated)
    revised["summary_en"] = CLEAN_SUMMARY
    for item in revised["criteria"]:
        if item["criterion"] == DISPUTED_CRITERION:
            item["level"] = DISPUTED_LEVEL
    review = scoring.synthetic_review(
        scoring.review_item(DISPUTED_CRITERION, "level_too_high", quotes[0], suggested_level=DISPUTED_LEVEL),
        verdict="revise",
    )
    for critic_round in CRITIC_ROUNDS:
        answers[answer_key("critic", refs, critic_round)] = sse("critic", review)
    for revision in REVISIONS:
        answers[answer_key("revise", refs, revision)] = sse("revise", revised)
    return answers


GROUNDED_INPUTS: Final = """\
/* m4_seeds.grounded_inputs */ SELECT json_build_object('revision_id', j.current_revision_id,
  'description', r.description_text,
  'skill', (SELECT v.profile_json -> 'skills' -> 0 ->> 'name' FROM active_config a
    JOIN profile_versions v ON v.id = a.profile_version_id WHERE a.id = 1))::text
FROM jobs j JOIN job_revisions r ON r.id = j.current_revision_id WHERE j.id = {job_id}"""
KEYED_DIR_VAR: Final = "JOBHUNTER_FAKE_LLM_KEYED_DIR"  # deploy/compose.e2e.yaml sets it on the worker
_SHA256_LINE: Final = re.compile(r"([0-9a-f]{64})\s+\*?(\S+)")


def grounded_inputs(job_id: int, run_sql: SqlRunner = stack_sql) -> tuple[int, str, str]:
    """The job's current revision id, its description and the active profile's first skill name."""
    doc = json_line(run_sql(GROUNDED_INPUTS.format(job_id=int(job_id))), what=f"job {job_id}'s inputs")
    if not isinstance(doc, dict):
        raise CheckFailed(f"job {job_id} has no current revision")
    revision, description, skill = doc.get("revision_id"), doc.get("description"), doc.get("skill")
    if not isinstance(revision, int) or not isinstance(description, str):
        raise CheckFailed(f"job {job_id} has no current revision with a description")
    if not isinstance(skill, str) or not skill:
        raise CheckFailed("the active profile version has no skill")
    return revision, description, skill


def _worker(args: list[str], *, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [*DC, "exec", "-T", "worker", *args],
        input=data,
        capture_output=True,
        check=False,
        timeout=DOCKER_TIMEOUT_S,
    )


def _failed(process: subprocess.CompletedProcess[bytes], what: str) -> CheckFailed:
    tail = " | ".join(process.stderr.decode("utf-8", "replace").strip().splitlines()[-3:])[:400]
    return CheckFailed(f"{what}: exit {process.returncode}: {tail}")


def keyed_dir() -> str:
    """The worker's keyed directory (`KEYED_DIR_VAR`); a worker without the E2E overlay fails loudly."""
    process = _worker(["printenv", KEYED_DIR_VAR])
    directory = process.stdout.decode("utf-8", "replace").strip()
    if process.returncode != 0 or not directory.startswith("/"):
        raise CheckFailed(
            f"the worker has no {KEYED_DIR_VAR} (deploy/compose.e2e.yaml): exit {process.returncode}"
        )
    return directory


def keyed_digests(directory: str) -> dict[str, str]:
    """The SHA-256 (hex) of each `.sse` file in the worker's keyed directory, by file name."""
    script = 'cd "$1" 2>/dev/null || exit 0; for f in *.sse; do if [ -f "$f" ]; then sha256sum "$f"; fi; done'
    process = _worker(["sh", "-c", script, "sh", directory])
    if process.returncode != 0:
        raise _failed(process, "the keyed answers' digests")
    found: dict[str, str] = {}
    for line in process.stdout.decode("utf-8", "replace").splitlines():
        match = _SHA256_LINE.fullmatch(line.strip())
        if match is not None:
            found[match.group(2)] = match.group(1)
    return found


def _missing(answers: Mapping[str, bytes], digests: Mapping[str, str]) -> dict[str, bytes]:
    """The answers whose file is not in the keyed directory with these bytes, by file name."""
    from jobhunter.llm.fake import keyed_answer_filename

    named = {keyed_answer_filename(key): body for key, body in answers.items()}
    return {
        name: body for name, body in named.items() if digests.get(name) != hashlib.sha256(body).hexdigest()
    }


GROUNDED_BOARD: Final = ("greenhouse", "getyourguide")
# A grounded candidate (`jobs j`, its revision `r`): a retained Greenhouse `getyourguide` job of completeness
# `full` (the description's sentences are checked in Python, `grounded_quotes`). Constants only.
GROUNDED_JOB_SQL: Final = f"""\
{RETAINED_JOB_SQL} AND j.completeness = 'full' AND EXISTS (SELECT 1 FROM postings p
  JOIN source_instances s ON s.id = p.source_instance_id WHERE p.job_id = j.id
  AND s.source_id = '{GROUNDED_BOARD[0]}' AND s.instance_key = '{GROUNDED_BOARD[1]}')"""  # noqa: S608
# Every candidate with its current revision, its description and, per published result of that revision (any
# tuple), the distinct quotes of its description evidence; one JSON array, by job id.
GROUNDED_JOBS: Final = f"""\
/* m4_seeds.grounded_jobs */ SELECT coalesce(json_agg(json_build_object('job_id', j.id,
  'revision_id', j.current_revision_id, 'description', r.description_text,
  'evidence', (SELECT coalesce(json_agg(q.quotes), '[]') FROM (SELECT json_agg(DISTINCT e.quote) AS quotes
    FROM llm_results l JOIN result_evidence e ON e.llm_result_id = l.id AND e.source_kind = 'description'
    WHERE l.job_revision_id = j.current_revision_id GROUP BY l.id) q)) ORDER BY j.id), '[]')::text
FROM jobs j JOIN job_revisions r ON r.id = j.current_revision_id WHERE {GROUNDED_JOB_SQL}"""  # noqa: S608


def grounded_file_names(job_revision_id: int) -> frozenset[str]:
    """The file names of the six grounded answers of a revision (`grounded_answers` with the critic)."""
    from jobhunter.llm.fake import answer_key, keyed_answer_filename

    refs = {"job_revision_id": job_revision_id}
    keys = [
        answer_key("generate", refs, 0),
        *(answer_key("critic", refs, n) for n in CRITIC_ROUNDS),
        *(answer_key("revise", refs, n) for n in REVISIONS),
    ]
    return frozenset(keyed_answer_filename(key) for key in keys)


def find_grounded_job(run_sql: SqlRunner = stack_sql) -> int | None:
    """E9's job (module docstring), or `None` when no candidate is grounded yet."""
    jobs = json_line(run_sql(GROUNDED_JOBS), what="the grounded candidates")
    present = set(keyed_digests(keyed_dir()))
    for job in jobs if isinstance(jobs, list) else []:
        try:
            quotes = set(grounded_quotes(str(job["description"])))
        except ValueError:
            continue
        if not grounded_file_names(int(job["revision_id"])) <= present:
            continue
        if any(isinstance(found, list) and found and set(found) <= quotes for found in job["evidence"]):
            return int(job["job_id"])
    return None


def grounded_job_id(run_sql: SqlRunner = stack_sql) -> int:
    """E9's job (module docstring); none fails loudly."""
    found = find_grounded_job(run_sql)
    if found is None:
        raise CheckFailed(
            f"no {GROUNDED_BOARD[0]} {GROUNDED_BOARD[1]} job of completeness full with three quotes has its "
            "grounded answer files and a published result with the grounded quotes as its description "
            "evidence"
        )
    return found


def grounded_files_present(job_id: int, *, run_sql: SqlRunner = stack_sql) -> bool:
    """Whether every answer of the job's grounded scoring is in the worker's keyed directory, unchanged."""
    revision, description, skill = grounded_inputs(job_id, run_sql)
    answers = grounded_answers(revision, description, skill)
    return not _missing(answers, keyed_digests(keyed_dir()))


# Into a staging directory of the keyed directory (one file system), then each file renamed into place.
_UNPACK: Final = (
    'set -e; staging="$1/.staging-$$"; mkdir -p "$staging"; tar -x -f - -C "$staging"; '
    'for f in "$staging"/*.sse; do mv -f "$f" "$1/"; done; rmdir "$staging"'
)


def write_keyed_answers(answers: Mapping[str, bytes]) -> int:
    """Each of `answers` (a key to its SSE bytes) written to the worker's keyed directory under its
    `keyed_answer_filename`, in one `docker compose exec` (a tar unpacked into a staging directory of the
    keyed directory, then each file renamed into place); a file there with the same bytes is not written
    again. How many it wrote."""
    directory = keyed_dir()
    missing = _missing(answers, keyed_digests(directory))
    if not missing:
        return 0
    packed = io.BytesIO()
    with tarfile.open(fileobj=packed, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for name, body in sorted(missing.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode = len(body), 0o644
            tar.addfile(member, io.BytesIO(body))
    process = _worker(["sh", "-c", _UNPACK, "sh", directory], data=packed.getvalue())
    if process.returncode != 0:
        raise _failed(process, f"writing {len(missing)} keyed answers")
    return len(missing)


def write_grounded_answers(job_id: int, *, run_sql: SqlRunner = stack_sql) -> list[str]:
    """The job's grounded answers written to the worker's keyed directory (module docstring); the quotes."""
    revision, description, skill = grounded_inputs(job_id, run_sql)
    write_keyed_answers(grounded_answers(revision, description, skill))
    return grounded_quotes(description)


# ---- E17: the rejected item --------------------------------------------------------------------------------

REJECTED_SEED: Final = """\
/* m4_seeds.rejected_seed */ SELECT r.id, coalesce((SELECT min(i.id) FROM raw_items i
  WHERE i.raw_response_id = r.id), 0)
FROM fetch_requests f JOIN raw_responses r ON r.fetch_request_id = f.id
WHERE f.spec ? 'm4_seed' ORDER BY f.id LIMIT 1"""
SEED_REJECTED_ITEM: Final = """\
BEGIN;
SET LOCAL session_replication_role = replica;
/* m4_seeds.rejected_item */ WITH page AS (
  SELECT f.run_id, f.run_attempt_no, f.cell_id, f.source_instance_id, f.phase, f.operation,
    f.detail_native_id, f.parent_fetch_request_id, f.request_key, f.page_no, f.request_class, f.spec,
    f.http_status, f.continuation, f.next_cursor, f.items, f.new_native_ids, f.new_postings,
    r.id AS response_id, r.source_instance_id AS response_instance_id, r.http_status AS response_status,
    r.headers, r.body_sha256, coalesce(r.body_ref_id, r.id) AS body_id, r.item_count
  FROM fetch_requests f JOIN raw_responses r ON r.id = f.raw_response_id
  JOIN source_instances s ON s.id = f.source_instance_id
  WHERE s.source_id = 'arbeitnow' AND f.status = 'succeeded' AND f.operation = 'list' AND f.page_no = 1
    AND NOT f.spec ? 'm4_seed'
  ORDER BY r.fetched_at DESC, f.id DESC LIMIT 1
), src AS (  -- the page with its item 0: no item 0, no row, so none of the three inserts writes
  SELECT page.*, i.item_sha256 FROM page
  JOIN raw_items i ON i.raw_response_id = page.response_id AND i.item_index = 0
), ids AS (
  SELECT nextval(pg_get_serial_sequence('fetch_requests', 'id')) AS request_id,
    nextval(pg_get_serial_sequence('raw_responses', 'id')) AS response_id
), fr AS (
  INSERT INTO fetch_requests (id, run_id, run_attempt_no, cell_id, source_instance_id, phase, operation,
    detail_native_id, parent_fetch_request_id, request_key, attempt_seq, page_no, request_class, spec, status,
    http_status, raw_response_id, continuation, next_cursor, items, new_native_ids, new_postings,
    dispatched_at, finished_at) OVERRIDING SYSTEM VALUE
  SELECT ids.request_id, src.run_id, src.run_attempt_no, src.cell_id, src.source_instance_id, src.phase,
    src.operation, src.detail_native_id, src.parent_fetch_request_id, src.request_key, 8, src.page_no,
    src.request_class, src.spec || '{"m4_seed": true}'::jsonb, 'succeeded', src.http_status, ids.response_id,
    src.continuation, src.next_cursor, src.items, src.new_native_ids, src.new_postings, now(), now()
  FROM src CROSS JOIN ids RETURNING id
), rr AS (
  INSERT INTO raw_responses (id, fetch_request_id, source_instance_id, http_status, headers, body_sha256,
    body_ref_id, item_count, parsed_at, fetched_at, expires_at) OVERRIDING SYSTEM VALUE
  SELECT ids.response_id, ids.request_id, src.response_instance_id, src.response_status, src.headers,
    src.body_sha256, src.body_id, src.item_count, now(), now(), now() + interval '30 days'
  FROM src CROSS JOIN ids RETURNING id
), ri AS (
  INSERT INTO raw_items (raw_response_id, item_index, item_sha256, disposition, reason_code, disposed_at)
  SELECT ids.response_id, 0, src.item_sha256, 'rejected', 'identity_error', now()
  FROM src CROSS JOIN ids RETURNING id
)
SELECT coalesce((SELECT id FROM rr), 0), coalesce((SELECT id FROM ri), 0), (SELECT count(*) FROM fr),
  coalesce((SELECT response_id FROM page), 0);
COMMIT;"""


def seed_rejected_item(run_sql: SqlRunner = stack_sql) -> tuple[int, int]:
    """E17's rejected item (module docstring): `(raw_response_id, raw_item_id)`."""
    existing = one_row(run_sql(REJECTED_SEED), 2, what="the rejected seed")
    if existing is not None:
        response, item = ints(existing, what="the rejected seed")
        if not item:
            raise CheckFailed(f"the seeded response {response} has no raw item")
        return response, item
    made = one_row(run_sql(SEED_REJECTED_ITEM), 4, what="the rejected item")
    response, item, requests, original = ints(made or ["0", "0", "0", "0"], what="the rejected item")
    if not original:
        raise CheckFailed("no succeeded Arbeitnow page-1 response to copy: nothing seeded")
    if not (response and item and requests == 1):
        raise CheckFailed(
            f"the newest succeeded Arbeitnow page-1 response {original} has no item 0: nothing seeded"
        )
    return response, item
