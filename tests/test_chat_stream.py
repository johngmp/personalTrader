"""ask_stream's distinction between a finished answer and one cut short by a cap.

Both look like plain prose in the transcript -- there is no "..." or anything else visually incomplete
about a reply chopped off mid-turn by MAX_TURNS or MAX_BUDGET_USD. Without a flag on the 'done' event,
the dashboard cannot tell the two apart, so a partial answer renders exactly like a complete one.
"""

from __future__ import annotations

import asyncio

import claude_agent_sdk

from trader.chat import ask_stream
from trader.config import PROJECT_ROOT


class ResultMessage:
    """Stands in for claude_agent_sdk's own ResultMessage.

    ask_stream only ever switches on ``type(message).__name__``, never isinstance, so a plain class of
    the same name is recognised exactly like the real one -- no need to depend on the SDK's internals.
    """

    def __init__(self, subtype: str, session_id: str = "sess-1") -> None:
        self.subtype = subtype
        self.session_id = session_id
        self.num_turns = 7
        self.total_cost_usd = 0.05
        self.content: list = []


def _fake_query(subtype: str):
    async def query(prompt, options=None, transport=None):
        yield ResultMessage(subtype)

    return query


def _run(monkeypatch, subtype: str):
    monkeypatch.setattr(claude_agent_sdk, "query", _fake_query(subtype))

    async def collect():
        return [item async for item in ask_stream("how is S1 doing?", PROJECT_ROOT)]

    return asyncio.run(collect())


def test_a_turn_cap_is_flagged_as_truncated(monkeypatch):
    events = _run(monkeypatch, "error_max_turns")
    kind, payload = events[-1]
    assert kind == "done"
    assert payload["truncated"] is True


def test_a_budget_cap_is_flagged_as_truncated(monkeypatch):
    events = _run(monkeypatch, "error_max_budget_usd")
    kind, payload = events[-1]
    assert kind == "done"
    assert payload["truncated"] is True


def test_a_normal_finish_is_not_flagged_as_truncated(monkeypatch):
    events = _run(monkeypatch, "success")
    kind, payload = events[-1]
    assert kind == "done"
    assert payload["truncated"] is False


def test_an_unrelated_error_subtype_is_not_called_a_truncation(monkeypatch):
    """error_during_execution and the like are failures, not a cap being hit -- don't conflate them."""
    events = _run(monkeypatch, "error_during_execution")
    kind, payload = events[-1]
    assert kind == "done"
    assert payload["truncated"] is False
