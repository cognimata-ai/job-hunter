"""The stubbed stack the smoke scripts' unit tests share (plan M4 Task 36; its review, Minor 4): the M2 and M3
routes and statements (`Stack`, `Database`), the secret-shaped values and texts their answers hold and the
output must not (`TOKEN`, `COOKIE`, `KEY`, `AD_TEXT`, `CONTENT`), the M3 checks' names, and the grounded job's
invented description with its detail (`DESCRIPTION`, `detail_body`).

`tests/unit/test_smoke_m3_script.py`, `test_e2e_setup_script.py` and `test_smoke_m4_script.py` import them
from here, so a change to one test module's own stubs never changes another's. Every datum is invented.
"""

import json
import re
import sys
from types import ModuleType
from typing import Any, Final

import httpx

from tests.support.profile import SYNTHETIC_CV, SYNTHETIC_PROFILE, SYNTHETIC_QUERIES

M2_CHECK_NAMES: Final = [
    "user_create",
    "login",
    "health_ready",
    "cv_upload",
    "text_review",
    "extract_ready",
    "confirm",
    "proposal_ready",
]
M3_CHECK_NAMES: Final = [
    *M2_CHECK_NAMES,
    "targets_created",
    "score_cycle",
    "score_now",
    "feedback",
    "scope_apply",
    "batch_path",
    "health",
]
TOKEN: Final = "c2Vzc2lvbi10b2tlbi1vZi10aGUtc3R1Yg"  # the stub's session token
COOKIE: Final = f"__Host-jh_session={TOKEN}"
KEY: Final = "sk-ant-api03-c3R1Yi1rZXktbmV2ZXItcHJpbnRlZA"  # a key-shaped value in the stub's answers
AD_TEXT: Final = "Wir suchen eine Backend-Entwicklerin (m/w/d) für unser Plattform-Team in Berlin."
# Texts the stub's answers hold, as the stack's do, and the output must not: the CV, the profile, the
# proposal's query texts, an ad, and a key.
CONTENT: Final = (
    "Erika Musterfrau",
    "erika.musterfrau@example.org",
    "Musterfirma",
    SYNTHETIC_PROFILE["summary"],
    *(query["text"] for query in SYNTHETIC_QUERIES["queries"]),
    AD_TEXT,
    KEY,
)
# An invented description: three sentences of 30 to 160 code points, then a short one.
DESCRIPTION: Final = (
    "Wir entwickeln eine Plattform für erfundene Reiseerlebnisse in Berlin.\n"
    "Du baust Dienste mit Python und PostgreSQL. Du arbeitest eng mit dem Produktteam zusammen.\n"
    "Kurz."
)


def detail_body(quotes: list[str]) -> dict[str, Any]:
    """`GET /api/jobs/{JOB}` of the grounded job, as the API answers it: contested, with three critic rounds
    (the first with T1), a rejected draft of `role_fit` 3 -> 2, and the quotes spanned in the description."""
    criteria = []
    for i, key in enumerate(("role_fit", "skills", "experience_seniority", "domain", "languages")):
        quote = quotes[i % 3]
        start = DESCRIPTION.index(quote)
        criteria.append(
            {
                "criterion": key,
                "evidence": [
                    {
                        "source_kind": "description",
                        "quote": quote,
                        "span_start": start,
                        "span_end": start + len(quote),
                    }
                ],
            }
        )
    rounds = [{"round": n, "triggers": ["T1"] if n == 1 else ["T0"]} for n in (1, 2, 3)]
    return {
        "description": {"text": DESCRIPTION},
        "score": {"result_status": "contested", "criteria": criteria},
        "contested": {
            "rounds": rounds,
            "rejected_drafts": [
                {"round": 0, "level_diff": [{"criterion": "role_fit", "from_level": 3, "to_level": 2}]},
                {"round": 1, "level_diff": []},
            ],
        },
    }


def template(statement: str) -> re.Pattern[str]:
    """`statement` with each `{name}` placeholder matching what `str.format` puts there."""
    return re.compile(re.sub(r"\\\{\w+\\\}", r"[^\\s]+?", re.escape(statement)))


class Database:
    """The rows the stubbed stack's `psql` answers, by statement; `seen` the statements' names in order.
    The first bulk operation is cancelled by the second confirm; `batch_submit` is the bulk operation's
    `batch_submit` row (status, what it skipped, submissions) and `eligible` its eligible and too-old counts;
    `submission` is its submission's row (id, state, requests, every stamp set, items, items awaiting) and
    `followups` the `batch_followup` cycles requested: by default the full batch path, which the dev stack's
    fixtures take since their dates move with the clock (M4 Task 18)."""

    def __init__(
        self,
        smoke: ModuleType,
        *,
        batch_submit: str = "succeeded|none|1",
        eligible: str = "0|28",
        submission: str = "7|reconciled|243|t|243|0",
        followups: str = "1",
        old_bulk: str = "cancelled|0",
    ) -> None:
        self.seen: list[str] = []
        self.batch_submit = batch_submit
        self.eligible = eligible
        self.submission = submission
        self.followups = followups
        self.old_bulk = old_bulk
        self.confirmed = 0
        self.statements = [
            (name, template(getattr(smoke, name)))
            for name in (
                "SEED_BOARDS",
                "ACTIVE",
                "TARGETS_CREATED",
                "LANE_STATE",
                "ADVANCE_BUCKET",
                "PENDING_OF",
                "CYCLE_AFTER",
                "INSTANCES_OF",
                "PUBLISHED_JOB",
                "PENDING_JOB",
                "TARGET_STATE",
                "APPLY_STATE",
                "FILTERED_BY",
                "BULK_OF",
                "BULK_TARGETS",
                "OLD_BULK",
                "BATCH_SUBMIT_OF",
                "ELIGIBLE_OF",
                "SUBMISSION_OF",
                "DECISIONS_OF",
                "FOLLOWUP_REQUESTED",
                "COUNTERS_CLEAN",
            )
        ]
        self.seed_inputs = sys.modules["smoke_m1"].SEED_INPUTS
        self.proposals = template(sys.modules["smoke_m2"].PROPOSALS_OF)

    def answer(self, sql: str) -> str:
        if sql == self.seed_inputs:
            self.seen.append("SEED_INPUTS")
            return "1|2|3|4"
        if self.proposals.fullmatch(sql):
            return "1|7"  # the confirm's one proposal, id 7
        name = next((name for name, pattern in self.statements if pattern.fullmatch(sql)), None)
        assert name is not None, f"the stub has no statement {sql[:80]!r}"
        self.seen.append(name)
        tuple_id = 1 + self.confirmed
        rows = {
            "SEED_BOARDS": "4|4|1",
            "ACTIVE": f"{tuple_id}|{3 if 'APPLY_STATE' in self.seen else 2}",
            "TARGETS_CREATED": "succeeded|310|310|0",
            "LANE_STATE": "0|0|4",
            "ADVANCE_BUCKET": "1",
            "PENDING_OF": "250",
            "CYCLE_AFTER": "5|completed|28|28",
            "INSTANCES_OF": "29|0",
            "PUBLISHED_JOB": "41|141",
            "PENDING_JOB": "42|142",
            "TARGET_STATE": "published_accepted|score_now",
            "APPLY_STATE": "succeeded|3|0",
            "FILTERED_BY": "12",
            "BULK_OF": f"{tuple_id}|active",
            "BULK_TARGETS": "succeeded|298|298|0",
            "OLD_BULK": self.old_bulk,
            "BATCH_SUBMIT_OF": self.batch_submit,
            "ELIGIBLE_OF": self.eligible,
            "SUBMISSION_OF": self.submission,
            "DECISIONS_OF": "followup=241,published=2",
            "FOLLOWUP_REQUESTED": self.followups,
            "COUNTERS_CLEAN": "t|4.120000|3.910000",
        }
        return rows[name]


class Stack:
    """The api of a stubbed dev stack: every route the smoke uses, answered as the stack answers it, with the
    CV, profile, query and ad texts and a key in its answers. `warning` is the scope dry-run's one warning."""

    def __init__(self, database: Database, warning: str = "scope_narrowed") -> None:
        self.database = database
        self.warning = warning
        self.drafts = {11: "uploaded", 12: "uploaded"}
        self.uploads = 0
        self.seen: list[tuple[str, str]] = []
        self.saved_draft: Any = None
        self.settings_version = 1
        self.saves: list[tuple[str, Any]] = []  # each settings save's idempotency key and payload, in order

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = (request.method, request.url.path)
        self.seen.append(route)
        if route == ("POST", "/api/auth/login"):
            return httpx.Response(204, headers={"set-cookie": f"{COOKIE}; Path=/; Secure; HttpOnly"})
        assert request.headers["cookie"] == COOKIE, route
        if request.method in ("POST", "PUT"):
            assert (request.headers["origin"], request.headers["x-jh-request"]) == (
                "http://localhost:8080",
                "1",
            ), route
        return self.answer(request, route)

    def draft(self, draft_id: int) -> httpx.Response:
        status = self.drafts[draft_id]
        report = {"email": 1, "phone": 0, "url": 0, "address": 0, "personal": 0, "name": 1}
        body = {
            "status": status,
            "failure_code": None,
            "strip_report": report,
            "stripped_text_preview": SYNTHETIC_CV,
            "draft": SYNTHETIC_PROFILE if status == "ready" else None,
            "validation_issues": [{"path": "total_years_experience", "code": "years_mismatch"}],
        }
        return httpx.Response(200, json=body)

    def answer(self, request: httpx.Request, route: tuple[str, str]) -> httpx.Response:
        is_json = request.headers.get("content-type") == "application/json"
        sent: Any = json.loads(request.content) if is_json else None
        confirmed = self.database.confirmed
        match route:
            case ("GET", "/api/system/health"):
                scoring = {"running_cycle_id": None, "pause_reason": None, "pending_targets": 0}
                body = {
                    "ready": {"ok": True},
                    "embedder": {"ok": True},
                    "quota": {},
                    "last_run": {"id": 3, "status": "succeeded"},
                    "scoring": scoring,
                }
                return httpx.Response(200, json=body)
            case ("POST", "/api/profile/cv"):
                assert b"%PDF-1.4" in request.content
                self.uploads += 1
                draft_id = 10 + self.uploads
                self.drafts[draft_id] = "text_review"
                return httpx.Response(202, json={"draft_id": draft_id})
            case ("GET", path) if path.startswith("/api/profile/drafts/"):
                return self.draft(int(path.rsplit("/", 1)[1]))
            case ("POST", path) if path.endswith("/text-review"):
                assert request.headers["idempotency-key"]
                self.drafts[int(path.split("/")[-2])] = "ready"
                return httpx.Response(202, json={"status": "extracting_profile"})
            case ("PUT", "/api/profile/drafts/12"):
                self.saved_draft = sent["draft"]
                return httpx.Response(200, json={"validation_issues": []})
            case ("POST", path) if path.endswith("/confirm"):
                assert sent == {"expected_rescore_ack": True} and request.headers["idempotency-key"]
                self.database.confirmed += 1
                body = {"profile_version_id": 5 + confirmed, "targets_to_create": 310 * confirmed}
                return httpx.Response(201, json={**body, "est_usd": "0.000000", "bulk_budget_usd": "37.24"})
            case ("GET", "/api/profile"):
                active = {"id": 4 + confirmed, "version_no": confirmed, "profile": SYNTHETIC_PROFILE}
                return httpx.Response(200, json={"active": active, "versions": [active]})
            case ("GET", "/api/profile/query-proposals/7"):
                return httpx.Response(200, json={"status": "ready", "items": SYNTHETIC_QUERIES["queries"]})
            case ("POST", "/api/runs"):
                assert sent == {"include_paid": True} and request.headers["idempotency-key"]
                return httpx.Response(202, json={"run": {"id": 3}, "created": True})
            case ("POST", "/api/system/score-cycle"):
                assert request.headers["idempotency-key"]
                assert self.database.seen[-2:] == ["ADVANCE_BUCKET", "PENDING_OF"]  # the refill just before
                return httpx.Response(202, json={"queued": True, "reason": None})
            case ("GET", "/api/system/spend"):
                group = {"model": "claude-sonnet-5-5", "effort": "medium", "role": "generate", "calls": 28}
                body = {"counted": "1.400000", "c_hat": "0.052646", "c_hat_source": "central"}
                return httpx.Response(
                    200,
                    json={
                        **body,
                        "critic_rate_7d": None,
                        "by_model_effort_role": [{**group, "counted": "1.2"}],
                    },
                )
            case ("POST", "/api/jobs/42/score-now"):
                assert sent == {} and request.headers["idempotency-key"]
                return httpx.Response(202, json={"target_id": 142, "state": "pending"})
            case ("POST", "/api/jobs/41/feedback"):
                assert sent == {"kind": "label", "label": "good"}
                return httpx.Response(201, json={"feedback_id": 1, "golden_case_id": 9})
            case ("GET", "/api/golden-cases"):
                case = {"id": 9, "job_id": 41, "label": "good", "comment": AD_TEXT, "source": "week1"}
                return httpx.Response(200, json={"items": [{**case, "active": True}], "next_cursor": None})
            case ("GET", "/api/settings"):
                payload = {
                    "scope": {"locations": [], "excluded_keywords": [AD_TEXT]},
                    "scoring": {"batch_min_targets": 200, "min_cycle_targets": 10},
                    "anthropic": KEY,
                }
                return httpx.Response(
                    200, json={"version": {"id": self.settings_version}, "payload": payload}
                )
            case ("POST", "/api/settings/dry-run"):
                if sent["payload"]["scoring"]["batch_min_targets"] != 200:  # check 14's floor
                    assert sent["payload"]["scope"]["locations"] == []
                    return httpx.Response(200, json={"errors": [], "warnings": [{"code": "budget_tight"}]})
                (location,) = sent["payload"]["scope"]["locations"]
                assert location["radius_km"] == 200 and location["gazetteer_id"] == "geonames:2950159"
                return httpx.Response(200, json={"errors": [], "warnings": [{"code": self.warning}]})
            case ("POST", "/api/settings"):
                self.saves.append((request.headers["idempotency-key"], sent["payload"]))
                base = self.settings_version
                self.settings_version += 1
                saved = {"id": self.settings_version, "version_no": self.settings_version}
                if sent["payload"]["scoring"]["batch_min_targets"] != 200:
                    assert (base, sent["acknowledged_warnings"]) == (2, ["budget_tight"])
                    return httpx.Response(201, json={"version": {**saved, "effect_classes": ["budget"]}})
                assert (sent["base_version_id"], sent["acknowledged_warnings"]) == (1, [self.warning])
                return httpx.Response(
                    201,
                    json={"version": {**saved, "effect_classes": ["scope"]}, "apply_scope_operation_id": 77},
                )
        raise AssertionError(f"the stub stack has no route {route}")
