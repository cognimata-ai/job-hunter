"""The profile review routes (spec §12.3, §12.6, §12.7, §14.2, §15.7, §20.1; M2 Task 14, Deviations 3, 9 and
29, rulings R14-1 to R14-5): the draft's status, its text edits, the approval, the field edits, the retry, the
discard and the profile list.

Drafts reach their states through the real pipeline: an upload or a paste through the api, the operations run
in process (`op_runner`) with docparse and the fake LLM on the committed synthetic stream. A purge is written
as §20.1's rules will write it (Task 23). Every name and datum is invented (Erika Musterfrau, Musterfirma
GmbH, example.org); no real CV, and no paid call.
"""

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from jobhunter.api.routes import profile as profile_routes
from jobhunter.db.transitions import transition
from jobhunter.llm import bounds, fake
from jobhunter.profile.contacts import Edits, apply_edits, strip_contacts
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.fixtures import count_rows
from tests.support.profile import (
    SYNTHETIC_CV,
    SYNTHETIC_PROFILE,
    OpRunner,
    Upload,
    cv_row,
    draft_row,
    draft_row_api,
    encrypted_pdf,
    idem,
    minimal_pdf,
    sha256,
)

pytestmark = pytest.mark.integration

# The synthetic CV with a protected-attribute line that holds a contact: `[PERSONAL]` takes the whole line,
# and the e-mail address in it is a contact of its own once the line is restored (Deviation 35).
REVIEW_CV = SYNTHETIC_CV + "\nReligion und Ethik als Wahlfach, Kontakt: erika.musterfrau@example.org\n"
NAME, EMAIL, PERSONAL = 0, 1, 2  # the replacements of `strip_contacts(REVIEW_CV)`
EMAIL_VALUE = "erika.musterfrau@example.org"
# What contact stripping takes out of `REVIEW_CV`: no response or log line may hold any of them.
STRIPPED_VALUES = ("Erika Musterfrau", EMAIL_VALUE, "Religion und Ethik", "Wahlfach")
FIRM = (REVIEW_CV.index("Musterfirma GmbH"), REVIEW_CV.index("Musterfirma GmbH") + len("Musterfirma GmbH"))
# The e-mail address inside the personal line, which has no index in the fresh strip.
INNER_EMAIL = (REVIEW_CV.rindex(EMAIL_VALUE), REVIEW_CV.rindex(EMAIL_VALUE) + len(EMAIL_VALUE))
PURGED = "The CV was deleted after 30 days; upload or paste it again"
BROKEN_PDF = b"%PDF-1.4\n" + b"\x00" * 64  # passes the upload checks, fails docparse (`parse_error`)
TEXT_PDF = minimal_pdf(SYNTHETIC_CV.splitlines())
EMPTY_EDITS: dict[str, list[object]] = {"restored": [], "removed": []}

_EXTRACT_OPS = sa.text("SELECT id, payload, dedup_key, status FROM operations WHERE kind = 'extract_profile'")
_PURGE_CONTENT = sa.text(
    "UPDATE profile_drafts SET draft_json = NULL, reviewed_text = NULL, content_purged_at = now() "
    "WHERE id = :d"
)
_PURGE_FILE = sa.text(
    "UPDATE cv_files SET storage_path = NULL, raw_text_enc = NULL, stripped_text = NULL, purged_at = now() "
    "WHERE id = (SELECT cv_file_id FROM profile_drafts WHERE id = :d)"
)


# --- helpers ----------------------------------------------------------------------------------------------


async def extract_ops(engine: AsyncEngine) -> list[sa.Row[Any]]:
    async with engine.connect() as conn:
        return list((await conn.execute(_EXTRACT_OPS)).all())


async def operation_jobs(engine: AsyncEngine, op_id: int) -> int:
    statement = sa.text(
        "SELECT count(*) FROM procrastinate_jobs WHERE task_name = 'jh:run_operation' "
        "AND (args ->> 'operation_id')::bigint = :i"
    )
    async with engine.connect() as conn:
        return int((await conn.execute(statement, {"i": op_id})).scalar_one())


async def execute(engine: AsyncEngine, statement: sa.TextClause, **params: object) -> None:
    async with engine.begin() as conn:
        await conn.execute(statement, params)


async def text_review_draft(op_runner: OpRunner, upload: Upload, text: str = REVIEW_CV) -> int:
    draft_id = await upload(text)
    await op_runner.run_pending()
    assert (await draft_row(op_runner.engine, draft_id)).status == "text_review"
    return draft_id


async def approve(client: httpx.AsyncClient, draft_id: int, key: str = "approve-key-0001") -> httpx.Response:
    return await client.post(f"/api/profile/drafts/{draft_id}/text-review", headers=idem(key))


async def ready_draft(client: httpx.AsyncClient, op_runner: OpRunner, upload: Upload) -> int:
    draft_id = await text_review_draft(op_runner, upload)
    assert (await approve(client, draft_id)).status_code == 202
    await op_runner.run_pending()
    assert (await draft_row(op_runner.engine, draft_id)).status == "ready"
    return draft_id


async def refused_paste(client: httpx.AsyncClient, op_runner: OpRunner, upload: Upload) -> int:
    """A paste whose approved text the model refused: `failed('refused')` with its reviewed text."""
    fake.scenario("refusal")
    draft_id = await text_review_draft(op_runner, upload)
    assert (await approve(client, draft_id)).status_code == 202
    await op_runner.run_pending()
    draft = await draft_row(op_runner.engine, draft_id)
    assert (draft.status, draft.failure_code, draft.reviewed_text is not None) == ("failed", "refused", True)
    fake.scenario("ok")
    return draft_id


async def put_text(client: httpx.AsyncClient, draft_id: int, *edits: dict[str, Any]) -> httpx.Response:
    return await client.put(f"/api/profile/drafts/{draft_id}/text", json={"span_edits": list(edits)})


def remove(start: int, end: int) -> dict[str, Any]:
    return {"op": "remove", "start": start, "end": end}


def restore(index: int) -> dict[str, Any]:
    return {"op": "restore", "replacement_index": index}


def restore_span(start: int, end: int) -> dict[str, Any]:
    return {"op": "restore", "start": start, "end": end}


async def retry(
    client: httpx.AsyncClient, draft_id: int, key: str = "retry-key-000001", **body: object
) -> httpx.Response:
    return await client.post(f"/api/profile/drafts/{draft_id}/retry", json=body, headers=idem(key))


def problem(r: httpx.Response) -> tuple[int, str]:
    return r.status_code, r.json()["code"]


def errors_of(r: httpx.Response) -> list[tuple[str, str]]:
    return [(e["field"], e["code"]) for e in r.json().get("errors") or []]


def assert_no_stripped_value(payload: object, *allowed: str) -> None:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    for value in STRIPPED_VALUES:
        if value not in allowed:
            assert value not in text, value


def expected_text(
    *, restored: tuple[int, ...] = (), spans: tuple[tuple[int, int], ...] = (), removed=()
) -> str:
    base = strip_contacts(REVIEW_CV).replacements
    edits = Edits(
        restored=frozenset({(base[i].start, base[i].end) for i in restored} | set(spans)),
        removed=frozenset(removed),
    )
    return apply_edits(REVIEW_CV, edits).stripped_text


# --- the Review Focus 1 pin -------------------------------------------------------------------------------


async def test_native_pdf_retry_requires_the_disclosure_ack(logged_in_client, failed_low_text_draft) -> None:
    did = failed_low_text_draft
    body = (await logged_in_client.get(f"/api/profile/drafts/{did}")).json()
    assert (
        body["status"] == "failed"
        and body["failure_code"] == "low_text"
        and body["repairs"] == ["paste_text", "native_pdf"]
    )
    r = await logged_in_client.post(
        f"/api/profile/drafts/{did}/retry", json={"mode": "native_pdf"}, headers=idem("k" * 16)
    )
    assert r.status_code == 422
    r = await logged_in_client.post(
        f"/api/profile/drafts/{did}/retry",
        json={"mode": "native_pdf", "ack_native_pdf": True},
        headers=idem("k2" * 8),
    )
    assert (
        r.status_code == 202
        and (await draft_row_api(logged_in_client, did))["status"] == "extracting_profile"
    )


# --- GET: the draft, its text and its map -----------------------------------------------------------------


async def test_a_draft_in_text_review_shows_its_stripped_text_and_map_and_no_stripped_value(
    engine, logged_in_client, op_runner, upload
) -> None:
    draft_id = await text_review_draft(op_runner, upload)
    body = await draft_row_api(logged_in_client, draft_id)
    fresh = strip_contacts(REVIEW_CV)
    assert body["status"] == "text_review"
    assert (body["draft"], body["validation_issues"], body["reviewed_text_sha256"], body["failure_code"]) == (
        None,
        [],
        None,
        None,
    )
    assert (body["repairs"], body["edits"], body["recomputed_total_years"]) == ([], EMPTY_EDITS, None)
    assert body["strip_report"] == dict(fresh.report)
    assert (
        body["stripped_text_preview"] == fresh.stripped_text == (await cv_row(engine, draft_id)).stripped_text
    )
    # The map (R14-2): every run in order, the replacements with their index into the fresh strip.
    segments = body["segments"]
    assert [(s["raw_start"], s["raw_end"]) for s in segments if s["kind"] == "replacement"] == [
        (r.start, r.end) for r in fresh.replacements
    ]
    assert [s["replacement_index"] for s in segments if s["kind"] == "replacement"] == [NAME, EMAIL, PERSONAL]
    assert all(s["restorable"] for s in segments if s["kind"] == "replacement")
    stripped, at = body["stripped_text_preview"], 0
    for s in segments:
        assert s["stripped_start"] == at
        shown = stripped[s["stripped_start"] : s["stripped_end"]]
        if s["kind"] == "text":
            assert shown == REVIEW_CV[s["raw_start"] : s["raw_end"]] and s["category"] is None
        else:
            assert shown == {"name": "[NAME]", "email": "[EMAIL]", "personal": "[PERSONAL]"}[s["category"]]
        at = s["stripped_end"]
    assert at == len(stripped) and segments[-1]["raw_end"] == len(REVIEW_CV)
    assert_no_stripped_value(body)  # offsets and the stripped text only: never a raw value


async def test_an_unknown_draft_is_a_404_on_every_route(logged_in_client) -> None:
    base = "/api/profile/drafts/987654"
    calls = [
        logged_in_client.get(base),
        logged_in_client.put(base, json={"draft": SYNTHETIC_PROFILE}),
        logged_in_client.put(f"{base}/text", json={"span_edits": []}),
        logged_in_client.post(f"{base}/text-review", headers=idem("unknown-key-0001")),
        logged_in_client.post(f"{base}/retry", json={"mode": "llm"}, headers=idem("unknown-key-0002")),
        logged_in_client.post(f"{base}/discard"),
    ]
    for call in calls:
        r = await call
        # The route's own answer, not the router's for a path it does not know.
        assert (problem(r), r.json()["title"]) == ((404, "not_found"), "Profile draft not found")


async def test_the_profile_list_is_empty_before_any_confirm(logged_in_client) -> None:
    r = await logged_in_client.get("/api/profile")
    assert (r.status_code, r.json()) == (200, {"active": None, "versions": []})


# --- PUT /text: the complete edit set (Deviation 29, R14-1, R14-2) ---------------------------------------


async def test_text_edits_remove_and_restore(engine, logged_in_client, op_runner, upload) -> None:
    draft_id = await text_review_draft(op_runner, upload)
    r = await put_text(logged_in_client, draft_id, remove(*FIRM), restore(EMAIL))
    assert r.status_code == 200
    preview = r.json()["stripped_text_preview"]
    assert preview == expected_text(restored=(EMAIL,), removed=(FIRM,))
    assert "Senior Backend-Entwicklerin, [REMOVED]\n" in preview and f"E-Mail: {EMAIL_VALUE}\n" in preview
    assert r.json() == {"stripped_text_preview": preview}
    cv = await cv_row(engine, draft_id)
    assert cv.stripped_text == preview
    assert cv.strip_report == {"email": 0, "phone": 0, "url": 0, "address": 0, "personal": 1, "name": 1}
    email = strip_contacts(REVIEW_CV).replacements[EMAIL]
    stored = (await draft_row(engine, draft_id)).text_edits
    assert stored == {"restored": [[email.start, email.end]], "removed": [list(FIRM)]}  # offsets only
    # An empty set undoes every edit: the fresh strip again.
    r = await put_text(logged_in_client, draft_id)
    assert r.json()["stripped_text_preview"] == strip_contacts(REVIEW_CV).stripped_text
    assert (await draft_row(engine, draft_id)).text_edits == EMPTY_EDITS


async def test_the_edit_set_composes_across_requests_and_a_reload_keeps_a_removal(
    engine, logged_in_client, op_runner, upload
) -> None:
    draft_id = await text_review_draft(op_runner, upload)
    first = await put_text(logged_in_client, draft_id, remove(*FIRM))
    assert first.json()["stripped_text_preview"] == expected_text(removed=(FIRM,))
    # A reloaded page knows only what GET returns: the stored set (R14-1).
    edits = (await draft_row_api(logged_in_client, draft_id))["edits"]
    assert edits == {"restored": [], "removed": [{"start": FIRM[0], "end": FIRM[1]}]}
    rebuilt = [remove(s["start"], s["end"]) for s in edits["removed"]]
    rebuilt += [restore_span(s["start"], s["end"]) for s in edits["restored"]]
    second = await put_text(logged_in_client, draft_id, *rebuilt, restore(EMAIL))
    assert second.status_code == 200
    both = second.json()["stripped_text_preview"]
    assert both == expected_text(restored=(EMAIL,), removed=(FIRM,))
    assert (
        "Musterfirma GmbH" not in both and EMAIL_VALUE in both
    )  # the earlier removal held, the value is back
    # The same as one request with both, in either order: the set, not its history, makes the text.
    one_go = await put_text(logged_in_client, draft_id, restore(EMAIL), remove(*FIRM))
    assert one_go.json()["stripped_text_preview"] == both
    stored = (await draft_row_api(logged_in_client, draft_id))["edits"]
    email = strip_contacts(REVIEW_CV).replacements[EMAIL]
    assert stored == {
        "restored": [{"start": email.start, "end": email.end}],
        "removed": [{"start": FIRM[0], "end": FIRM[1]}],
    }


async def test_restore_by_span_reaches_a_contact_inside_a_restored_personal_span(
    engine, logged_in_client, op_runner, upload
) -> None:
    draft_id = await text_review_draft(op_runner, upload)
    r = await put_text(logged_in_client, draft_id, restore(PERSONAL))
    preview = r.json()["stripped_text_preview"]
    assert preview.endswith("\nReligion und Ethik als Wahlfach, Kontakt: [EMAIL]\n")
    assert preview.count(EMAIL_VALUE) == 0  # the contact inside the restored line stays replaced
    # Its segment has no index in the fresh strip; it is restored by its span.
    segments = (await draft_row_api(logged_in_client, draft_id))["segments"]
    inner = [s for s in segments if s["kind"] == "replacement" and s["replacement_index"] is None]
    assert [(s["category"], s["restorable"], (s["raw_start"], s["raw_end"])) for s in inner] == [
        ("email", True, INNER_EMAIL)
    ]
    r = await put_text(
        logged_in_client,
        draft_id,
        restore(PERSONAL),
        restore_span(inner[0]["raw_start"], inner[0]["raw_end"]),
    )
    assert r.status_code == 200
    assert r.json()["stripped_text_preview"].endswith(f"Kontakt: {EMAIL_VALUE}\n")
    assert r.json()["stripped_text_preview"] == expected_text(restored=(PERSONAL,), spans=(INNER_EMAIL,))
    # Without its personal span the same span names no replacement.
    r = await put_text(logged_in_client, draft_id, restore_span(*INNER_EMAIL))
    assert (problem(r), errors_of(r)) == (
        (422, "validation_failed"),
        [("body.span_edits.0.start", "no_replacement")],
    )


@pytest.mark.parametrize(
    ("edits", "errors"),
    [
        pytest.param(
            [remove(10, 20), remove(15, 25)],
            [("body.span_edits.1.start", "overlap")],
            id="overlapping-removals",
        ),
        pytest.param(
            [remove(len(REVIEW_CV) - 2, len(REVIEW_CV) + 5)],
            [("body.span_edits.0.end", "out_of_range")],
            id="end",
        ),
        pytest.param([remove(-3, 4)], [("body.span_edits.0.start", "out_of_range")], id="negative-start"),
        pytest.param(
            [restore(3)], [("body.span_edits.0.replacement_index", "unknown_replacement")], id="index"
        ),
        pytest.param(
            [restore_span(5, 9)], [("body.span_edits.0.start", "no_replacement")], id="unmatched-span"
        ),
        pytest.param(
            [{"op": "remove", "start": 1, "end": 3, "replacement_index": 0}],
            [("body.span_edits.0.replacement_index", "unexpected")],
            id="removal-with-index",
        ),
        pytest.param(
            [{"op": "hide", "start": 1, "end": 3}], [("body.span_edits.0.op", "literal_error")], id="op"
        ),
    ],
)
async def test_an_invalid_edit_set_is_a_422_on_its_field_and_changes_nothing(
    engine, logged_in_client, op_runner, upload, edits: list[dict[str, Any]], errors: list[tuple[str, str]]
) -> None:
    draft_id = await text_review_draft(op_runner, upload)
    assert (await put_text(logged_in_client, draft_id, remove(*FIRM))).status_code == 200
    before = (await cv_row(engine, draft_id)).stripped_text, (await draft_row(engine, draft_id)).text_edits
    r = await put_text(logged_in_client, draft_id, *edits)
    assert (problem(r), errors_of(r)) == ((422, "validation_failed"), errors)
    assert_no_stripped_value(r.json())
    assert (
        (await cv_row(engine, draft_id)).stripped_text,
        (await draft_row(engine, draft_id)).text_edits,
    ) == before


async def test_editing_the_text_of_a_ready_draft_discards_the_extracted_draft(
    engine, logged_in_client, op_runner, upload
) -> None:
    draft_id = await ready_draft(logged_in_client, op_runner, upload)
    ready = await draft_row_api(logged_in_client, draft_id)
    assert ready["status"] == "ready" and ready["draft"] is not None and ready["validation_issues"]
    r = await put_text(logged_in_client, draft_id, remove(*FIRM))
    assert r.status_code == 200
    body = await draft_row_api(logged_in_client, draft_id)
    assert (body["status"], body["draft"], body["validation_issues"]) == ("text_review", None, [])
    assert body["edits"]["removed"] == [{"start": FIRM[0], "end": FIRM[1]}]
    row = await draft_row(engine, draft_id)
    assert (row.draft_json, row.status_reason, row.failure_code) == (None, None, None)
    # Approving the new text extracts again from it (§12.3).
    assert (await approve(logged_in_client, draft_id, "approve-key-0002")).status_code == 202
    assert (await draft_row(engine, draft_id)).reviewed_text == r.json()["stripped_text_preview"]


async def test_a_text_edit_outside_the_review_states_is_a_409(
    engine, logged_in_client, op_runner, upload
) -> None:
    draft_id = await text_review_draft(op_runner, upload)
    assert (await approve(logged_in_client, draft_id)).status_code == 202  # extracting_profile now
    before = (await cv_row(engine, draft_id)).stripped_text
    assert problem(await put_text(logged_in_client, draft_id, remove(*FIRM))) == (409, "conflict_state")
    assert (await cv_row(engine, draft_id)).stripped_text == before


async def test_a_native_draft_has_no_text_to_edit(engine, logged_in_client, upload) -> None:
    draft_id = await upload(TEXT_PDF, native_pdf_opt_in=True)  # the PDF itself is sent: no text was read
    async with engine.begin() as conn:  # as its extraction would end (not run here)
        await transition(
            conn,
            "profile_drafts",
            draft_id,
            ["extracting_profile"],
            "ready",
            set_={"draft_json": SYNTHETIC_PROFILE},
        )
    body = await draft_row_api(logged_in_client, draft_id)
    assert (body["status"], body["stripped_text_preview"], body["segments"]) == ("ready", None, [])
    assert problem(await put_text(logged_in_client, draft_id, remove(1, 2))) == (409, "conflict_state")
    assert (await draft_row(engine, draft_id)).status == "ready"


# --- POST /text-review: the approval ----------------------------------------------------------------------


async def test_approve_stores_the_reviewed_text_and_its_sha256_and_creates_one_operation(
    engine, logged_in_client, op_runner, upload
) -> None:
    draft_id = await text_review_draft(op_runner, upload)
    preview = (await put_text(logged_in_client, draft_id, remove(*FIRM))).json()["stripped_text_preview"]
    # As a draft is after a native retry whose result the user sent back to the text review: approving text
    # chooses the text path again, so the extraction must not send the PDF.
    await execute(engine, sa.text("UPDATE profile_drafts SET native_pdf = true WHERE id = :d"), d=draft_id)
    r = await approve(logged_in_client, draft_id)
    assert (r.status_code, r.json()) == (202, {"status": "extracting_profile"})
    row = await draft_row(engine, draft_id)
    assert (row.status, row.reviewed_text, bytes(row.reviewed_text_sha256)) == (
        "extracting_profile",
        preview,
        sha256(preview),
    )
    assert row.native_pdf is False and row.failure_code is None
    [op] = await extract_ops(engine)
    assert (op.payload, op.dedup_key, op.status) == (
        {"draft_id": draft_id},
        f"extract_profile:{draft_id}",
        "pending",
    )
    assert await operation_jobs(engine, op.id) == 1  # its job is deferred in the same transaction
    # The same key returns the stored response and starts nothing; a new key meets the new state.
    again = await approve(logged_in_client, draft_id)
    assert (again.status_code, again.json()) == (202, {"status": "extracting_profile"})
    assert len(await extract_ops(engine)) == 1
    assert problem(await approve(logged_in_client, draft_id, "approve-key-0003")) == (409, "conflict_state")
    missing = await logged_in_client.post(f"/api/profile/drafts/{draft_id}/text-review")
    assert problem(missing) == (400, "idempotency_key_required")
    body = await draft_row_api(logged_in_client, draft_id)
    assert body["reviewed_text_sha256"] == sha256(preview).hex()


# --- PUT /drafts/{id}: the field edits --------------------------------------------------------------------


async def test_a_saved_draft_is_validated_again_and_stored_repaired(
    engine, logged_in_client, op_runner, upload
) -> None:
    draft_id = await ready_draft(logged_in_client, op_runner, upload)
    draft = (await draft_row_api(logged_in_client, draft_id))["draft"]
    assert draft["user_entered"] == {"work_authorization_de": "unspecified", "security_clearances": []}
    draft["summary"] = "Senior Backend-Entwicklerin, Kontakt: max.mustermann@example.org"
    draft["total_years_experience"] = 10.5  # now within a year of the roles: no years_mismatch
    draft["skills"].append(
        {
            "name": "Rust",
            "category": "programming_language",
            "level": "basic",
            "years": 1,
            "evidence": "Rust (1 Jahr)",
        }
    )
    draft["user_entered"] = {
        "work_authorization_de": "authorized",
        "security_clearances": [{"name": " ", "level": None}],
    }
    r = await logged_in_client.put(f"/api/profile/drafts/{draft_id}", json={"draft": draft})
    assert r.status_code == 200
    issues = [(i["path"], i["code"]) for i in r.json()["validation_issues"]]
    assert issues == [
        ("summary", "contact_removed"),
        ("skills[6].evidence", "ungrounded"),
        ("user_entered.security_clearances[0].name", "limit"),
    ]
    assert "max.mustermann" not in r.text and set(r.json()) == {"validation_issues"}
    row = await draft_row(engine, draft_id)
    assert (
        row.draft_json["summary"] == ""
        and row.draft_json["user_entered"]["work_authorization_de"] == "authorized"
    )
    assert [(i["path"], i["code"]) for i in row.validation_issues] == issues
    shown = await draft_row_api(logged_in_client, draft_id)
    assert shown["draft"]["summary"] == "" and shown["validation_issues"] == r.json()["validation_issues"]
    # A draft in another state takes no field edit.
    other = await text_review_draft(op_runner, upload, SYNTHETIC_CV)
    assert problem(await logged_in_client.put(f"/api/profile/drafts/{other}", json={"draft": draft})) == (
        409,
        "conflict_state",
    )


async def test_the_recomputed_years_use_the_injected_clock(
    logged_in_client, op_runner, upload, monkeypatch
) -> None:
    draft_id = await ready_draft(logged_in_client, op_runner, upload)
    # 2016-01 to 2020-02 and 2020-03 to the day, merged: 3,917 days on 2026-10-21, 5,085 on 2030-01-01.
    monkeypatch.setattr(profile_routes, "utcnow", lambda: datetime(2026, 10, 21, 9, 0, tzinfo=UTC))
    assert (await draft_row_api(logged_in_client, draft_id))["recomputed_total_years"] == 10.7
    monkeypatch.setattr(profile_routes, "utcnow", lambda: datetime(2030, 1, 1, 9, 0, tzinfo=UTC))
    assert (await draft_row_api(logged_in_client, draft_id))["recomputed_total_years"] == 13.9


# --- repairs (Deviation 3) and the retry ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pdf", "code"),
    [
        pytest.param(BROKEN_PDF, "parse_error", id="parse_error"),
        pytest.param(None, "encrypted", id="encrypted"),
    ],
)
async def test_a_text_phase_failure_offers_paste_text(
    engine, logged_in_client, op_runner, upload, pdf: bytes | None, code: str
) -> None:
    draft_id = await upload(pdf or encrypted_pdf(TEXT_PDF))
    await op_runner.run_pending()
    body = await draft_row_api(logged_in_client, draft_id)
    assert (body["status"], body["failure_code"], body["repairs"]) == ("failed", code, ["paste_text"])


async def test_a_refused_paste_offers_retry_llm_until_its_content_is_purged(
    engine, logged_in_client, op_runner, upload
) -> None:
    draft_id = await refused_paste(logged_in_client, op_runner, upload)
    assert (await draft_row_api(logged_in_client, draft_id))["repairs"] == ["retry_llm"]
    await execute(engine, _PURGE_CONTENT, d=draft_id)
    assert (await draft_row_api(logged_in_client, draft_id))["repairs"] == []
    # The retry meets the purged input: 409 with the purge's detail, nothing changes.
    before = await draft_row(engine, draft_id)
    r = await retry(logged_in_client, draft_id, mode="llm")
    assert problem(r) == (409, "conflict_state") and r.json()["detail"] == PURGED
    assert len(await extract_ops(engine)) == 1  # the approval's only
    assert await draft_row(engine, draft_id) == before


async def test_no_native_pdf_repair_or_retry_while_the_native_path_is_disabled(
    engine, logged_in_client, failed_low_text_draft, monkeypatch
) -> None:
    monkeypatch.setattr(bounds, "NATIVE_PDF_ENABLED", False)  # read per request (R14-4)
    did = failed_low_text_draft
    assert (await draft_row_api(logged_in_client, did))["repairs"] == ["paste_text"]
    r = await retry(logged_in_client, did, mode="native_pdf", ack_native_pdf=True)
    assert problem(r) == (409, "conflict_state")
    assert await extract_ops(engine) == [] and (await draft_row(engine, did)).status == "failed"


async def test_retry_llm_of_a_text_phase_failure_is_a_409_not_a_500(
    engine, logged_in_client, failed_low_text_draft
) -> None:
    r = await retry(logged_in_client, failed_low_text_draft, mode="llm")
    assert problem(r) == (409, "conflict_state") and r.json()["detail"] != PURGED
    assert await extract_ops(engine) == []
    assert (await draft_row(engine, failed_low_text_draft)).status == "failed"
    assert await count_rows(engine, "alerts", code="api.unhandled_exception") == 0


async def test_a_native_retry_of_a_purged_file_is_a_409_and_changes_nothing(
    engine, logged_in_client, failed_low_text_draft
) -> None:
    did = failed_low_text_draft
    await execute(engine, _PURGE_FILE, d=did)
    assert (await draft_row_api(logged_in_client, did))["repairs"] == ["paste_text"]
    r = await retry(logged_in_client, did, mode="native_pdf", ack_native_pdf=True)
    assert problem(r) == (409, "conflict_state") and r.json()["detail"] == PURGED
    assert await extract_ops(engine) == []
    cv = await cv_row(engine, did)
    assert (cv.native_pdf_opt_in, cv.extraction_method) == (False, None)


async def test_a_native_retry_after_the_content_purge_reopens_the_draft_unpurged(
    engine, logged_in_client, failed_low_text_draft
) -> None:
    did = failed_low_text_draft
    await execute(engine, _PURGE_CONTENT, d=did)
    assert (await draft_row_api(logged_in_client, did))["repairs"] == ["paste_text", "native_pdf"]
    r = await retry(logged_in_client, did, mode="native_pdf", ack_native_pdf=True)
    assert (r.status_code, r.json()) == (202, {"status": "extracting_profile"})
    draft = await draft_row(engine, did)
    assert (
        draft.status,
        draft.native_pdf,
        draft.content_purged_at,
        draft.failure_code,
        draft.status_reason,
    ) == (
        "extracting_profile",
        True,
        None,
        None,
        None,
    )
    cv = await cv_row(engine, did)
    assert (cv.native_pdf_opt_in, cv.extraction_method) == (True, "native_pdf")
    [op] = await extract_ops(engine)
    assert (op.payload, op.dedup_key) == ({"draft_id": did}, f"extract_profile:{did}")


async def test_a_retry_is_idempotent_and_only_from_failed(
    engine, logged_in_client, op_runner, upload
) -> None:
    draft_id = await refused_paste(logged_in_client, op_runner, upload)
    r = await retry(logged_in_client, draft_id, mode="llm")
    assert (r.status_code, r.json()) == (202, {"status": "extracting_profile"})
    draft = await draft_row(engine, draft_id)
    assert (draft.status, draft.failure_code, draft.native_pdf) == ("extracting_profile", None, False)
    assert len(await extract_ops(engine)) == 2  # the approval's, ended, and the retry's
    again = await retry(logged_in_client, draft_id, mode="llm")
    assert (again.status_code, again.json()) == (202, {"status": "extracting_profile"})
    assert len(await extract_ops(engine)) == 2
    reused = await retry(logged_in_client, draft_id, mode="native_pdf", ack_native_pdf=True)
    assert problem(reused) == (422, "idempotency_key_reused")
    assert problem(await retry(logged_in_client, draft_id, "retry-key-000002", mode="llm")) == (
        409,
        "conflict_state",
    )
    # The retried extraction runs on the same approved text.
    await op_runner.run_pending()
    assert (await draft_row(engine, draft_id)).status == "ready"


# --- discard -------------------------------------------------------------------------------------------


async def discard_once(engine: AsyncEngine, client: httpx.AsyncClient, draft_id: int) -> None:
    r = await client.post(f"/api/profile/drafts/{draft_id}/discard")
    assert (r.status_code, r.content) == (204, b"")
    row = await draft_row(engine, draft_id)
    assert (row.status, row.status_reason) == ("discarded", "user_discard")
    assert problem(await client.post(f"/api/profile/drafts/{draft_id}/discard")) == (409, "conflict_state")


async def test_discard_ends_a_reviewable_or_failed_draft(
    engine, logged_in_client, op_runner, upload, failed_low_text_draft
) -> None:
    await discard_once(engine, logged_in_client, failed_low_text_draft)
    await discard_once(engine, logged_in_client, await text_review_draft(op_runner, upload))
    await discard_once(engine, logged_in_client, await ready_draft(logged_in_client, op_runner, upload))
    extracting = await text_review_draft(op_runner, upload)
    assert (await approve(logged_in_client, extracting, "approve-key-0004")).status_code == 202
    assert problem(await logged_in_client.post(f"/api/profile/drafts/{extracting}/discard")) == (
        409,
        "conflict_state",
    )


# --- privacy (R14-5) -------------------------------------------------------------------------------------


async def test_no_response_or_log_line_holds_a_stripped_or_removed_value(
    engine, logged_in_client, op_runner, upload, log_events
) -> None:
    draft_id = await text_review_draft(op_runner, upload)
    responses = [
        await put_text(logged_in_client, draft_id, remove(*FIRM), restore(PERSONAL)),
        await logged_in_client.get(f"/api/profile/drafts/{draft_id}"),
        await put_text(logged_in_client, draft_id, remove(1, 2), remove(1, 3)),  # a 422
        await approve(logged_in_client, draft_id),
        await logged_in_client.get(f"/api/profile/drafts/{draft_id}"),
    ]
    for r in responses:
        # The restored personal line is the user's choice to send; its inner e-mail address stays replaced,
        # and the removed firm is gone from every answer.
        assert_no_stripped_value(r.text, "Religion und Ethik", "Wahlfach")
        assert "Musterfirma GmbH" not in r.text
    assert_no_stripped_value(json.dumps(log_events()))
    assert "Musterfirma" not in json.dumps(log_events())
