"""Settings test helpers (M2 Task 17; Task 18 extends it): the save and read routes as a client uses them.

- `settings_body(client)`: `GET /api/settings` (200 required);
- `warning_codes(client, payload)`: the warning codes `POST /api/settings/dry-run` gives for `payload`, so a
  save acknowledges exactly its own warnings;
- `save(client, payload, *, base=None, key=None)`: `POST /api/settings` over the active version (or `base`),
  acknowledging the dry-run's warnings, with a fresh `Idempotency-Key` unless one is given; 201 required, its
  body returned;
- `save_scope(client, radius_km=…)`: the active payload with one scope location, Berlin, at that radius,
  saved as `save` does (the 201 body);
- `BERLIN`, `berlin(radius_km)`: that location (the GeoNames id of Berlin; coordinates of the city centre);
- `activate_profile(engine, seniority=…)`: a confirmed synthetic profile (an invented candidate,
  `SYNTHETIC_PROFILE`) made active with its tuple over the active model policy, as Task 21's confirm leaves
  the profile and tuple pointers (its scope part is not done here). Returns `(profile_version_id,
  llm_tuple_id)`;
- `active_row(engine)` and `generation(engine)`: the `active_config` row and the ranking generation.
"""

import copy
import hashlib
import uuid
from typing import Any, Final

import httpx
import sqlalchemy as sa
from jobhunter.db.transitions import transition
from jobhunter.llm.schemas.profile import ConfirmedProfile
from jobhunter.settings.save import ensure_tuple
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.profile import SYNTHETIC_PROFILE, idem

SETTINGS_URL: Final = "/api/settings"
DRY_RUN_URL: Final = "/api/settings/dry-run"
BERLIN: Final[dict[str, Any]] = {
    "label": "Berlin",
    "gazetteer_id": "geonames:2950159",
    "lat": 52.52437,
    "lon": 13.41053,
    "radius_km": 25,
}

_ACTIVE = sa.text("SELECT * FROM active_config WHERE id = 1")
_GENERATION = sa.text("SELECT generation FROM ranking_state WHERE id = 1")
_LOCK_ACTIVE = sa.text("SELECT 1 FROM active_config WHERE id = 1 FOR UPDATE")
_CV_FILE = sa.text(
    "INSERT INTO cv_files (sha256, mime, size_bytes) VALUES (:h, 'text/plain', 1) RETURNING id"
)
_DRAFT = sa.text(
    "INSERT INTO profile_drafts (cv_file_id, status, reviewed_text_sha256, draft_json) "
    "VALUES (:cv, 'ready', :h, CAST(:profile AS jsonb)) RETURNING id"
)
_VERSION = sa.text(
    "INSERT INTO profile_versions (version_no, draft_id, cv_file_id, profile_json, profile_hash) "
    "VALUES ((SELECT coalesce(max(version_no), 0) + 1 FROM profile_versions), :d, :cv, "
    "CAST(:profile AS jsonb), :h) RETURNING id"
)
_ACTIVATE = sa.text(
    "UPDATE active_config SET profile_version_id = :pv, llm_tuple_id = :t, updated_at = now() WHERE id = 1"
)


def berlin(radius_km: int) -> dict[str, Any]:
    return {**BERLIN, "radius_km": radius_km}


async def settings_body(client: httpx.AsyncClient) -> dict[str, Any]:
    """`GET /api/settings` as the client sees it."""
    r = await client.get(SETTINGS_URL)
    assert r.status_code == 200, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


async def warning_codes(client: httpx.AsyncClient, payload: Any) -> list[str]:
    """The codes of the warnings `POST /api/settings/dry-run` gives for `payload`, in its order."""
    r = await client.post(DRY_RUN_URL, json={"payload": payload})
    assert r.status_code == 200, (r.status_code, r.text)
    return [str(w["code"]) for w in r.json()["warnings"]]


def fresh_key(prefix: str = "settings-save") -> str:
    """A new `Idempotency-Key` (16 to 128 characters of `[A-Za-z0-9_-]`)."""
    return f"{prefix}-{uuid.uuid4().hex}"


async def save(
    client: httpx.AsyncClient, payload: Any, *, base: int | None = None, key: str | None = None
) -> dict[str, Any]:
    """`POST /api/settings` of `payload` over `base` (the active version by default), acknowledging the
    dry-run's warnings; 201 required, its body returned."""
    if base is None:
        base = int((await settings_body(client))["version"]["id"])
    r = await client.post(
        SETTINGS_URL,
        json={
            "payload": payload,
            "base_version_id": base,
            "acknowledged_warnings": await warning_codes(client, payload),
        },
        headers=idem(key or fresh_key()),
    )
    assert r.status_code == 201, (r.status_code, r.text)
    body: dict[str, Any] = r.json()
    return body


async def save_scope(client: httpx.AsyncClient, *, radius_km: int) -> dict[str, Any]:
    """The active payload with one scope location, Berlin, at `radius_km`, saved (the 201 body)."""
    active = await settings_body(client)
    payload = copy.deepcopy(active["payload"])
    payload["scope"]["locations"] = [berlin(radius_km)]
    return await save(client, payload, base=int(active["version"]["id"]))


async def active_row(engine: AsyncEngine) -> sa.Row[Any]:
    async with engine.connect() as conn:
        return (await conn.execute(_ACTIVE)).one()


async def generation(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(_GENERATION)).scalar_one())


async def activate_profile(engine: AsyncEngine, *, seniority: str = "senior") -> tuple[int, int]:
    """A confirmed synthetic profile and its tuple, made active (module docstring)."""
    profile = ConfirmedProfile.model_validate({**SYNTHETIC_PROFILE, "seniority": seniority})
    profile_json = profile.model_dump_json()
    digest = hashlib.sha256(profile_json.encode("utf-8")).digest()
    async with engine.begin() as conn:
        await conn.execute(_LOCK_ACTIVE)  # the first lock, as the confirm takes it (§9.1)
        cv = (await conn.execute(_CV_FILE, {"h": digest})).scalar_one()
        draft = (await conn.execute(_DRAFT, {"cv": cv, "h": digest, "profile": profile_json})).scalar_one()
        version = int(
            (
                await conn.execute(_VERSION, {"d": draft, "cv": cv, "profile": profile_json, "h": digest})
            ).scalar_one()
        )
        await transition(
            conn,
            "profile_drafts",
            draft,
            ["ready"],
            "confirmed",
            set_={
                "confirmed_profile_version_id": version,
                "confirm_response": {"profile_version_id": version},
            },
        )
        model_policy_id = int((await conn.execute(_ACTIVE)).one().model_policy_id)
        tuple_id = await ensure_tuple(conn, profile_version_id=version, model_policy_id=model_policy_id)
        await conn.execute(_ACTIVATE, {"pv": version, "t": tuple_id})
    return version, tuple_id
