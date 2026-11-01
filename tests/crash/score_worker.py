"""`jobhunter worker` with a stalling fake LLM, for the score lane's crash tests (M3 Task 28):
`python -m tests.crash.score_worker`.

It registers a one-shot keyed answer `stall_after_first_event` for the answer key `JH_TEST_STALL_KEY`
(`llm.fake.answer_key`, e.g. `generate:<job_revision_id>:0`) in the fake's process-wide state, so the first
try sent with that key gets its recorded `message_start` event and then nothing, for `JH_TEST_STALL_S`
seconds, before it fails as a read timeout would; every later try of that key is the committed recording's.
Then it runs the worker exactly as `jobhunter worker` does (`worker_main`), on the fake LLM
(`JOBHUNTER_FAKE_LLM=1`, which `startup_or_exit` requires here). The stall prints `{"event":
"fake_stall_started", "key": <key>}` when it begins (the test's readiness acknowledgement that the call is in
flight). A cancellation ends the stall at once, as it ends a real stalled stream.
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Final

import httpx2
from jobhunter.config import startup_or_exit
from jobhunter.llm import fake

KEY_VAR: Final = "JH_TEST_STALL_KEY"
SECONDS_VAR: Final = "JH_TEST_STALL_S"


def stalling_stream(key: str, stall_s: float) -> type[fake._StalledStream]:
    """`fake._StalledStream` with a stall of `stall_s` seconds, announced on stdout."""

    class Stalled(fake._StalledStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield self._first
            print(json.dumps({"event": "fake_stall_started", "key": key}), flush=True)
            await asyncio.sleep(stall_s)
            raise httpx2.ReadTimeout("fake: the stall ran out", request=self._request)

    return Stalled


def main() -> int:
    cfg = startup_or_exit("worker")
    if not cfg.fake_llm:
        raise SystemExit("tests.crash.score_worker runs on the fake LLM only (JOBHUNTER_FAKE_LLM=1)")
    key, stall_s = os.environ[KEY_VAR], float(os.environ[SECONDS_VAR])
    fake._StalledStream = stalling_stream(key, stall_s)  # type: ignore[misc]
    fake.answer(key, "stall_after_first_event", consume=True)  # the process-wide state: no test block here
    from jobhunter.worker.main import worker_main  # imported as `jobhunter worker` imports it

    return asyncio.run(worker_main(cfg))


if __name__ == "__main__":
    raise SystemExit(main())
