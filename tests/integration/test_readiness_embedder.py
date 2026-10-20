import pytest
from jobhunter.embeddings import EMBEDDING_MODEL_ID

pytestmark = pytest.mark.integration


async def test_readyz_and_health_report_the_embedder(
    api_client, logged_in_client, fake_embedder, worker_lock_heartbeat, log_events
) -> None:
    assert (await api_client.get("/readyz")).status_code == 200
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["embedder"] == {"ok": True, "model_id": EMBEDDING_MODEL_ID, "checksum_ok": True}
    assert body["ready"] == {"ok": True, "failing": []}

    fake_embedder.checksum_ok = False
    r = await api_client.get("/readyz")
    assert r.status_code == 503 and r.json() == {"failing": ["embedder"]}
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["embedder"] == {"ok": False, "model_id": EMBEDDING_MODEL_ID, "checksum_ok": False}
    assert body["ready"] == {"ok": False, "failing": ["embedder"]}

    fake_embedder.down = True
    assert (await api_client.get("/readyz")).json() == {"failing": ["embedder"]}
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["embedder"] == {"ok": False, "model_id": None, "checksum_ok": None}

    fake_embedder.reset()
    fake_embedder.model_id = "another-model@0/fp32"  # an embedder this code cannot use
    assert (await api_client.get("/readyz")).json() == {"failing": ["embedder"]}
    body = (await logged_in_client.get("/api/system/health")).json()
    assert body["embedder"] == {"ok": False, "model_id": "another-model@0/fp32", "checksum_ok": True}

    failed = [(e["probe"], e["reason"]) for e in log_events() if e["event"] == "readiness_probe_failed"]
    assert set(failed) == {
        ("embedder", "checksum_failed"),
        ("embedder", "unavailable"),
        ("embedder", "unusable"),
    }


async def test_the_embedder_probe_runs_beside_a_failed_database(
    fresh_pg_server, make_config, fake_embedder
) -> None:
    import httpx
    from jobhunter.api.app import create_app
    from jobhunter.db.session import make_engine

    cfg = make_config("api", fresh_pg_server)
    engine = make_engine(cfg)
    fake_embedder.down = True
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(cfg=cfg, engine=engine)),
            base_url="https://testserver",
        ) as c:
            r = await c.get("/readyz")
    finally:
        await engine.dispose()
    # every failing component is reported, the embedder after the database's codes
    assert r.status_code == 503 and r.json() == {"failing": ["migrations", "worker_heartbeat", "embedder"]}
