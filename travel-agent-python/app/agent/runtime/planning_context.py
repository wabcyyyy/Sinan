"""CP 研究与路网阶段快照；沿用同机 SQLite 检查点与清理周期。"""

import hashlib

from langgraph.graph import END, START, StateGraph

from app.agent.runtime import checkpoint


def _graph():
    graph = StateGraph(dict)
    graph.add_node("snapshot", lambda state: state)
    graph.add_edge(START, "snapshot")
    graph.add_edge("snapshot", END)
    return graph.compile(checkpointer=checkpoint.get_checkpointer())


def _key(user_id, itinerary_id, fingerprint, deadline_at):
    identity = f"{user_id}:{fingerprint}:{deadline_at:.6f}"
    return checkpoint.run_config(f"constraint-context-{itinerary_id}-{hashlib.sha256(identity.encode()).hexdigest()}")


def save_constraint_context(user_id, itinerary_id, fingerprint, deadline_at, context):
    _graph().invoke({"context": context}, _key(user_id, itinerary_id, fingerprint, deadline_at))


def load_constraint_context(user_id, itinerary_id, fingerprint, deadline_at):
    state = _graph().get_state(_key(user_id, itinerary_id, fingerprint, deadline_at))
    return state.values.get("context") if state.values else None
