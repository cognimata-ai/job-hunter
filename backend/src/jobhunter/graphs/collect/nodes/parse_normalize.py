"""Node 5, `parse_normalize` (spec §7.16.1, §9.2): one `raw_items` row per element of the run's responses.

It passes `collect.before_parse` first (crash case C4). Then, for every `raw_responses` row of the run
(through its fetch request's `run_id`) whose request `succeeded` and whose `parsed_at` is unset, in id order:
`items.parse_response` in a transaction of its own, with the adapter of the response's source instance, then
`collect.parse.between_responses` (crash case C5). A `failed('contract')` 200 has `parsed_at` set when it is
stored, so it is never selected; a resumed attempt finds the responses parsed before with `parsed_at` set and
parses only the others, and a response's rows are written once (`ON CONFLICT DO NOTHING` besides).

Once a response's transaction has committed, its items are counted in `RAW_ITEMS` (by source and
disposition). The update's `counters` are the dispositions of the responses this call parsed
(`raw_items_pending`; `raw_items_out_of_scope`, one total whatever the reason, ruling T15-c;
`raw_items_duplicate_in_page`; `raw_items_rejected`): the run's own totals are its rows (I1, §17.2), whichever
attempt wrote them.
"""

from dataclasses import asdict
from typing import Final, get_args

from jobhunter.db.sql import q
from jobhunter.faults import afault_point
from jobhunter.graphs.collect.deps import CollectDeps
from jobhunter.graphs.collect.nodes import Node
from jobhunter.graphs.collect.state import CollectState, CollectUpdate
from jobhunter.observability.metrics import RAW_ITEMS
from jobhunter.sources import items
from jobhunter.sources.base import SourceAdapter
from jobhunter.sources.instances import list_instances
from jobhunter.worker.runs import RunOwner

PARSE_NORMALIZE: Final = "parse_normalize"
_UNPARSED: Final = q(
    "SELECT r.id, f.source_instance_id FROM raw_responses r "
    "JOIN fetch_requests f ON f.id = r.fetch_request_id "
    "WHERE f.run_id = :run AND f.status = 'succeeded' AND r.parsed_at IS NULL ORDER BY r.id"
)


def make_parse_normalize(deps: CollectDeps) -> Node:
    async def parse_normalize(state: CollectState) -> CollectUpdate:
        await afault_point("collect.before_parse")
        owner = RunOwner(state["run_id"], state["attempt_no"])
        async with deps.engine.connect() as conn:
            unparsed = (await conn.execute(_UNPARSED, {"run": owner.run_id})).all()
            instances = {instance.id: instance for instance in await list_instances(conn)} if unparsed else {}
        adapters: dict[int, SourceAdapter] = {}
        counters = {f"raw_items_{disposition}": 0 for disposition in get_args(items.Disposition)}
        for raw_response_id, instance_id in unparsed:
            instance = instances[int(instance_id)]
            if instance.id not in adapters:
                adapters[instance.id] = deps.adapters(instance)
            async with deps.engine.begin() as conn:
                counts = await items.parse_response(
                    conn, int(raw_response_id), adapter=adapters[instance.id], owner=owner
                )
            for disposition, n in asdict(counts).items():
                counters[f"raw_items_{disposition}"] += n
                if n:
                    RAW_ITEMS.labels(source=instance.source_id, disposition=disposition).inc(n)
            await afault_point("collect.parse.between_responses")
        return {"counters": counters, "steps": [PARSE_NORMALIZE]}

    return parse_normalize
