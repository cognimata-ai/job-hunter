"""The api lifespan's shutdown (M0 Carry-forward 7, M4 Task 1): when it ends, the Argon2 gate's thread pool is
shut down with it."""

import pytest
from jobhunter.api import app as app_module
from jobhunter.api.argon2_gate import Argon2Gate

pytestmark = pytest.mark.integration


async def test_the_lifespan_closes_the_argon2_gate(make_config, monkeypatch) -> None:
    cfg = make_config("api")
    monkeypatch.setattr(app_module, "startup_or_exit", lambda role: cfg)
    api = app_module.create_app().api  # nothing injected: the lifespan runs
    gate = api.state.argon2_gate
    assert isinstance(gate, Argon2Gate)
    async with api.router.lifespan_context(api):
        assert api.state.cfg is cfg
        assert await gate.run(pow, 2, 10) == 1024  # open while the app runs
    with pytest.raises(RuntimeError):
        await gate.run(pow, 2, 10)
