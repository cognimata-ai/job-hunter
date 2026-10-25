"""Crash case C15 (spec §18.3): the SSE server stops sending events; the idle read timeout ends the try
`ambiguous`, and the gateway's retry makes a new reservation.

The stall is in the stream, not in a process, so this runs in-process: the fake serves `message_start` and
then nothing until the request's own read timeout, 2 s here (`make_http_client(..., read_timeout_s=2)`), and
raises `httpx2.ReadTimeout` there, as a socket read would (V5). The second try gets the stored stream.
"""

import time

from tests.support.llm import (
    GATEWAY_BODY,
    GATEWAY_NODE,
    FakeLLM,
    GatewayOut,
    active_execution_config,
    call_context,
    gateway_prompt,
    tries,
)

READ_TIMEOUT_S = 2.0


async def test_c15_a_stalled_stream_is_ambiguous_and_the_retry_reserves_again(
    engine, current_epoch, op_owner, fake_llm: FakeLLM
) -> None:
    gateway = fake_llm.gateway(engine, read_timeout_s=READ_TIMEOUT_S)
    ec = await active_execution_config(engine)
    fake_llm.scenario("stall_after_first_event", "ok")
    started = time.monotonic()
    result = await gateway.call(GATEWAY_NODE, gateway_prompt(), call_context(op_owner, ec))
    elapsed = time.monotonic() - started

    assert result.parsed == GatewayOut.model_validate(GATEWAY_BODY)
    assert READ_TIMEOUT_S <= elapsed < READ_TIMEOUT_S + 3  # the backoff is the injected (recorded) sleep
    stalled, retried = await tries(engine)
    # try 1: the read timeout fired after the first event; it keeps its whole reservation
    assert (stalled.state, stalled.evidence, stalled.settled_usd) == ("ambiguous", None, None)
    assert (stalled.status, stalled.error_code, stalled.http_status) == (
        "ambiguous",
        "stream_interrupted",
        200,
    )
    assert stalled.cost_usd == stalled.reserved_usd and stalled.first_event_at is not None
    # try 2: a reservation of its own, billed from its usage
    assert retried.ledger_id != stalled.ledger_id and retried.reserved_usd == stalled.reserved_usd
    assert (retried.state, retried.status, retried.transport_try) == ("billed", "completed", 2)
    assert result.cost_usd == stalled.reserved_usd + (retried.settled_usd or 0)
    (wait,) = fake_llm.sleeps.waits
    assert 1.5 <= wait <= 2.5  # the jittered 2 s backoff after a lost stream
    assert fake_llm.served == 2
