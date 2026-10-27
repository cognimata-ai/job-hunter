"""The batch params (spec §10.9 L3932, §10.10, §10.6; plan Task 16, Carry T8-a): an item's params are the
online generator body for the same prompt except `stream`, with the prefix's cache marker on `system[1]` and
`max_tokens` the first stage; and the fake's answer-key header goes with a create in fake LLM mode only.

Offline, no database: the online body is one `gateway._chat` try on the capturing transport
(`tests.support.llm.capture_request`), the batch params `LLMGateway.batch_request_body` (which builds no
connection), and the create goes to a transport of the test's own. The profile and the ad are synthetic.
"""

import dataclasses
import json
from types import MappingProxyType
from typing import Any, Final, cast

import httpx2
from jobhunter.config import AppConfig
from jobhunter.db.seed import load_settings_v1
from jobhunter.llm.batches import BatchesClient, BatchRequest, Created
from jobhunter.llm.cache_monitor import CacheMonitor
from jobhunter.llm.fake import KEY_HEADER, PURPOSE_HEADER, batch_keys
from jobhunter.llm.gateway import LLMGateway, NodeConfig, RenderedPrompt, node_config
from jobhunter.llm.prompts.render import job_ad_block, score_prompt, score_schema
from jobhunter.llm.transport import make_http_client
from jobhunter.scoring.rubric import RUBRIC_V2, load_rubric
from jobhunter.types import get_object
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.llm import (
    PAYLOAD_KEY,
    cache_control_paths,
    capture_request,
    generator_wire_body,
    score_profile,
    v2_version,
)

SETTINGS = load_settings_v1()
MODEL_POLICY = get_object(SETTINGS, "scoring.model_policy")
EXECUTION = get_object(SETTINGS, "scoring.execution")
VERSION = v2_version(2)
AD = job_ad_block(
    title="Backend Engineer (Python)",
    company="Beispiel Logistik GmbH",
    locations=["Hamburg"],
    work_modes=[("hybrid", "structured")],
    employment_types=[("full_time", "structured")],
    completeness="full",
    description_text="Wir suchen eine:n Backend Engineer. Du entwickelst Services in Python und PostgreSQL.",
)
_BATCH: Final[dict[str, Any]] = {
    "id": "msgbatch_payload",
    "type": "message_batch",
    "processing_status": "in_progress",
    "request_counts": {"processing": 1, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
    "ended_at": None,
    "created_at": "2026-10-26T12:00:00Z",
    "expires_at": "2026-10-27T12:00:00Z",
    "archived_at": None,
    "cancel_initiated_at": None,
    "results_url": None,
}


def generate_node() -> NodeConfig:
    return node_config(
        "generate", model_policy=MODEL_POLICY, execution=EXECUTION, schema=score_schema("generate", VERSION)
    )


def generate_prompt() -> RenderedPrompt:
    return score_prompt(
        "generate", version=VERSION, rubric=load_rubric(RUBRIC_V2), profile=score_profile(), ad=AD
    )


def keyed(cfg: AppConfig) -> AppConfig:
    """`cfg` with the payload tests' key (never a real one: nothing leaves the process)."""
    return dataclasses.replace(
        cfg, secrets=MappingProxyType({"anthropic_key": PAYLOAD_KEY.get_secret_value().encode()})
    )


class CreateCapture(httpx2.AsyncBaseTransport):
    """The API origin's transport: every request kept, each answered with one in-progress batch."""

    def __init__(self) -> None:
        self.seen: list[httpx2.Request] = []
        self.bodies: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.seen.append(request)
        self.bodies.append(json.loads(await request.aread()))
        return httpx2.Response(200, headers={"request-id": "req_payload"}, json=_BATCH)


async def test_the_batch_params_are_the_online_generator_body_without_stream(payload_cfg: AppConfig) -> None:
    cfg = keyed(payload_cfg)
    node, prompt = generate_node(), generate_prompt()
    online = await capture_request(cfg, node, prompt, generator_wire_body())
    async with make_http_client(cfg, mount=CreateCapture()) as http:
        gateway = LLMGateway(cast(AsyncEngine, None), cfg, http_client=http, cache=CacheMonitor())
        params: dict[str, Any] = gateway.batch_request_body(node, prompt, max_tokens=node.max_tokens)
    assert online.body["stream"] is True and "stream" not in params
    assert params == {key: value for key, value in online.body.items() if key != "stream"}
    assert params["max_tokens"] == get_object(EXECUTION, "generate")["max_tokens"] == 6000  # the first stage
    assert cache_control_paths(params) == ["/system/1/cache_control"]  # the prefix marker kept (L6622)
    assert params["output_config"]["format"]["schema"] == dict(node.schema.json)


async def test_the_fake_key_map_goes_with_a_create_in_fake_mode_only(payload_cfg: AppConfig) -> None:
    """Carry T8-a: the fake reads each item's answer key from `X-JH-Fake-Key` (`fake.batch_keys`); a live
    create carries neither it nor `X-JH-Purpose`."""
    node = generate_node()
    requests = [
        BatchRequest(f"t{i}_a1", {"model": node.model, "max_tokens": 6000}, {"job_revision_id": 700 + i})
        for i in (1, 2)
    ]
    sent: dict[bool, httpx2.Request] = {}
    for fake_mode in (False, True):
        cfg = dataclasses.replace(keyed(payload_cfg), fake_llm=fake_mode)
        transport = CreateCapture()
        async with make_http_client(cfg, mount=transport) as http:
            created = await BatchesClient(cfg, http_client=http).create(requests)
        assert isinstance(created, Created) and created.provider_batch_id == "msgbatch_payload"
        (request,) = transport.seen
        assert (request.method, request.url.host, request.url.path) == (
            "POST",
            "api.anthropic.com",
            "/v1/messages/batches",
        )
        assert transport.bodies[0] == {
            "requests": [{"custom_id": r.custom_id, "params": dict(r.params)} for r in requests]
        }
        sent[fake_mode] = request
    live, fake = sent[False], sent[True]
    assert KEY_HEADER.lower() not in live.headers and PURPOSE_HEADER.lower() not in live.headers
    assert live.headers["x-api-key"] == PAYLOAD_KEY.get_secret_value()
    assert fake.headers[KEY_HEADER] == batch_keys({"t1_a1": "generate:701:0", "t2_a1": "generate:702:0"})
    assert fake.headers[PURPOSE_HEADER] == "generate"
