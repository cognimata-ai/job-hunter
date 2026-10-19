"""The node modules of the collection graph (spec §9.2); each `make_*(deps)` builds its node.

`Node` is the type of a node over the graph's state: LangGraph's node protocols name the parameter `state`,
which `mypy --strict` matches, so a plain `Callable` does not fit.
"""

from typing import Protocol

from jobhunter.graphs.collect.state import CollectState, CollectUpdate


class Node(Protocol):
    async def __call__(self, state: CollectState) -> CollectUpdate: ...
