import pytest
from jobhunter.graphs import checkpointer
from psycopg_pool import AsyncConnectionPool, PoolTimeout


async def test_open_saver_closes_its_pool_when_opening_fails(monkeypatch) -> None:
    """Ruling T13-g (b): a pool whose opening fails is closed, so its connection workers do not linger."""
    opened: list[AsyncConnectionPool] = []
    real_open = AsyncConnectionPool.open

    async def open_then_time_out(self: AsyncConnectionPool, *args: object, **kwargs: object) -> None:
        opened.append(self)
        await real_open(self, wait=False)  # the pool really starts: its workers try to connect
        raise PoolTimeout("pool initialization incomplete")

    monkeypatch.setattr(AsyncConnectionPool, "open", open_then_time_out)
    with pytest.raises(PoolTimeout):
        async with checkpointer.open_saver("postgresql://nobody@127.0.0.1:1/none?connect_timeout=1"):
            pytest.fail("the block must not run when the pool did not open")
    (pool,) = opened
    assert pool.closed
