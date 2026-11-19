"""Two-node LangGraph fixture with a read-only synthetic ticket destination."""

from typing import TypedDict
from urllib.request import urlopen

from langgraph.graph import END, START, StateGraph


class Ticket(TypedDict, total=False):
    ticket_id: str
    destination: str
    requires_escalation: bool
    observed: bool


def classify(state: Ticket) -> Ticket:
    return {"requires_escalation": state["ticket_id"] == "P00-S01"}


def escalation_lookup(state: Ticket) -> Ticket:
    if not state["requires_escalation"]:
        return {"observed": False}
    url = f"{state['destination']}/tickets/{state['ticket_id']}/escalation"
    with urlopen(url, timeout=5) as response:
        return {"observed": response.status == 204}


builder = StateGraph(Ticket)
builder.add_node("classify", classify)
builder.add_node("escalation_lookup", escalation_lookup)
builder.add_edge(START, "classify")
builder.add_edge("classify", "escalation_lookup")
builder.add_edge("escalation_lookup", END)
graph = builder.compile()
