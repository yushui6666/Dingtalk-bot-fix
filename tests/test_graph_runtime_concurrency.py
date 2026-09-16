"""GraphRuntime 工单级并发：不同工单并行，同一工单严格串行。"""

from __future__ import annotations

import asyncio

import pytest

from graphs.runtime import GraphRuntime


class ProbeTicketGraph:
    def __init__(self, delay: float = 0.05) -> None:
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.calls: list[int] = []

    async def ainvoke(self, state, config):
        ticket_id = int(state["ticket_id"])
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            self.calls.append(ticket_id)
            return {"ticket_id": ticket_id}
        finally:
            self.active -= 1


def _runtime(tmp_path) -> tuple[GraphRuntime, ProbeTicketGraph]:
    runtime = GraphRuntime(
        pipeline=object(),
        checkpoint_path=tmp_path / "checkpoints.sqlite",
    )
    graph = ProbeTicketGraph()
    runtime._ticket_graph = graph  # 单测绕过 checkpointer 初始化
    return runtime, graph


@pytest.mark.asyncio
async def test_different_tickets_run_in_parallel(tmp_path):
    runtime, graph = _runtime(tmp_path)

    await asyncio.gather(
        runtime.invoke_ticket(1, {"message_id": "m1"}),
        runtime.invoke_ticket(2, {"message_id": "m2"}),
    )

    assert graph.max_active == 2


@pytest.mark.asyncio
async def test_same_ticket_is_serialized(tmp_path):
    runtime, graph = _runtime(tmp_path)

    await asyncio.gather(
        runtime.invoke_ticket(1, {"message_id": "m1"}),
        runtime.invoke_ticket(1, {"message_id": "m2"}),
    )

    assert graph.max_active == 1
    assert graph.calls == [1, 1]
