import operator
from typing import Annotated, TypedDict

import pytest
from jobhunter.graphs.checkpointer import open_saver
from jobhunter.worker.runs import thread_id  # the run thread naming of Task 12: fails before Task 12 exists
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

pytestmark = pytest.mark.integration


class S(TypedDict):
    steps: Annotated[list[str], operator.add]


async def test_v6_resume_reruns_only_unfinished_node_and_thread_delete(migrated) -> None:
    calls = {"a": 0, "b": 0}

    async def a(state: S) -> S:
        calls["a"] += 1
        return {"steps": ["a"]}

    async def b(state: S) -> S:
        calls["b"] += 1
        if calls["b"] == 1:
            raise RuntimeError("crash in b")
        return {"steps": ["b"]}

    g = StateGraph(S)
    g.add_node("a", a)
    g.add_node("b", b)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)
    async with open_saver(migrated.dsn("app")) as saver:
        t1, t2 = thread_id(1, 1), thread_id(2, 1)
        app = g.compile(checkpointer=saver)
        cfg: RunnableConfig = {"configurable": {"thread_id": t1}}
        with pytest.raises(RuntimeError):
            await app.ainvoke({"steps": []}, cfg, durability="sync")
        out = await app.ainvoke(None, cfg, durability="sync")
        assert calls == {"a": 1, "b": 2} and out["steps"] == ["a", "b"]
        await app.ainvoke({"steps": []}, {"configurable": {"thread_id": t2}}, durability="sync")
        await saver.adelete_thread(t1)
        assert await saver.aget_tuple({"configurable": {"thread_id": t1}}) is None
        assert await saver.aget_tuple({"configurable": {"thread_id": t2}}) is not None
